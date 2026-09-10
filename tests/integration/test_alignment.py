from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tts_studio.events import EventStore
from tts_studio.generation.domain import AlignmentState
from tts_studio.generation.registry import GenerationRegistry
from tts_studio.generation.service import GenerationService
from tts_studio.models.registry import DownloadState, ModelInstallation, ModelRegistry
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FAKE_WORKER_LAUNCH = WorkerLaunchSpec(
    command=("uv", "run", "--project", "workers/fake", "tts-studio-fake-worker"),
    cwd=_REPOSITORY_ROOT,
)


def _activate_model(registry: ModelRegistry) -> ModelInstallation:
    registry.upsert_engine_installation(
        engine_installation_id="fake@0.2.0",
        engine_id="fake",
        version="0.2.0",
        command=list(_FAKE_WORKER_LAUNCH.command),
        working_directory=str(_FAKE_WORKER_LAUNCH.cwd),
        environment={},
        capabilities={
            "model_lifecycle": True,
            "preset_voices": True,
            "streaming_synthesis": True,
        },
        lifecycle_state="ready",
    )
    download = registry.create_download_job(
        job_id="alignment-download",
        repository_id="fixtures/compatible",
        requested_revision=None,
        engine_installation_id="fake@0.2.0",
        staging_path="staging/alignment-download",
        correlation_id="alignment-download-correlation",
    )
    for state in (
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    ):
        registry.transition_download_job(download.id, state)
    return registry.activate_model(
        download_job_id=download.id,
        model_id="fixtures/compatible",
        repository_id="fixtures/compatible",
        requested_revision=None,
        resolved_commit="a" * 40,
        engine_installation_id="fake@0.2.0",
        compatibility_evidence={"engine_id": "fake"},
        runtime_variant="fp32",
        manifest={},
        checksum_summary={},
        byte_size=1,
        cache_path="models/model-alignment",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


@pytest.fixture
async def alignment_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[GenerationService, StorageLayout, WorkerSupervisor]]:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    model_registry = ModelRegistry(database)
    _activate_model(model_registry)
    supervisor = WorkerSupervisor(layout, startup_timeout=10)
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    service = GenerationService(
        GenerationRegistry(database),
        model_registry,
        supervisor,
        layout=layout,
        event_store=EventStore(database),
    )
    try:
        yield service, layout, supervisor
    finally:
        await service.close()
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_core_alignment_over_real_grpc_preserves_retained_artifact(
    alignment_context: tuple[GenerationService, StorageLayout, WorkerSupervisor],
) -> None:
    service, layout, _supervisor = alignment_context
    job = await service.create(
        model_id="fixtures/compatible",
        voice_id="fake-neutral",
        text="hello alignment world",
    )
    completed = await service.wait(job.id)
    assert completed.state.value == "completed"

    artifact = service.list_history()[0]
    artifact_path = service.read_artifact(artifact.id)
    original_bytes = artifact_path.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    assert original_hash == artifact.sha256

    queued = await service.request_alignment(job.id, "alignment-correlation")
    assert queued.state is AlignmentState.QUEUED
    for _ in range(100):
        alignment = service.get_alignment(job.id)
        assert alignment is not None
        if alignment.state is AlignmentState.COMPLETED:
            break
        if alignment.state is AlignmentState.FAILED:
            raise AssertionError(alignment.error)
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("alignment did not complete")

    assert alignment.result is not None
    assert alignment.result.job_id == job.id
    assert alignment.result.artifact_id == artifact.id
    assert alignment.result.sample_rate_hz == artifact.sample_rate
    assert alignment.result.total_frames == artifact.frame_count
    assert [unit.text for unit in alignment.result.units] == ["hello", "alignment", "world"]
    assert alignment.result.units[0].milliseconds(artifact.sample_rate) == (
        0.0,
        artifact.frame_count / 3 * 1_000.0 / artifact.sample_rate,
    )
    assert alignment.result.units[-1].milliseconds(artifact.sample_rate) == (
        artifact.frame_count * 2 / 3 * 1_000.0 / artifact.sample_rate,
        artifact.frame_count * 1_000.0 / artifact.sample_rate,
    )

    assert artifact_path.read_bytes() == original_bytes
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == original_hash
    assert tuple(layout.audio.glob(".alignment-*")) == ()
    assert tuple(layout.staging.glob(".alignment-*")) == ()
