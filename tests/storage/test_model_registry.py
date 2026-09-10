import sqlite3
from pathlib import Path

import pytest

from tts_studio.models.registry import (
    DownloadState,
    InvalidDownloadTransitionError,
    InvalidRegistryDataError,
    ModelRegistry,
)
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _registry(tmp_path: Path) -> tuple[ModelRegistry, Database]:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = ModelRegistry(database)
    registry.upsert_engine_installation(
        engine_installation_id="fake@1",
        engine_id="fake",
        version="1.0.0",
        command=["uv", "run", "tts-studio-fake-worker"],
        working_directory="workers/fake",
        environment={"lockfile": "uv.lock"},
        capabilities={"model_download": True},
        lifecycle_state="ready",
    )
    return registry, database


def _new_job(registry: ModelRegistry, suffix: str) -> str:
    return registry.create_download_job(
        job_id=f"job-{suffix}",
        repository_id="fixtures/compatible",
        requested_revision="main",
        engine_installation_id="fake@1",
        staging_path=f"staging/job-{suffix}",
        correlation_id=f"correlation-{suffix}",
    ).id


def _advance_to_activating(registry: ModelRegistry, job_id: str) -> None:
    for state in (
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    ):
        registry.transition_download_job(job_id, state, phase=state.value)


def _activate(registry: ModelRegistry, job_id: str, model_id: str, commit: str) -> None:
    registry.activate_model(
        download_job_id=job_id,
        model_id=model_id,
        repository_id="fixtures/compatible",
        requested_revision="main",
        resolved_commit=commit,
        engine_installation_id="fake@1",
        compatibility_evidence={"adapter": "fake", "compatible": True},
        runtime_variant="int8",
        manifest={"files": [{"path": "model.onnx", "sha256": "abc"}]},
        checksum_summary={"algorithm": "sha256", "verified": 1},
        byte_size=512,
        cache_path=f"models/fixtures--compatible/{commit}",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"ready": 0},
    )


def test_download_job_round_trips_progress_and_paths(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    job_id = _new_job(registry, "one")
    registry.transition_download_job(job_id, DownloadState.VALIDATING, phase="validating")

    job = registry.transition_download_job(
        job_id,
        DownloadState.DOWNLOADING,
        phase="weights",
        bytes_downloaded=128,
        total_bytes=512,
    )

    assert job.state is DownloadState.DOWNLOADING
    assert (job.bytes_downloaded, job.total_bytes) == (128, 512)
    assert job.staging_path == "staging/job-one"
    assert registry.get_download_job(job_id) == job


def test_download_job_states_only_move_forward(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    job_id = _new_job(registry, "one")
    registry.transition_download_job(job_id, DownloadState.VALIDATING)
    registry.transition_download_job(job_id, DownloadState.DOWNLOADING)

    with pytest.raises(InvalidDownloadTransitionError):
        registry.transition_download_job(job_id, DownloadState.VALIDATING)

    registry.transition_download_job(
        job_id,
        DownloadState.FAILED,
        error={"code": "download_failed", "message": "Download failed safely."},
    )
    with pytest.raises(InvalidDownloadTransitionError):
        registry.transition_download_job(job_id, DownloadState.COMPLETED)


def test_activation_replaces_the_only_active_repository_revision(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    first = _new_job(registry, "one")
    _advance_to_activating(registry, first)
    _activate(registry, first, "model-one", "a" * 40)
    second = _new_job(registry, "two")
    _advance_to_activating(registry, second)

    _activate(registry, second, "model-two", "b" * 40)

    assert [(model.id, model.resolved_commit) for model in registry.list_models()] == [
        ("model-two", "b" * 40)
    ]
    completed = registry.get_download_job(second)
    assert completed.state is DownloadState.COMPLETED
    assert completed.target_model_id == "model-two"


def test_database_rejects_two_active_rows_for_one_repository(tmp_path: Path) -> None:
    registry, database = _registry(tmp_path)
    first = _new_job(registry, "one")
    _advance_to_activating(registry, first)
    _activate(registry, first, "model-one", "a" * 40)

    with (
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
        database.transaction() as connection,
    ):
        connection.execute(
            """
            INSERT INTO model_installations
            SELECT 'model-two', repository_id, requested_revision, ?,
                   engine_installation_id, compatibility_evidence_json,
                   runtime_variant, manifest_json, checksum_summary_json,
                   byte_size, 'models/fixtures--compatible/bbbbbbbb',
                   desired_load_state, observed_load_state, replica_summary_json,
                   last_error_json, created_at, updated_at
            FROM model_installations WHERE id = 'model-one'
            """,
            ("b" * 40,),
        )


def test_activation_rollback_preserves_prior_revision(tmp_path: Path) -> None:
    registry, database = _registry(tmp_path)
    first = _new_job(registry, "one")
    _advance_to_activating(registry, first)
    _activate(registry, first, "model-one", "a" * 40)
    second = _new_job(registry, "two")
    _advance_to_activating(registry, second)
    with database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_replacement BEFORE INSERT ON model_installations "
            "BEGIN SELECT RAISE(ABORT, 'simulated activation failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated activation failure"):
        _activate(registry, second, "model-two", "b" * 40)

    assert [(model.id, model.resolved_commit) for model in registry.list_models()] == [
        ("model-one", "a" * 40)
    ]
    pending = registry.get_download_job(second)
    assert pending.state is DownloadState.ACTIVATING
    assert pending.target_model_id is None


def test_recovery_marks_only_an_active_job_failed_with_structured_error(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    job_id = _new_job(registry, "one")
    registry.transition_download_job(job_id, DownloadState.VALIDATING)

    recovered = registry.mark_recovery_failure(
        job_id,
        {"code": "recovery_required", "message": "Interrupted during validation."},
    )

    assert recovered.state is DownloadState.FAILED
    assert recovered.error == {
        "code": "recovery_required",
        "message": "Interrupted during validation.",
    }
    with pytest.raises(InvalidDownloadTransitionError):
        registry.mark_recovery_failure(job_id, {"code": "recovery_required"})


def test_registry_rejects_invalid_json_and_non_relative_managed_paths(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    job_id = _new_job(registry, "one")
    with pytest.raises(InvalidRegistryDataError):
        registry.transition_download_job(
            job_id,
            DownloadState.FAILED,
            error={"unsafe_number": float("nan")},
        )

    _advance_to_activating(registry, job_id)
    with pytest.raises(InvalidRegistryDataError):
        registry.activate_model(
            download_job_id=job_id,
            model_id="model-one",
            repository_id="fixtures/compatible",
            requested_revision="main",
            resolved_commit="a" * 40,
            engine_installation_id="fake@1",
            compatibility_evidence={},
            runtime_variant="int8",
            manifest={},
            checksum_summary={},
            byte_size=512,
            cache_path="/tmp/model",
            desired_load_state="unloaded",
            observed_load_state="unloaded",
            replica_summary={},
        )


def test_remove_model_reports_whether_a_row_was_deleted(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    job_id = _new_job(registry, "one")
    _advance_to_activating(registry, job_id)
    _activate(registry, job_id, "model-one", "a" * 40)

    assert registry.remove_model("model-one") is True
    assert registry.remove_model("model-one") is False
    assert registry.list_models() == ()
