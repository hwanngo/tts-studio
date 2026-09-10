from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from tts_studio.events import EventStore
from tts_studio.models.activation import ModelActivation
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.models.service import ModelService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _registry(tmp_path: Path) -> tuple[ModelRegistry, StorageLayout]:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = ModelRegistry(database)
    registry.upsert_engine_installation(
        engine_installation_id="fake@1.0.0",
        engine_id="fake",
        version="1.0.0",
        command=["fake-worker"],
        working_directory="workers/fake",
        environment={},
        capabilities={"model_download": True},
        lifecycle_state="ready",
    )
    return registry, layout


def _active_model_without_directory(registry: ModelRegistry, model_id: str) -> None:
    job = registry.create_download_job(
        job_id=f"job-{model_id}",
        repository_id="fixtures/compatible",
        requested_revision="main",
        engine_installation_id="fake@1.0.0",
        staging_path=f"staging/job-{model_id}",
        correlation_id="correlation-active",
    )
    for state in (
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    ):
        registry.transition_download_job(job.id, state)
    registry.activate_model(
        download_job_id=job.id,
        model_id=model_id,
        repository_id="fixtures/compatible",
        requested_revision="main",
        resolved_commit="a" * 40,
        engine_installation_id="fake@1.0.0",
        compatibility_evidence={"engine_id": "fake"},
        runtime_variant="int8",
        manifest={"files": []},
        checksum_summary={"algorithm": "sha256"},
        byte_size=0,
        cache_path=f"models/{model_id}",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


def test_recovery_fails_interrupted_jobs_and_removes_only_managed_partial_data(
    tmp_path: Path,
) -> None:
    registry, layout = _registry(tmp_path)
    job = registry.create_download_job(
        job_id="interrupted-job",
        repository_id="fixtures/compatible",
        requested_revision="main",
        engine_installation_id="fake@1.0.0",
        staging_path="staging/interrupted-job",
        correlation_id="correlation-one",
    )
    registry.transition_download_job(job.id, DownloadState.VALIDATING)
    registry.transition_download_job(job.id, DownloadState.DOWNLOADING)
    partial = layout.staging / job.id
    partial.mkdir()
    (partial / "partial.bin").write_bytes(b"partial")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.bin").write_bytes(b"keep")

    ModelActivation(layout).recover(registry)

    recovered = registry.get_download_job(job.id)
    assert recovered.state is DownloadState.FAILED
    assert recovered.error == {
        "code": "recovery_required",
        "message": "An interrupted model download was cleaned up.",
        "retryable": True,
    }
    assert not partial.exists()
    assert (unrelated / "keep.bin").read_bytes() == b"keep"


def test_service_recovery_fails_queued_jobs_and_publishes_retryable_event(
    tmp_path: Path,
) -> None:
    registry, layout = _registry(tmp_path)
    job = registry.create_download_job(
        job_id="queued-job",
        repository_id="fixtures/compatible",
        requested_revision="main",
        engine_installation_id="fake@1.0.0",
        staging_path="staging/queued-job",
        correlation_id="correlation-queued",
    )
    database = Database(layout.database_path)
    events = EventStore(database)
    service = ModelService(registry, object(), (), layout=layout, event_store=events)

    service.recover_downloads()

    recovered = registry.get_download_job(job.id)
    assert recovered.state is DownloadState.FAILED
    assert recovered.error == {
        "code": "recovery_required",
        "message": "An interrupted model download was cleaned up.",
        "retryable": True,
    }
    published = events.read_after(0).events
    assert len(published) == 1
    assert published[0].event_type == "download.failed"
    assert published[0].payload == {
        "job_id": job.id,
        "phase": "queued",
        "bytes_downloaded": 0,
        "message": "An interrupted model download requires recovery.",
        "error_code": "recovery_required",
        "retryable": True,
    }


def test_recovery_is_idempotent_and_removes_unreferenced_staging(tmp_path: Path) -> None:
    registry, layout = _registry(tmp_path)
    orphan = layout.staging / "orphan-job"
    orphan.mkdir()
    (orphan / "partial.bin").write_bytes(b"partial")
    activation = ModelActivation(layout)

    activation.recover(registry)
    activation.recover(registry)

    assert tuple(layout.staging.iterdir()) == ()


def test_recovery_removes_abandoned_core_promotion_directories(
    tmp_path: Path,
) -> None:
    registry, layout = _registry(tmp_path)
    abandoned = layout.models / f"promotion--{'a' * 32}"
    (abandoned / "weights").mkdir(parents=True)
    (abandoned / "weights" / "model.bin").write_bytes(b"partial")

    ModelActivation(layout).recover(registry)

    assert not abandoned.exists()
    assert tuple(layout.models.iterdir()) == ()


def test_recovery_removes_abandoned_vieneu_scratch_without_following_redirects(
    tmp_path: Path,
) -> None:
    registry, layout = _registry(tmp_path)
    scratch = layout.root / ".vieneu-downloads"
    abandoned = scratch / ".repository-download-model-123"
    abandoned.mkdir(parents=True)
    (abandoned / "partial.bin").write_bytes(b"partial")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.bin"
    protected.write_bytes(b"keep")
    try:
        (scratch / "redirected").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    ModelActivation(layout).recover(registry)

    assert not scratch.exists()
    assert protected.read_bytes() == b"keep"


def test_recovery_preserves_a_replaced_vieneu_scratch_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, layout = _registry(tmp_path)
    scratch = layout.root / ".vieneu-downloads"
    abandoned = scratch / ".repository-download-model-123"
    abandoned.mkdir(parents=True)
    (abandoned / "partial.bin").write_bytes(b"old")
    original_lstat = Path.lstat
    original_rmtree = shutil.rmtree
    replaced = False

    def replace_after_snapshot(path: Path):
        nonlocal replaced
        metadata = original_lstat(path)
        if path == scratch and not replaced:
            replaced = True
            original_rmtree(path)
            path.mkdir()
            (path / "replacement.bin").write_bytes(b"keep")
        return metadata

    monkeypatch.setattr(Path, "lstat", replace_after_snapshot)

    ModelActivation(layout).recover(registry)

    assert replaced
    assert (scratch / "replacement.bin").read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["file", "symlink", "fifo"])
def test_recovery_removes_stale_non_directory_vieneu_scratch_entries_without_following_them(
    tmp_path: Path, kind: str
) -> None:
    _, layout = _registry(tmp_path)
    scratch = layout.root / ".vieneu-downloads-stale"
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.bin"
    protected.write_bytes(b"keep")

    if kind == "file":
        scratch.write_bytes(b"stale")
    elif kind == "symlink":
        try:
            scratch.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks are unavailable: {error}")
    else:
        try:
            os.mkfifo(scratch)
        except (AttributeError, OSError) as error:
            pytest.skip(f"named pipes are unavailable: {error}")

    ModelActivation(layout).recover(ModelRegistry(Database(layout.database_path)))

    assert not scratch.exists()
    assert not scratch.is_symlink()
    assert protected.read_bytes() == b"keep"


def test_recovery_preserves_a_regular_scratch_replacement_after_identity_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, layout = _registry(tmp_path)
    scratch = layout.root / ".vieneu-downloads-replaced"
    scratch.write_bytes(b"old")
    original_lstat = Path.lstat
    original_unlink = Path.unlink
    lstat_calls = 0

    def replace_after_identity_snapshot(path: Path):
        nonlocal lstat_calls
        metadata = original_lstat(path)
        if path == scratch:
            lstat_calls += 1
            if lstat_calls == 2:
                original_unlink(path)
                path.write_bytes(b"replacement")
        return metadata

    monkeypatch.setattr(Path, "lstat", replace_after_identity_snapshot)

    ModelActivation(layout).recover(ModelRegistry(Database(layout.database_path)))

    assert (scratch).read_bytes() == b"replacement"


def test_recovery_unlinks_a_redirected_staging_entry_without_touching_target(
    tmp_path: Path,
) -> None:
    registry, layout = _registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.bin"
    protected.write_bytes(b"keep")
    (layout.staging / "orphan-link").symlink_to(outside, target_is_directory=True)

    ModelActivation(layout).recover(registry)

    assert not (layout.staging / "orphan-link").exists()
    assert protected.read_bytes() == b"keep"


def test_recovery_never_promotes_a_retirement_named_symlink_for_an_active_model(
    tmp_path: Path,
) -> None:
    registry, layout = _registry(tmp_path)
    model_id = "active-model"
    _active_model_without_directory(registry, model_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.bin"
    protected.write_bytes(b"keep")
    retirement = layout.staging / f"retired--{model_id}--{'a' * 32}"
    try:
        retirement.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    ModelActivation(layout).recover(registry)

    expected = layout.models / model_id
    assert not expected.exists()
    assert not expected.is_symlink()
    assert not retirement.exists()
    assert protected.read_bytes() == b"keep"


@dataclass(frozen=True)
class _ReparseMetadata:
    st_mode: int
    st_dev: int
    st_ino: int
    st_file_attributes: int


def test_recovery_never_promotes_a_retirement_named_reparse_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, layout = _registry(tmp_path)
    model_id = "active-model"
    _active_model_without_directory(registry, model_id)
    retirement = layout.staging / f"retired--{model_id}--{'b' * 32}"
    retirement.mkdir()
    original_lstat = Path.lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def reparse_lstat(path: Path) -> object:
        metadata = original_lstat(path)
        if path == retirement:
            return _ReparseMetadata(
                st_mode=metadata.st_mode,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_file_attributes=reparse_flag,
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    ModelActivation(layout).recover(registry)

    assert not (layout.models / model_id).exists()
    assert not retirement.exists()
