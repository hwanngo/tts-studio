from pathlib import Path

import pytest

from tts_studio.models.registry import DownloadState, InvalidRegistryDataError, ModelRegistry
from tts_studio.storage.db import Database


def test_model_replica_configuration_defaults_to_one_and_can_be_updated(tmp_path: Path) -> None:
    registry = ModelRegistry(Database(tmp_path / "db.sqlite3"))
    registry._database.migrate()
    _activate(registry)

    model = registry.get_model("model")
    assert model.desired_replicas == 1
    updated = registry.set_desired_replicas("model", 3)
    assert updated.desired_replicas == 3
    assert registry.get_model("model").desired_replicas == 3


@pytest.mark.parametrize("count", [0, 9, True, "2"])
def test_model_replica_configuration_rejects_unsafe_counts(tmp_path: Path, count: object) -> None:
    registry = ModelRegistry(Database(tmp_path / "db.sqlite3"))
    registry._database.migrate()
    _activate(registry)
    with pytest.raises(InvalidRegistryDataError):
        registry.set_desired_replicas("model", count)  # type: ignore[arg-type]


def _activate(registry: ModelRegistry) -> None:
    registry.upsert_engine_installation(
        engine_installation_id="fake@1", engine_id="fake", version="1",
        command=["fake"], working_directory=".", environment={}, capabilities={}, lifecycle_state="ready",
    )
    job = registry.create_download_job(
        job_id="download", repository_id="repo", engine_installation_id="fake@1",
        staging_path="staging/download", correlation_id="correlation",
    )
    for state in (DownloadState.VALIDATING, DownloadState.DOWNLOADING, DownloadState.VERIFYING, DownloadState.ACTIVATING):
        registry.transition_download_job(job.id, state)
    registry.activate_model(
        download_job_id=job.id, model_id="model", repository_id="repo", requested_revision=None,
        resolved_commit="a" * 40, engine_installation_id="fake@1", compatibility_evidence={"engine_id": "fake"},
        runtime_variant="fp32", manifest={}, checksum_summary={}, byte_size=1, cache_path="models/model",
        desired_load_state="unloaded", observed_load_state="unloaded", replica_summary={},
    )
