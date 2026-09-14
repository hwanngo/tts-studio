import hashlib
import os
from pathlib import Path

import pytest

from tts_studio.events import EventStore
from tts_studio.generation.domain import AudioArtifact, GenerationState
from tts_studio.generation.registry import GenerationRegistry
from tts_studio.generation.service import GenerationService
from tts_studio.settings.domain import CoreSettings
from tts_studio.settings.repository import CoreSettingsRepository
from tts_studio.settings.service import (
    SettingsPatch,
    SettingsService,
    SettingsValidationError,
)
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _identity_bound_deletion_setup_available() -> bool:
    return (
        all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"))
        and os.open in getattr(os, "supports_dir_fd", ())
        and os.stat in getattr(os, "supports_dir_fd", ())
    )


def _setup(tmp_path: Path):
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = GenerationRegistry(database)
    repository = CoreSettingsRepository(database)
    generation_service = GenerationService(
        registry,
        object(),
        object(),
        layout=layout,
        event_store=EventStore(database),
    )
    return layout, registry, SettingsService(repository, generation_service)


def _allow_identity_unlink(service: SettingsService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service._artifact_owner,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )


def _artifact(
    registry: GenerationRegistry, layout: StorageLayout, job_id: str, artifact_id: str, *, size: int
) -> AudioArtifact:
    job = registry.create_job(
        job_id=job_id,
        model_id="model",
        engine_id="fake",
        voice_id="voice",
        text=job_id,
        correlation_id=job_id,
    )
    for state in (
        GenerationState.LOADING,
        GenerationState.GENERATING,
        GenerationState.FINALIZING,
        GenerationState.COMPLETED,
    ):
        registry.transition_job(job.id, state)
    path = layout.audio / f"{artifact_id}.wav"
    path.write_bytes(b"x" * size)
    return registry.create_artifact(
        job_id=job.id,
        artifact_id=artifact_id,
        path=f"audio/{artifact_id}.wav",
        byte_size=size,
        sha256=hashlib.sha256(b"x" * size).hexdigest(),
        sample_rate=48_000,
        channel_count=1,
        frame_count=1,
    )


def test_migration_defaults_retain_audio(tmp_path: Path) -> None:
    _, _, service = _setup(tmp_path)
    assert service.get_settings() == CoreSettings(True, None, None, None)
    assert service.apply_generation_defaults() is True


@pytest.mark.parametrize("field", ["artifact_max_age_days", "artifact_max_storage_bytes"])
def test_limits_accept_positive_values_and_reject_non_positive(tmp_path: Path, field: str) -> None:
    _, _, service = _setup(tmp_path)
    assert service.update_settings(SettingsPatch(**{field: 1})).__getattribute__(field) == 1
    with pytest.raises(SettingsValidationError):
        service.update_settings(SettingsPatch(**{field: 0}))
    with pytest.raises(SettingsValidationError):
        service.update_settings(SettingsPatch(**{field: -1}))


def test_patch_changes_only_supplied_fields(tmp_path: Path) -> None:
    _, _, service = _setup(tmp_path)
    service.update_settings(SettingsPatch(retain_audio_by_default=False, artifact_max_age_days=4))
    assert service.update_settings(SettingsPatch(api_token_env="TTS_TOKEN")) == CoreSettings(
        False, 4, None, "TTS_TOKEN"
    )


def test_explicit_generation_retention_remains_authoritative(tmp_path: Path) -> None:
    _, _, service = _setup(tmp_path)
    service.update_settings(SettingsPatch(retain_audio_by_default=False))
    assert service.apply_generation_defaults() is False
    assert service.apply_generation_defaults(True) is True


def test_generation_service_uses_settings_default_only_when_omitted(tmp_path: Path) -> None:
    _, _, service = _setup(tmp_path)
    service.update_settings(SettingsPatch(retain_audio_by_default=False))
    generation_service = service._artifact_owner
    generation_service.set_retention_default_provider(lambda: service.apply_generation_defaults())
    assert generation_service.resolve_retention(None) is False
    assert generation_service.resolve_retention(True) is True
    assert service.apply_generation_defaults(False) is False


def test_summary_counts_retained_artifacts_and_bytes(tmp_path: Path) -> None:
    layout, registry, service = _setup(tmp_path)
    _artifact(registry, layout, "one", "artifact-one", size=3)
    _artifact(registry, layout, "two", "artifact-two", size=7)
    service.update_settings(SettingsPatch(artifact_max_storage_bytes=20))
    summary = service.retention_summary()
    assert summary.retained_count == 2
    assert summary.retained_bytes == 10
    assert summary.max_storage_bytes == 20
    assert summary.max_age_days is None
    assert str(layout.root) not in repr(summary)


@pytest.mark.skipif(
    not _identity_bound_deletion_setup_available(),
    reason="identity-bound artifact deletion setup is unavailable on this platform",
)
def test_clear_retention_deletes_every_artifact_regardless_of_policy_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, registry, service = _setup(tmp_path)
    _allow_identity_unlink(service, monkeypatch)
    first = _artifact(registry, layout, "one", "artifact-one", size=3)
    second = _artifact(registry, layout, "two", "artifact-two", size=7)
    service.update_settings(SettingsPatch(artifact_max_storage_bytes=1000))

    result = service.clear_retention()

    assert result.deleted == 2
    assert result.skipped == 0
    assert result.failed == 0
    assert registry.list_history() == ()
    assert not (layout.root / first.path).exists()
    assert not (layout.root / second.path).exists()


def test_clear_retention_reports_false_owner_result_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, registry, service = _setup(tmp_path)
    artifact = _artifact(registry, layout, "one", "artifact-one", size=3)
    service.update_settings(SettingsPatch(artifact_max_storage_bytes=1))
    monkeypatch.setattr(service._artifact_owner, "delete_artifact", lambda _: False)
    result = service.clear_retention()
    assert result.deleted == 0
    assert result.skipped == 1
    assert result.failed == 0
    assert registry.get_job(artifact.job_id).artifact_id == artifact.id


def test_clear_retention_reports_filesystem_failures_without_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, registry, service = _setup(tmp_path)
    artifact = _artifact(registry, layout, "one", "artifact-one", size=3)
    service.update_settings(SettingsPatch(artifact_max_age_days=1))
    monkeypatch.setattr(
        service._artifact_owner,
        "_unlink_validated_artifact",
        lambda _candidate: (_ for _ in ()).throw(PermissionError("/private/path")),
    )
    result = service.clear_retention()
    assert result.deleted == 0
    assert result.failed == 1
    assert result.issues[0].artifact_id == "artifact-one"
    assert "/private/path" not in result.issues[0].message
    assert str(layout.root) not in result.issues[0].message
    assert (layout.root / artifact.path).exists()
    assert registry.get_job(artifact.job_id).artifact_id == artifact.id


@pytest.mark.skipif(
    not _identity_bound_deletion_setup_available(),
    reason="identity-bound artifact deletion setup is unavailable on this platform",
)
def test_clear_retention_deletes_all_artifacts_with_equal_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, registry, service = _setup(tmp_path)
    _allow_identity_unlink(service, monkeypatch)
    first = _artifact(registry, layout, "job-a", "artifact-a", size=3)
    second = _artifact(registry, layout, "job-b", "artifact-b", size=7)
    database = Database(layout.database_path)
    same_time = "2026-01-01T00:00:00+00:00"
    with database.transaction() as connection:
        connection.execute("UPDATE audio_artifacts SET retained_at = ?", (same_time,))

    result = service.clear_retention()

    assert result.deleted == 2
    assert result.issues == ()
    assert first.id not in {item.id for item in registry.list_history()}
    assert second.id not in {item.id for item in registry.list_history()}


def test_clear_retention_reports_database_failure_after_file_cleanup_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, registry, service = _setup(tmp_path)
    artifact = _artifact(registry, layout, "one", "artifact-one", size=3)
    service.update_settings(SettingsPatch(artifact_max_storage_bytes=1))
    _allow_identity_unlink(service, monkeypatch)

    def fail_delete(_: str) -> bool:
        raise RuntimeError("sqlite path /private/path")

    monkeypatch.setattr(registry, "delete_artifact", fail_delete)
    result = service.clear_retention()
    assert result.deleted == 0
    assert result.skipped == 0
    assert result.failed == 1
    assert result.issues[0].artifact_id == artifact.id
    assert "/private/path" not in result.issues[0].message
    assert (layout.root / artifact.path).read_bytes() == b"x" * artifact.byte_size
    assert registry.get_job(artifact.job_id).artifact_id == artifact.id


def test_clear_retention_with_no_matching_artifacts_is_successful(tmp_path: Path) -> None:
    _, _, service = _setup(tmp_path)
    result = service.clear_retention()
    assert result.deleted == result.skipped == result.failed == 0
    assert result.issues == ()
