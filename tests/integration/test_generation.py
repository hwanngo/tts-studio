from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tts_studio.events import EventStore
from tts_studio.generation.domain import GenerationState
from tts_studio.generation.registry import GenerationRegistry
from tts_studio.generation.service import GenerationService
from tts_studio.models.registry import DownloadState, ModelInstallation, ModelRegistry
from tts_studio.models.service import ModelInUseError, ModelService
from tts_studio.references.domain import ReferenceMetadata
from tts_studio.references.registry import ReferenceRecordNotFoundError
from tts_studio.references.service import ReferenceService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.generation import WorkerOperationError
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor, _pid_alive

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
        job_id="download-generation",
        repository_id="fixtures/compatible",
        requested_revision=None,
        engine_installation_id="fake@0.2.0",
        staging_path="staging/download-generation",
        correlation_id="download-correlation",
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
        cache_path="models/model-generation",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


@pytest.fixture
async def integration_context(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[GenerationService, GenerationRegistry, StorageLayout, EventStore, WorkerSupervisor]
]:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    model_registry = ModelRegistry(database)
    _activate_model(model_registry)
    generation_registry = GenerationRegistry(database)
    events = EventStore(database)
    supervisor = WorkerSupervisor(layout, startup_timeout=10)
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    service = GenerationService(
        generation_registry,
        model_registry,
        supervisor,
        layout=layout,
        event_store=events,
    )
    try:
        yield service, generation_registry, layout, events, supervisor
    finally:
        await service.close()
        await supervisor.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "retry"])
async def test_stuck_native_inference_is_contained_before_cancellation_or_retry(
    integration_context,
    mode: str,
) -> None:
    service, _registry, layout, _events, supervisor = integration_context
    await supervisor.stop_all()
    launch = WorkerLaunchSpec(
        command=(
            "uv",
            "run",
            "--project",
            "workers/fake",
            "python",
            "tests/fixtures/stuck_synthesis_worker.py",
        ),
        cwd=_REPOSITORY_ROOT,
    )
    worker = await supervisor.start("fake", launch)
    job = await service.create(model_id="fixtures/compatible", voice_id="fake-neutral", text=mode)
    async with asyncio.timeout(10):
        helper_file = layout.root / "staging" / "stuck-helper.pid"
        while not helper_file.exists():
            await asyncio.sleep(0.01)
        if mode == "cancel":
            await service.cancel(job.id)
        terminal = await service.wait(job.id)
    assert worker.process.returncode is not None
    if os.name != "nt":
        assert not _pid_alive(int(helper_file.read_text()))
    if mode == "cancel":
        assert terminal.state is GenerationState.CANCELLED
        assert not terminal.can_retry
    else:
        assert terminal.state is GenerationState.FAILED
        assert terminal.error["code"] == "provider_unavailable"
        assert terminal.can_retry
        successor = await service.retry(job.id)
        assert (await service.wait(successor.id)).state is GenerationState.COMPLETED
        assert (await service.retry(job.id)).id == successor.id
    assert not list((layout.root / "staging").glob(".generation-*.pcm"))


@pytest.mark.asyncio
async def test_ready_replicas_generate_concurrently_and_queue_excess_work(
    integration_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _registry, layout, _events, supervisor = integration_context
    model = ModelRegistry(Database(layout.database_path)).get_model("fixtures/compatible")
    await supervisor.ensure_replicas(model, 2)
    entered: set[str] = set()
    release = asyncio.Event()
    consume = service._consume_stream

    async def hold_stream(job, stream, writer):
        # Both real gRPC leases must reach synthesis before either is released.
        entered.add(job.id)
        await release.wait()
        return await consume(job, stream, writer)

    monkeypatch.setattr(service, "_consume_stream", hold_stream)
    first = await service.create(model_id=model.id, voice_id="fake-neutral", text="first")
    second_task = asyncio.create_task(
        service.create(model_id=model.id, voice_id="fake-neutral", text="second")
    )
    try:
        async with asyncio.timeout(2):
            second = await second_task
            while len(entered) < 2:
                await asyncio.sleep(0.01)
        assert service.get(first.id).state is GenerationState.GENERATING
        assert service.get(second.id).state is GenerationState.GENERATING
        third_task = asyncio.create_task(
            service.create(model_id=model.id, voice_id="fake-neutral", text="third")
        )
        await asyncio.sleep(0.02)
        assert not third_task.done(), "excess work must wait for replica capacity"
    finally:
        release.set()
        await asyncio.gather(second_task, return_exceptions=True)
    assert (await service.wait(first.id)).state is GenerationState.COMPLETED
    assert (await service.wait(second.id)).state is GenerationState.COMPLETED
    third = await asyncio.wait_for(third_task, 5)
    assert (await service.wait(third.id)).state is GenerationState.COMPLETED


@pytest.mark.asyncio
async def test_real_authenticated_fake_worker_publishes_valid_wav_and_replays_events(
    integration_context: tuple[
        GenerationService, GenerationRegistry, StorageLayout, EventStore, WorkerSupervisor
    ],
) -> None:
    service, registry, _layout, events, _ = integration_context

    queued = await service.create(
        model_id="fixtures/compatible",
        voice_id="fake-neutral",
        text="real grpc generation",
    )
    completed = await service.wait(queued.id)

    assert completed.state.value == "completed"
    artifact = service.list_history()[0]
    wav_path = service.read_artifact(artifact.id)
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getframerate() == 48_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
    assert wav_path.stat().st_size == artifact.byte_size
    assert registry.get_job(queued.id).artifact_id == artifact.id
    replay = events.read_after(0, stream_kind="generation", stream_id=queued.id)
    assert [event.event_type for event in replay.events][-1] == "generation.completed"
    assert all("pcm" not in json.dumps(event.payload).casefold() for event in replay.events)


@pytest.mark.asyncio
async def test_real_authenticated_fake_reference_generation_finalizes_wav_and_cleans_on_cancellation(
    integration_context: tuple[
        GenerationService, GenerationRegistry, StorageLayout, EventStore, WorkerSupervisor
    ],
) -> None:
    original_service, registry, layout, events, supervisor = integration_context
    references = ReferenceService(Database(layout.database_path), layout)
    service = GenerationService(
        registry,
        ModelRegistry(Database(layout.database_path)),
        supervisor,
        layout=layout,
        event_store=events,
        reference_service=references,
    )
    reference_payload = _reference_wav_bytes()
    recording = references.create_upload(
        model_id="fixtures/compatible",
        payload=reference_payload,
        transcript="contract reference transcript",
    )
    references.mark_validated(
        recording.id,
        ReferenceMetadata(container="wav", sample_rate_hz=16_000, channels=1, duration_ms=1000),
    )
    reference_path = layout.reference_staging / recording.id

    try:
        queued = await service.create(
            model_id="fixtures/compatible",
            reference_id=recording.id,
            text="real grpc reference generation",
        )
        completed = await service.wait(queued.id)
        assert completed.state is GenerationState.COMPLETED
        assert completed.voice_id is None
        assert completed.reference_id is None
        artifact = service.list_history()[0]
        with wave.open(str(service.read_artifact(artifact.id)), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48_000, 1, 2)
            assert wav.getnframes() > 0

        cancelled_recording = references.create_upload(
            model_id="fixtures/compatible",
            payload=reference_payload,
            transcript="cancel reference transcript",
        )
        references.mark_validated(
            cancelled_recording.id,
            ReferenceMetadata(container="wav", sample_rate_hz=16_000, channels=1, duration_ms=1000),
        )
        cancelled_path = layout.reference_staging / cancelled_recording.id
        cancelled = await service.create(
            model_id="fixtures/compatible",
            reference_id=cancelled_recording.id,
            text="cancel " * 1000,
        )
        await asyncio.sleep(0.01)
        await service.cancel(cancelled.id)
        cancelled_result = await service.wait(cancelled.id)
        assert cancelled_result.state is GenerationState.CANCELLED
        assert cancelled_result.artifact_id is None
        assert not cancelled_path.exists()
    finally:
        await service.close()

    assert not reference_path.exists()
    with pytest.raises(ReferenceRecordNotFoundError):
        references.get(recording.id)
    with pytest.raises(ReferenceRecordNotFoundError):
        references.get(cancelled_recording.id)
    event_json = "\n".join(
        json.dumps(event.payload, sort_keys=True) for event in events.read_after(0).events
    )
    assert "contract reference transcript" not in event_json
    assert "cancel reference transcript" not in event_json
    assert str(layout.root) not in event_json
    with sqlite3.connect(layout.database_path) as connection:
        values = connection.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        for table, _ in values:
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            serialized = repr(rows)
            assert "contract reference transcript" not in serialized
            assert "cancel reference transcript" not in serialized
            assert str(layout.root) not in serialized
            assert not any(
                isinstance(value, bytes) and reference_payload in value
                for row in rows
                for value in row
            )
    for log_path in layout.logs.iterdir():
        log_bytes = log_path.read_bytes()
        assert b"contract reference transcript" not in log_bytes
        assert b"cancel reference transcript" not in log_bytes
        assert str(layout.root).encode() not in log_bytes
        assert reference_payload not in log_bytes
    assert original_service is not service


def _reference_wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x01\x02" * 1_600)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_real_worker_cancellation_leaves_no_partial_audio_and_serializes_replica(
    integration_context: tuple[
        GenerationService, GenerationRegistry, StorageLayout, EventStore, WorkerSupervisor
    ],
) -> None:
    service, registry, layout, _, _ = integration_context

    first = await service.create(
        model_id="fixtures/compatible",
        voice_id="fake-neutral",
        text="long " * 2000,
    )
    await asyncio.sleep(0.01)
    second_task = asyncio.create_task(
        service.create(
            model_id="fixtures/compatible",
            voice_id="fake-neutral",
            text="second job",
        )
    )
    await asyncio.sleep(0.01)
    cancelled = await service.cancel(first.id)
    second = await second_task
    assert (await service.wait(cancelled.id)).state.value == "cancelled"
    assert (await service.wait(second.id)).state.value == "completed"
    assert tuple(layout.staging.iterdir()) == ()
    assert registry.get_job(first.id).artifact_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        GenerationState.QUEUED,
        GenerationState.LOADING,
        GenerationState.GENERATING,
        GenerationState.FINALIZING,
    ],
)
@pytest.mark.parametrize("operation", ["remove", "replace"])
async def test_model_removal_refuses_an_installation_pinned_by_a_queued_generation(
    integration_context: tuple[
        GenerationService, GenerationRegistry, StorageLayout, EventStore, WorkerSupervisor
    ],
    state: GenerationState,
    operation: str,
) -> None:
    _service, registry, layout, _, supervisor = integration_context
    job = registry.create_job(
        model_id="fixtures/compatible",
        engine_id="fake",
        voice_id="fake-neutral",
        text="pinned model",
        correlation_id="pinned-correlation",
    )
    model_registry = ModelRegistry(Database(layout.database_path))
    model = model_registry.get_model(job.model_id)
    (layout.root / model.cache_path).mkdir()
    async with supervisor.acquire(model) as lease:
        await lease.load_model(model)
    for next_state in [
        GenerationState.LOADING,
        GenerationState.GENERATING,
        GenerationState.FINALIZING,
    ]:
        if registry.get_job(job.id).state is state:
            break
        registry.transition_job(job.id, next_state)
    model_service = ModelService(
        model_registry,
        supervisor,
        (AdapterDescriptor("fake", 0, _FAKE_WORKER_LAUNCH),),
        layout=layout,
        generation_registry=registry,
    )

    if operation == "remove":
        with pytest.raises(ModelInUseError):
            await model_service.remove_model(job.model_id)
    else:
        download = await model_service.start_download(
            model.repository_id, correlation_id="pinned-replace"
        )
        assert (await model_service.wait_for_download(download.id)).state is DownloadState.FAILED
        assert model_service.get_download(download.id).error["code"] == "model_in_use"
    assert registry.get_job(job.id).state is state
    assert await lease.list_voices()
    assert (layout.root / model.cache_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove", "replace"])
async def test_model_retirement_unloads_the_authenticated_worker(
    integration_context, operation: str
) -> None:
    _, registry, layout, _, supervisor = integration_context
    models = ModelRegistry(Database(layout.database_path))
    model = models.get_model("fixtures/compatible")
    (layout.root / model.cache_path).mkdir()
    service = ModelService(
        models,
        supervisor,
        (AdapterDescriptor("fake", 0, _FAKE_WORKER_LAUNCH),),
        layout=layout,
        generation_registry=registry,
    )
    async with supervisor.acquire(model) as lease:
        await lease.load_model(model)
        assert await lease.list_voices()

    if operation == "remove":
        await service.remove_model(model.id)
    else:
        download = await service.start_download(
            model.repository_id,
            variant="fp32",
            correlation_id="replace-loaded",
        )
        assert (await service.wait_for_download(download.id)).state is DownloadState.COMPLETED

    # Probe the old loaded identity through the authenticated Worker interface.
    with pytest.raises(WorkerOperationError, match="loaded"):
        await lease.list_voices()
    assert not (layout.root / model.cache_path).exists()
