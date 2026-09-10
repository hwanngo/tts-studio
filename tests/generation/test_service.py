from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tts_studio_protocol.engine.v1 import engine_pb2

import tts_studio.generation.service as service_module
import tts_studio.storage.identity as identity_module
from tts_studio.events import EventStore
from tts_studio.generation.domain import GenerationJob, GenerationState, SynthesisOptions
from tts_studio.generation.registry import GenerationRegistry
from tts_studio.generation.service import (
    GenerationArtifactDeletionError,
    GenerationArtifactNotFoundError,
    GenerationCapabilityError,
    GenerationRequestError,
    GenerationService,
    GenerationVoiceNotFoundError,
)
from tts_studio.models.registry import ModelInstallation
from tts_studio.references.domain import ReferenceMetadata
from tts_studio.references.registry import ReferenceRecordNotFoundError
from tts_studio.references.service import ReferenceService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.generation import (
    GrpcWorkerLease,
    WorkerCapabilities,
    WorkerOperationError,
)
from tts_studio.workers.process import WorkerProcess


def _identity_bound_unlink_available() -> bool:
    try:
        libc = identity_module.ctypes.CDLL(None, use_errno=True)
        return libc.funlinkat is not None
    except (AttributeError, OSError, TypeError):
        return False


def _model() -> ModelInstallation:
    return ModelInstallation(
        id="model-one",
        repository_id="fixtures/compatible",
        requested_revision=None,
        resolved_commit="a" * 40,
        engine_installation_id="fake@0.2.0",
        compatibility_evidence={"engine_id": "fake"},
        runtime_variant="fp32",
        manifest={},
        checksum_summary={},
        byte_size=1,
        cache_path="models/model-one",
        desired_load_state="loaded",
        observed_load_state="unloaded",
        replica_summary={},
        last_error=None,
        created_at="now",
        updated_at="now",
    )


class _Models:
    def __init__(self, model: ModelInstallation) -> None:
        self.model = model

    def get_model(self, model_id: str) -> ModelInstallation:
        if model_id != self.model.id:
            raise LookupError
        return self.model


class _Stream:
    def __init__(
        self,
        events: list[engine_pb2.SynthesisEvent],
        *,
        block: bool = False,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        self._events = events
        self._index = 0
        self._block = block
        self._start_gate = start_gate
        self._released = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> engine_pb2.SynthesisEvent:
        if self._start_gate is not None and self._index == 0:
            await self._start_gate.wait()
        if self._block and self._index >= len(self._events):
            await self._released.wait()
        if self.closed:
            raise asyncio.CancelledError
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event

    async def aclose(self) -> None:
        self.closed = True
        self._released.set()


_QUEUE_SHUTDOWN = getattr(asyncio, "QueueShutDown", None)


class _ShutdownQueue(asyncio.Queue[bytes | None]):
    def put_nowait(self, item: bytes | None) -> None:
        del item
        if _QUEUE_SHUTDOWN is None:
            raise RuntimeError("QueueShutDown is unavailable")
        raise _QUEUE_SHUTDOWN


class _Lease:
    capabilities = WorkerCapabilities(
        engine_id="fake",
        engine_version="0.2.0",
        supported=frozenset({"preset_voices", "streaming_synthesis"}),
        max_concurrency=1,
    )

    def __init__(
        self,
        events: list[engine_pb2.SynthesisEvent],
        *,
        block: bool = False,
        capabilities: WorkerCapabilities | None = None,
        drop_preset_capability_on_load: bool = False,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        self.events = events
        self.block = block
        self.start_gate = start_gate
        self.drop_preset_capability_on_load = drop_preset_capability_on_load
        if capabilities is not None:
            self.capabilities = capabilities
        self.streams: list[_Stream] = []
        self.loaded: list[str] = []
        self.requests: list[engine_pb2.SynthesizeRequest] = []

    async def load_model(self, model: ModelInstallation) -> None:
        self.loaded.append(model.id)
        if self.drop_preset_capability_on_load:
            self.capabilities = WorkerCapabilities(
                engine_id=self.capabilities.engine_id,
                engine_version=self.capabilities.engine_version,
                supported=self.capabilities.supported - {"preset_voices"},
                max_concurrency=self.capabilities.max_concurrency,
            )

    async def unload_model(self, model: ModelInstallation) -> None:
        del model

    async def list_voices(self) -> tuple[engine_pb2.PresetVoice, ...]:
        return (engine_pb2.PresetVoice(id="fake-neutral", label="Fake Neutral"),)

    def synthesize(self, request: engine_pb2.SynthesizeRequest) -> _Stream:
        self.requests.append(request)
        stream = _Stream(self.events, block=self.block, start_gate=self.start_gate)
        self.streams.append(stream)
        return stream


class _StrictRuntimeCall:
    def __init__(self, events: list[engine_pb2.SynthesisEvent]) -> None:
        self._events = events

    def __aiter__(self) -> AsyncIterator[engine_pb2.SynthesisEvent]:
        async def events() -> AsyncIterator[engine_pb2.SynthesisEvent]:
            for event in self._events:
                yield event

        return events()

    def cancel(self) -> None:
        return None


class _StrictRuntimeStub:
    """Production RPC seam matching a Worker that rejects duplicate loads."""

    def __init__(self) -> None:
        self.loaded = False
        self.load_calls = 0
        self.unload_calls = 0

    async def LoadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del metadata, timeout
        self.load_calls += 1
        if self.loaded:
            return engine_pb2.LoadModelResponse(
                error=engine_pb2.WorkerError(
                    code="model_already_loaded", message="model already loaded"
                )
            )
        self.loaded = True
        return engine_pb2.LoadModelResponse(loaded=True)

    async def UnloadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del request, metadata, timeout
        self.unload_calls += 1
        if not self.loaded:
            return engine_pb2.UnloadModelResponse(
                error=engine_pb2.WorkerError(
                    code="model_not_loaded", message="model is not loaded"
                )
            )
        self.loaded = False
        return engine_pb2.UnloadModelResponse(unloaded=True)

    async def ListVoices(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del request, metadata, timeout
        return engine_pb2.ListVoicesResponse(
            voices=[engine_pb2.PresetVoice(id="fake-neutral", label="Fake Neutral")]
        )

    def Synthesize(self, request: Any, *, metadata: Any, timeout: float) -> _StrictRuntimeCall:
        del request, metadata, timeout
        return _StrictRuntimeCall(_events())


class _LazyReferenceRuntimeStub(_StrictRuntimeStub):
    async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del request, metadata, timeout
        return engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
            engine_id="fake",
            engine_version="0.2.0",
            capabilities=[
                engine_pb2.Capability(name="preset_voices", supported=True),
                engine_pb2.Capability(name="streaming_synthesis", supported=True),
                engine_pb2.Capability(name="reference_cloning", supported=True),
            ],
            max_concurrency=1,
        )


class _FreshGrpcPool:
    def __init__(self, worker: WorkerProcess) -> None:
        self.worker = worker

    @asynccontextmanager
    async def acquire(self, model: ModelInstallation) -> AsyncIterator[GrpcWorkerLease]:
        del model
        yield GrpcWorkerLease(self.worker)


class _UnloadFailsOnceStrictStub(_StrictRuntimeStub):
    def __init__(self) -> None:
        super().__init__()
        self.fail_unload_call = 2

    async def UnloadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        if self.unload_calls + 1 == self.fail_unload_call:
            self.unload_calls += 1
            del request, metadata, timeout
            return engine_pb2.UnloadModelResponse(
                error=engine_pb2.WorkerError(
                    code="model_unload_failed",
                    message="runtime cleanup details must not escape",
                    retryable=True,
                )
            )
        return await super().UnloadModel(request, metadata=metadata, timeout=timeout)


class _Pool:
    def __init__(self, lease: _Lease) -> None:
        self.lease = lease
        self.entries = 0

    @asynccontextmanager
    async def acquire(self, model: ModelInstallation) -> AsyncIterator[_Lease]:
        del model
        self.entries += 1
        yield self.lease


class _UnloadFailsAfterPreflight(_Lease):
    def __init__(self, events: list[engine_pb2.SynthesisEvent]) -> None:
        super().__init__(events)
        self.unload_calls = 0

    async def unload_model(self, model: ModelInstallation) -> None:
        del model
        self.unload_calls += 1
        if self.unload_calls > 1:
            raise RuntimeError("model cleanup failed")


class _LoadFailsWithoutModel(_Lease):
    def __init__(self) -> None:
        super().__init__([])
        self.unload_calls = 0

    async def load_model(self, model: ModelInstallation) -> None:
        del model
        raise WorkerOperationError(
            engine_pb2.WorkerError(
                code="model_load_failed",
                message="model failed to load",
                retryable=True,
            )
        )

    async def unload_model(self, model: ModelInstallation) -> None:
        del model
        self.unload_calls += 1
        raise WorkerOperationError(
            engine_pb2.WorkerError(code="model_not_loaded", message="model is not loaded")
        )


class _SynthesisFailsAndUnloadFails(_Lease):
    def __init__(self) -> None:
        super().__init__([
            engine_pb2.SynthesisEvent(
                error=engine_pb2.WorkerError(
                    code="synthesis_failed", message="synthesis failed", retryable=True
                )
            )
        ])
        self.unload_calls = 0

    async def unload_model(self, model: ModelInstallation) -> None:
        del model
        self.unload_calls += 1
        raise WorkerOperationError(
            engine_pb2.WorkerError(code="unload_failed", message="unload failed", retryable=True)
        )


class _CloseAndUnloadFails(_Lease):
    def __init__(self) -> None:
        super().__init__(_events())
        self.unload_calls = 0

    def synthesize(self, request: engine_pb2.SynthesizeRequest) -> _Stream:
        del request
        stream = _Stream(_events())
        original_close = stream.aclose

        async def close_with_failure() -> None:
            await original_close()
            raise WorkerOperationError(
                engine_pb2.WorkerError(
                    code="stream_close_failed", message="stream close failed", retryable=True
                )
            )

        stream.aclose = close_with_failure  # type: ignore[method-assign]
        return stream

    async def unload_model(self, model: ModelInstallation) -> None:
        del model
        self.unload_calls += 1
        raise WorkerOperationError(
            engine_pb2.WorkerError(code="unload_failed", message="unload failed", retryable=True)
        )


class _WorkerErrorAfterPreflight(_Lease):
    def __init__(self, events: list[engine_pb2.SynthesisEvent]) -> None:
        super().__init__(events)
        self.load_calls = 0

    async def load_model(self, model: ModelInstallation) -> None:
        self.load_calls += 1
        if self.load_calls > 1:
            raise WorkerOperationError(
                engine_pb2.WorkerError(
                    code="model_load_failed",
                    message="raw Worker traceback and secret",
                    retryable=False,
                )
            )
        await super().load_model(model)


def _registry(tmp_path: Path) -> tuple[GenerationRegistry, StorageLayout, EventStore]:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return GenerationRegistry(database), layout, EventStore(database)


def _events(*, sample_rate: int = 48_000) -> list[engine_pb2.SynthesisEvent]:
    return [
        engine_pb2.SynthesisEvent(
            header=engine_pb2.AudioHeader(
                sample_rate_hz=sample_rate,
                channels=1,
                sample_format=engine_pb2.S16LE,
            )
        ),
        engine_pb2.SynthesisEvent(chunk=engine_pb2.PcmChunk(sequence=0, pcm=b"\x00\x00" * 4)),
        engine_pb2.SynthesisEvent(
            progress=engine_pb2.SynthesisProgress(message="halfway", duration_frames=4)
        ),
        engine_pb2.SynthesisEvent(
            result=engine_pb2.SynthesisResult(total_frames=4, duration_ms=0)
        ),
    ]


def _events_with_chunks(count: int) -> list[engine_pb2.SynthesisEvent]:
    return [
        engine_pb2.SynthesisEvent(
            header=engine_pb2.AudioHeader(
                sample_rate_hz=48_000,
                channels=1,
                sample_format=engine_pb2.S16LE,
            )
        ),
        *[
            engine_pb2.SynthesisEvent(
                chunk=engine_pb2.PcmChunk(sequence=sequence, pcm=b"\x00\x00" * 4)
            )
            for sequence in range(count)
        ],
        engine_pb2.SynthesisEvent(
            result=engine_pb2.SynthesisResult(total_frames=count * 4, duration_ms=0)
        ),
    ]


def _events_with_empty_chunk() -> list[engine_pb2.SynthesisEvent]:
    return [
        engine_pb2.SynthesisEvent(
            header=engine_pb2.AudioHeader(
                sample_rate_hz=48_000,
                channels=1,
                sample_format=engine_pb2.S16LE,
            )
        ),
        engine_pb2.SynthesisEvent(chunk=engine_pb2.PcmChunk(sequence=0, pcm=b"")),
        engine_pb2.SynthesisEvent(chunk=engine_pb2.PcmChunk(sequence=1, pcm=b"\x00\x00" * 4)),
        engine_pb2.SynthesisEvent(result=engine_pb2.SynthesisResult(total_frames=4)),
    ]


def _service(tmp_path: Path, lease: _Lease) -> tuple[GenerationService, GenerationRegistry, StorageLayout, EventStore]:
    registry, layout, events = _registry(tmp_path)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
    )
    return service, registry, layout, events


def _reference_service(layout: StorageLayout, registry: GenerationRegistry) -> ReferenceService:
    del registry
    return ReferenceService(Database(layout.database_path), layout)


def _validated_reference(reference_service: ReferenceService, *, model_id: str = "model-one") -> str:
    recording = reference_service.create_upload(
        model_id=model_id,
        payload=b"reference bytes",
        transcript="spoken transcript",
    )
    reference_service.mark_validated(
        recording.id,
        ReferenceMetadata(container="wav", sample_rate_hz=16_000, channels=1, duration_ms=1000),
    )
    return recording.id


def _reference_capabilities() -> WorkerCapabilities:
    return WorkerCapabilities(
        engine_id="fake",
        engine_version="0.2.0",
        supported=frozenset({"streaming_synthesis", "reference_cloning"}),
        max_concurrency=1,
    )


@pytest.mark.asyncio
async def test_post_refresh_option_capability_failure_is_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = _Lease(
        _events(),
        capabilities=WorkerCapabilities(
            engine_id="fake",
            engine_version="0.2.0",
            supported=frozenset({"streaming_synthesis", "preset_voices", "speed"}),
            max_concurrency=1,
        ),
    )
    service, _registry_value, _layout, _events_value = _service(tmp_path, lease)
    calls = 0
    original = service_module._require_option_capabilities

    def refreshed_capabilities(current_lease: Any, options: SynthesisOptions | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GenerationCapabilityError("the Worker does not support the speed option")
        original(current_lease, options)

    monkeypatch.setattr(service_module, "_require_option_capabilities", refreshed_capabilities)
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="stable", options=SynthesisOptions(speed=1.2))
    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert failed.error == {"code": "capability_unsupported", "message": "the Worker does not support the speed option", "retryable": False}
    await service.close()


@pytest.mark.asyncio
async def test_failed_generation_is_not_marked_retryable_without_native_retry_operation(tmp_path: Path) -> None:
    events = [
        engine_pb2.SynthesisEvent(
            error=engine_pb2.WorkerError(
                code="provider_unavailable", message="upstream unavailable", retryable=True
            )
        )
    ]
    service, _registry_value, _layout, _events_value = _service(tmp_path, _Lease(events))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="retry safety")
    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert failed.error == {
        "code": "provider_unavailable",
        "message": "The engine Worker failed during synthesis.",
        "retryable": False,
    }
    await service.close()


@pytest.mark.asyncio
async def test_preview_preserves_load_failure_and_does_not_unload_unloaded_model(tmp_path: Path) -> None:
    lease = _LoadFailsWithoutModel()
    service, _registry_value, _layout, _events_value = _service(tmp_path, lease)

    with pytest.raises(WorkerOperationError, match="model failed to load") as error:
        await service.preview(model_id="model-one", voice_id="fake-neutral", text="preview")

    assert error.value.code == "model_load_failed"
    assert lease.unload_calls == 0


@pytest.mark.asyncio
async def test_preview_preserves_synthesis_failure_when_unload_also_fails(tmp_path: Path) -> None:
    lease = _SynthesisFailsAndUnloadFails()
    service, _registry_value, _layout, _events_value = _service(tmp_path, lease)

    with pytest.raises(WorkerOperationError, match="synthesis failed") as error:
        await service.preview(model_id="model-one", voice_id="fake-neutral", text="preview")

    assert error.value.code == "synthesis_failed"
    assert lease.unload_calls == 1


@pytest.mark.asyncio
async def test_preview_preserves_stream_close_failure_when_unload_also_fails(tmp_path: Path) -> None:
    lease = _CloseAndUnloadFails()
    service, _registry_value, _layout, _events_value = _service(tmp_path, lease)

    with pytest.raises(WorkerOperationError, match="stream close failed") as error:
        await service.preview(model_id="model-one", voice_id="fake-neutral", text="preview")

    assert error.value.code == "stream_close_failed"
    assert lease.unload_calls == 1


@pytest.mark.asyncio
async def test_completed_nonretained_job_pcm_subscription_replays_audio(tmp_path: Path) -> None:
    service, _registry, _layout, _events_store = _service(tmp_path, _Lease(_events()))
    queued = await service.create(
        model_id="model-one", voice_id="fake-neutral", text="done", retain_artifact=False
    )
    completed = await asyncio.wait_for(service.wait(queued.id), timeout=1)

    subscription = service.subscribe_pcm(completed.id)
    assert [chunk async for chunk in subscription] == [b"\x00\x00" * 4]

    await service.close()


@pytest.mark.asyncio
async def test_nonretained_pcm_replay_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "_EPHEMERAL_REPLAY_MAX_BYTES", 4)
    service, _registry, _layout, _events_store = _service(tmp_path, _Lease(_events_with_chunks(2)))
    queued = await service.create(
        model_id="model-one", voice_id="fake-neutral", text="large", retain_artifact=False
    )
    await asyncio.wait_for(service.wait(queued.id), timeout=1)

    assert queued.id not in service._pcm_replay
    subscription = service.subscribe_pcm(queued.id)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(subscription.__anext__(), timeout=1)
    await service.close()


@pytest.mark.asyncio
async def test_completed_retained_job_pcm_subscription_terminates_without_waiting(tmp_path: Path) -> None:
    service, _registry, _layout, _events_store = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="done")
    completed = await asyncio.wait_for(service.wait(queued.id), timeout=1)

    subscription = service.subscribe_pcm(completed.id)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(subscription.__anext__(), timeout=1)

    await service.close()


@pytest.mark.asyncio
async def test_pcm_subscriber_drains_buffered_audio_before_terminal_signal(tmp_path: Path) -> None:
    service, _registry, _layout, _events_store = _service(tmp_path, _Lease(_events_with_chunks(3)))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="drain")
    subscription = service.subscribe_pcm(queued.id)
    first_chunk = asyncio.create_task(subscription.__anext__())
    while queued.id not in service._pcm_subscribers:
        await asyncio.sleep(0)

    completed = await asyncio.wait_for(service.wait(queued.id), timeout=1)
    chunks = [await first_chunk]
    chunks.extend([chunk async for chunk in subscription])

    assert completed.state is GenerationState.COMPLETED
    assert chunks == [b"\x00\x00" * 4] * 3
    await service.close()


@pytest.mark.asyncio
async def test_stalled_pcm_subscriber_does_not_block_generation_or_next_job(tmp_path: Path) -> None:
    gate = asyncio.Event()
    lease = _Lease(_events_with_chunks(9), start_gate=gate)
    service, _registry, _layout, _events_store = _service(tmp_path, lease)

    first = await service.create(model_id="model-one", voice_id="fake-neutral", text="first")
    subscriber = service.subscribe_pcm(first.id)
    first_chunk = asyncio.create_task(subscriber.__anext__())
    while first.id not in service._pcm_subscribers:
        await asyncio.sleep(0)
    gate.set()
    assert await first_chunk == b"\x00\x00" * 4

    second_create = asyncio.create_task(
        service.create(model_id="model-one", voice_id="fake-neutral", text="second")
    )
    completed = await asyncio.wait_for(service.wait(first.id), timeout=1)
    second = await asyncio.wait_for(second_create, timeout=1)
    second_completed = await asyncio.wait_for(service.wait(second.id), timeout=1)

    assert completed.state is GenerationState.COMPLETED
    assert second_completed.state is GenerationState.COMPLETED
    await subscriber.aclose()
    await service.close()


@pytest.mark.asyncio
async def test_shutdown_pcm_subscriber_is_removed_without_failing_generation(tmp_path: Path) -> None:
    if _QUEUE_SHUTDOWN is None:
        pytest.skip("asyncio.QueueShutDown is unavailable")
    service, _registry, _layout, _events_store = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="closed")
    subscriber_queue = _ShutdownQueue()
    service._pcm_subscribers[queued.id] = {subscriber_queue}

    completed = await asyncio.wait_for(service.wait(queued.id), timeout=1)

    assert completed.state is GenerationState.COMPLETED
    assert queued.id not in service._pcm_subscribers
    await service.close()


@pytest.mark.asyncio
async def test_full_pcm_subscriber_queue_does_not_block_terminal_cleanup(tmp_path: Path) -> None:
    service, _registry, _layout, _events_store = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="cleanup")
    subscriber_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
    for index in range(8):
        subscriber_queue.put_nowait(bytes([index]))
    service._pcm_subscribers[queued.id] = {subscriber_queue}

    completed = await asyncio.wait_for(service.wait(queued.id), timeout=1)

    assert completed.state is GenerationState.COMPLETED
    assert subscriber_queue.full()
    assert subscriber_queue in service._pcm_terminated[queued.id]
    await service.close()


@pytest.mark.asyncio
async def test_create_drives_durable_state_machine_and_publishes_artifact_events(tmp_path: Path) -> None:
    service, registry, layout, events = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    assert completed.model_id == "model-one"
    assert completed.voice_id == "fake-neutral"
    assert completed.text == "hello"
    assert completed.artifact_id is not None
    artifact = service.list_history()[0]
    assert artifact.job_id == queued.id
    assert (layout.root / artifact.path).is_file()
    assert [event.event_type for event in events.read_after(0).events] == [
        "generation.queued",
        "generation.loading",
        "generation.progress",
        "generation.progress",
        "generation.finalizing",
        "generation.completed",
    ]
    assert registry.get_job(queued.id).state is GenerationState.COMPLETED


@pytest.mark.asyncio
async def test_preset_generation_unloads_preflight_model_before_execution_load(
    tmp_path: Path,
) -> None:
    stub = _StrictRuntimeStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities(
                engine_id="fake",
                engine_version="0.2.0",
                supported=frozenset({"preset_voices", "streaming_synthesis"}),
                max_concurrency=1,
            ),
        ),
    )
    lease = GrpcWorkerLease(worker)
    service, _registry, _layout, _events_store = _service(tmp_path, lease)

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    assert stub.load_calls == 2
    assert stub.unload_calls == 2
    assert stub.loaded is False


@pytest.mark.asyncio
async def test_reference_generation_refreshes_lazy_capability_before_preflight_check(
    tmp_path: Path,
) -> None:
    stub = _LazyReferenceRuntimeStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            loaded_model_id=None,
            capabilities=WorkerCapabilities(
                engine_id="fake",
                engine_version="0.2.0",
                supported=frozenset({"streaming_synthesis"}),
                max_concurrency=1,
            ),
        ),
    )
    lease = GrpcWorkerLease(worker)
    registry, layout, events = _registry(tmp_path)
    references = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=references,
    )
    reference_id = _validated_reference(references)

    queued = await service.create(model_id="model-one", reference_id=reference_id, text="hello")
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    assert stub.load_calls == 2
    assert stub.unload_calls == 2
    assert stub.loaded is False


@pytest.mark.asyncio
async def test_failed_unload_keeps_worker_runtime_owned_for_next_generation(
    tmp_path: Path,
) -> None:
    stub = _UnloadFailsOnceStrictStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            loaded_model_id=None,
            capabilities=WorkerCapabilities(
                engine_id="fake",
                engine_version="0.2.0",
                supported=frozenset({"preset_voices", "streaming_synthesis"}),
                max_concurrency=1,
            ),
        ),
    )
    registry, layout, events = _registry(tmp_path)
    service = GenerationService(
        registry,
        _Models(_model()),
        _FreshGrpcPool(worker),
        layout=layout,
        event_store=events,
    )

    first = await service.create(model_id="model-one", voice_id="fake-neutral", text="first")
    assert (await service.wait(first.id)).state is GenerationState.COMPLETED

    second = await service.create(model_id="model-one", voice_id="fake-neutral", text="second")
    assert (await service.wait(second.id)).state is GenerationState.COMPLETED
    assert stub.loaded is False


@pytest.mark.asyncio
async def test_durable_worker_failure_redacts_raw_message_but_preserves_code_and_retryability(
    tmp_path: Path,
) -> None:
    service, registry, _layout, _events_store = _service(
        tmp_path, _WorkerErrorAfterPreflight(_events())
    )

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")
    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert failed.error == {
        "code": "model_load_failed",
        "message": "The engine Worker failed during generation.",
        "retryable": False,
    }
    assert registry.get_job(queued.id).error == failed.error
    assert "raw Worker traceback" not in str(failed.error)


@pytest.mark.asyncio
async def test_voice_listing_unloads_temporary_model_ownership(
    tmp_path: Path,
) -> None:
    stub = _StrictRuntimeStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities(
                engine_id="fake",
                engine_version="0.2.0",
                supported=frozenset({"preset_voices", "streaming_synthesis"}),
                max_concurrency=1,
            ),
        ),
    )
    lease = GrpcWorkerLease(worker)
    service, _registry, _layout, _events_store = _service(tmp_path, lease)

    voices = await service.list_voices("model-one")

    assert voices[0].id == "fake-neutral"
    assert stub.unload_calls == 1
    assert stub.loaded is False


@pytest.mark.asyncio
async def test_unload_failure_does_not_rollback_completed_artifact(
    tmp_path: Path,
) -> None:
    lease = _UnloadFailsAfterPreflight(_events())
    service, registry, layout, _events_store = _service(tmp_path, lease)

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    assert completed.artifact_id is not None
    assert registry.get_job(queued.id).state is GenerationState.COMPLETED
    assert service.list_history()
    assert tuple(layout.audio.glob("generation-*.wav"))


def test_finalizing_artifacts_are_not_visible_through_history_or_reads(tmp_path: Path) -> None:
    service, registry, layout, _events_store = _service(tmp_path, _Lease(_events()))
    job = registry.create_job(
        job_id="finalizing-job",
        model_id="model-one",
        engine_id="fake",
        voice_id="fake-neutral",
        text="finalizing",
        correlation_id="finalizing-correlation",
    )
    registry.transition_job(job.id, GenerationState.LOADING)
    registry.transition_job(job.id, GenerationState.GENERATING)
    registry.transition_job(job.id, GenerationState.FINALIZING)
    artifact_path = layout.audio / "generation-finalizing-job.wav"
    artifact_path.write_bytes(b"private artifact")
    registry.create_artifact(
        job_id=job.id,
        artifact_id="artifact-finalizing-job",
        path="audio/generation-finalizing-job.wav",
        byte_size=len(b"private artifact"),
        sha256="a" * 64,
        sample_rate=48_000,
        channel_count=1,
        frame_count=0,
    )

    assert service.list_history() == ()
    with pytest.raises(GenerationArtifactNotFoundError):
        service.read_artifact("artifact-finalizing-job")


@pytest.mark.asyncio
async def test_preset_generation_requires_runtime_preset_capability_before_loading(
    tmp_path: Path,
) -> None:
    lease = _Lease(
        _events(),
        capabilities=WorkerCapabilities(
            engine_id="fake",
            engine_version="0.2.0",
            supported=frozenset({"streaming_synthesis"}),
            max_concurrency=1,
        ),
    )
    service, _registry, _layout, _events_store = _service(tmp_path, lease)

    with pytest.raises(GenerationCapabilityError, match="preset"):
        await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")

    assert lease.loaded == []
    assert lease.requests == []


@pytest.mark.asyncio
async def test_voice_listing_requires_runtime_preset_capability_before_loading(
    tmp_path: Path,
) -> None:
    lease = _Lease(
        _events(),
        capabilities=WorkerCapabilities(
            engine_id="fake",
            engine_version="0.2.0",
            supported=frozenset({"streaming_synthesis"}),
            max_concurrency=1,
        ),
    )
    service, _registry, _layout, _events_store = _service(tmp_path, lease)

    with pytest.raises(GenerationCapabilityError, match="preset"):
        await service.list_voices("model-one")

    assert lease.loaded == []


@pytest.mark.asyncio
async def test_voice_listing_rechecks_runtime_preset_capability_after_loading(
    tmp_path: Path,
) -> None:
    lease = _Lease(_events(), drop_preset_capability_on_load=True)
    service, _registry, _layout, _events_store = _service(tmp_path, lease)

    with pytest.raises(GenerationCapabilityError, match="preset"):
        await service.list_voices("model-one")

    assert lease.loaded == ["model-one"]


@pytest.mark.asyncio
async def test_preset_generation_rejects_missing_preset_capability_after_loading(
    tmp_path: Path,
) -> None:
    lease = _Lease(_events(), drop_preset_capability_on_load=True)
    service, registry, _layout, _events_store = _service(tmp_path, lease)

    with pytest.raises(GenerationCapabilityError, match="preset"):
        await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")

    assert lease.loaded == ["model-one"]
    assert lease.requests == []
    assert registry.list_history() == ()
    assert registry.list_jobs() == ()


@pytest.mark.asyncio
async def test_reference_generation_claims_transcript_builds_oneof_and_cleans_after_completion(
    tmp_path: Path,
) -> None:
    lease = _Lease(_events(), capabilities=_reference_capabilities())
    service, registry, layout, _ = _service(tmp_path, lease)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=EventStore(Database(layout.database_path)),
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    reference_path = layout.reference_staging / reference_id

    queued = await service.create(
        model_id="model-one", reference_id=reference_id, text="clone me"
    )
    assert queued.voice_id is None
    assert queued.reference_id == reference_id
    assert reference_path.exists()
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    assert completed.reference_id is None
    request = lease.requests[0]
    assert request.WhichOneof("voice_source") == "reference"
    assert request.reference.reference_path == f"staging/references/{reference_id}"
    assert request.reference.transcript == "spoken transcript"
    assert not reference_path.exists()
    with pytest.raises(ReferenceRecordNotFoundError):
        reference_service.get(reference_id)


@pytest.mark.asyncio
async def test_reference_generation_rejects_missing_or_ambiguous_source(tmp_path: Path) -> None:
    service, registry, layout, _ = _service(tmp_path, _Lease(_events()))
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(_Lease(_events())),
        layout=layout,
        event_store=EventStore(Database(layout.database_path)),
        reference_service=reference_service,
    )

    with pytest.raises(GenerationRequestError, match="exactly one"):
        await service.create(model_id="model-one", text="text")
    with pytest.raises(GenerationRequestError, match="exactly one"):
        await service.create(model_id="model-one", voice_id="fake-neutral", reference_id="ref", text="text")


@pytest.mark.asyncio
async def test_reference_capability_is_required_before_claim(tmp_path: Path) -> None:
    lease = _Lease(_events())
    service, registry, layout, _ = _service(tmp_path, lease)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=EventStore(Database(layout.database_path)),
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)

    with pytest.raises(GenerationCapabilityError, match="reference"):
        await service.create(model_id="model-one", reference_id=reference_id, text="text")

    assert reference_service.get(reference_id).state.value == "validated"


@pytest.mark.asyncio
async def test_reference_cleanup_happens_after_stream_terminal_path(tmp_path: Path) -> None:
    lease = _Lease(_events(), block=True, capabilities=_reference_capabilities())
    registry, layout, events = _registry(tmp_path)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    queued = await service.create(model_id="model-one", reference_id=reference_id, text="cancel me")
    while not lease.streams:
        await asyncio.sleep(0)
    assert (layout.reference_staging / reference_id).exists()

    await service.cancel(queued.id)
    cancelled = await service.wait(queued.id)

    assert cancelled.state is GenerationState.CANCELLED
    assert not (layout.reference_staging / reference_id).exists()
    assert cancelled.reference_id is None


@pytest.mark.asyncio
async def test_reference_cleanup_happens_after_malformed_pcm(tmp_path: Path) -> None:
    lease = _Lease(_events(sample_rate=44_100), capabilities=_reference_capabilities())
    registry, layout, events = _registry(tmp_path)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    queued = await service.create(model_id="model-one", reference_id=reference_id, text="malformed")

    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert failed.error["code"] == "malformed_pcm"
    assert failed.reference_id is None
    assert not (layout.reference_staging / reference_id).exists()


@pytest.mark.asyncio
async def test_recover_marks_reference_job_recovery_required_when_transcript_is_lost(tmp_path: Path) -> None:
    lease = _Lease(_events(), capabilities=_reference_capabilities())
    registry, layout, events = _registry(tmp_path)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    queued = await service.create(model_id="model-one", reference_id=reference_id, text="restart")
    registry.transition_job(queued.id, GenerationState.LOADING)
    restarted_reference_service = ReferenceService(Database(layout.database_path), layout)
    restarted = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=restarted_reference_service,
    )

    await restarted.recover()

    recovered = registry.get_job(queued.id)
    assert recovered.state is GenerationState.FAILED
    assert recovered.error["code"] == "reference_recovery_required"
    assert not (layout.reference_staging / reference_id).exists()


@pytest.mark.asyncio
async def test_close_fails_queued_reference_job_closed_before_redacting_reference_id(tmp_path: Path) -> None:
    lease = _Lease(_events(), capabilities=_reference_capabilities())
    registry, layout, events = _registry(tmp_path)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    queued = await service.create(model_id="model-one", reference_id=reference_id, text="shutdown")

    await service.close()

    closed = registry.get_job(queued.id)
    assert closed.state is GenerationState.FAILED
    assert closed.error["code"] == "reference_recovery_required"
    assert closed.reference_id is None
    assert not (layout.reference_staging / reference_id).exists()
    assert any(event.event_type == "generation.failed" for event in events.read_after(0).events)


@pytest.mark.asyncio
async def test_close_retains_reference_id_and_records_cleanup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = _Lease(_events(), capabilities=_reference_capabilities())
    registry, layout, events = _registry(tmp_path)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    queued = await service.create(model_id="model-one", reference_id=reference_id, text="shutdown cleanup")
    monkeypatch.setattr(service, "_release_reference", lambda handle: False)

    await service.close()

    closed = registry.get_job(queued.id)
    assert closed.state is GenerationState.FAILED
    assert closed.error == {
        "code": "cleanup_failed",
        "message": "Generation cleanup failed; restart Core to retry cleanup.",
        "retryable": True,
    }
    assert closed.reference_id == reference_id
    assert any(event.event_type == "generation.failed" for event in events.read_after(0).events)


@pytest.mark.asyncio
async def test_close_fails_in_flight_reference_job_without_clearing_reference_id_until_cleanup(
    tmp_path: Path,
) -> None:
    lease = _Lease(_events(), block=True, capabilities=_reference_capabilities())
    registry, layout, events = _registry(tmp_path)
    reference_service = _reference_service(layout, registry)
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(lease),
        layout=layout,
        event_store=events,
        reference_service=reference_service,
    )
    reference_id = _validated_reference(reference_service)
    queued = await service.create(model_id="model-one", reference_id=reference_id, text="in flight")
    while not lease.streams:
        await asyncio.sleep(0)

    await service.close()

    closed = registry.get_job(queued.id)
    assert closed.state is GenerationState.FAILED
    assert closed.error["code"] == "reference_recovery_required"
    assert closed.reference_id is None
    assert not (layout.reference_staging / reference_id).exists()


@pytest.mark.asyncio
async def test_recover_never_reschedules_queued_reference_job_without_reference_service(tmp_path: Path) -> None:
    registry, layout, events = _registry(tmp_path)
    job = registry.create_job(
        job_id="queued-reference",
        model_id="model-one",
        engine_id="fake",
        reference_id="missing-reference",
        text="restart",
        correlation_id="queued-reference-correlation",
    )
    service = GenerationService(
        registry,
        _Models(_model()),
        _Pool(_Lease(_events())),
        layout=layout,
        event_store=events,
    )

    await service.recover()

    recovered = registry.get_job(job.id)
    assert recovered.state is GenerationState.FAILED
    assert recovered.error["code"] == "reference_recovery_required"
    assert recovered.reference_id == "missing-reference"


@pytest.mark.asyncio
async def test_create_rejects_voice_not_reported_by_runtime_worker(tmp_path: Path) -> None:
    service, registry, _, _ = _service(tmp_path, _Lease(_events()))

    with pytest.raises(GenerationVoiceNotFoundError):
        await service.create(model_id="model-one", voice_id="stale-voice", text="hello")

    assert registry.list_jobs() == ()


@pytest.mark.asyncio
async def test_malformed_pcm_fails_and_removes_private_files(tmp_path: Path) -> None:
    service, registry, layout, events = _service(tmp_path, _Lease(_events(sample_rate=44_100)))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")
    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "malformed_pcm"
    assert tuple(layout.staging.iterdir()) == ()
    assert service.list_history() == ()
    assert events.read_after(0).events[-1].event_type == "generation.failed"
    assert registry.get_job(queued.id).artifact_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [
    [3, 0, 1], [0, 1, 3, 3], [0, 1, 3, 2], [0, 3, 1], [2, 0, 1, 3], [0, 1],
])
async def test_synthesis_requires_header_first_and_one_terminal_result(tmp_path: Path, order: list[int]) -> None:
    valid = _events()
    service, _, layout, _ = _service(tmp_path, _Lease([valid[index] for index in order]))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="event order")
    failed = await service.wait(queued.id)
    assert failed.state is GenerationState.FAILED
    assert failed.error["code"] == "malformed_pcm"
    assert service.list_history() == ()
    assert tuple(layout.staging.iterdir()) == ()
    assert tuple(layout.audio.iterdir()) == ()


@pytest.mark.asyncio
async def test_empty_pcm_chunk_does_not_stop_following_audio(tmp_path: Path) -> None:
    service, _, layout, _ = _service(tmp_path, _Lease(_events_with_empty_chunk()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="empty chunk")
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    artifact = service.list_history()[0]
    assert artifact.frame_count == 4
    assert (layout.root / artifact.path).stat().st_size > 44


@pytest.mark.asyncio
async def test_persistence_failure_after_publication_removes_artifact_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, layout, _ = _service(tmp_path, _Lease(_events()))

    def fail_create_artifact(**_: object) -> object:
        raise RuntimeError("artifact persistence failed")

    monkeypatch.setattr(registry, "create_artifact", fail_create_artifact)
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="persist failure")
    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert service.list_history() == ()
    assert tuple(layout.audio.glob("generation-*.wav")) == ()


@pytest.mark.asyncio
async def test_completion_persistence_failure_removes_file_and_artifact_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, layout, _ = _service(tmp_path, _Lease(_events()))
    original_transition = registry.transition_job

    def fail_completion(job_id: str, state: GenerationState, **kwargs: Any) -> GenerationJob:
        if state is GenerationState.COMPLETED:
            raise RuntimeError("completion persistence failed")
        return original_transition(job_id, state, **kwargs)

    monkeypatch.setattr(registry, "transition_job", fail_completion)
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="complete failure")
    failed = await service.wait(queued.id)

    assert failed.state is GenerationState.FAILED
    assert service.list_history() == ()
    assert tuple(layout.audio.glob("generation-*.wav")) == ()


@pytest.mark.asyncio
async def test_unsupported_streaming_capability_fails_before_queueing(tmp_path: Path) -> None:
    lease = _Lease(
        _events(),
        capabilities=WorkerCapabilities(
            engine_id="fake",
            engine_version="0.2.0",
            supported=frozenset(),
            max_concurrency=1,
        ),
    )
    service, registry, _, _ = _service(tmp_path, lease)

    with pytest.raises(GenerationCapabilityError):
        await service.create(model_id="model-one", voice_id="fake-neutral", text="unsupported")

    assert registry.list_jobs() == ()


@pytest.mark.asyncio
async def test_cancellation_closes_worker_stream_and_removes_staging(tmp_path: Path) -> None:
    lease = _Lease(_events(), block=True)
    service, registry, layout, _ = _service(tmp_path, lease)

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="hello")
    await asyncio.sleep(0)
    cancelled = await service.cancel(queued.id)
    terminal = await service.wait(queued.id)

    assert cancelled.cancellation_requested is True
    assert terminal.state is GenerationState.CANCELLED
    assert lease.streams[0].closed is True
    assert tuple(layout.staging.iterdir()) == ()
    assert registry.get_job(queued.id).artifact_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancellation_is_terminal_only_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool,
) -> None:
    lease = _Lease(_events(), block=True)
    service, _, layout, events = _service(tmp_path, lease)
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="cancel cleanup")
    while not tuple(layout.staging.iterdir()):
        await asyncio.sleep(0)
    original_unlink = Path.unlink
    states_at_cleanup = []

    def unlink(path: Path, *args, **kwargs):
        if path.parent == layout.staging:
            states_at_cleanup.append(service.get(queued.id).state)
            if cleanup_fails:
                raise PermissionError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    await service.cancel(queued.id)
    terminal = await service.wait(queued.id)
    assert states_at_cleanup
    assert all(state is GenerationState.GENERATING for state in states_at_cleanup)
    if cleanup_fails:
        assert terminal.state is GenerationState.FAILED
        assert terminal.error["code"] == "cleanup_failed"
        assert terminal.error["retryable"] is False
        assert not any(event.event_type == "generation.cancelled" for event in events.read_after(0).events)
        monkeypatch.undo()
        await service.recover()
    else:
        assert terminal.state is GenerationState.CANCELLED
    assert tuple(layout.staging.iterdir()) == ()
    assert service.list_history() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_state", [GenerationState.LOADING, GenerationState.GENERATING, GenerationState.FINALIZING])
@pytest.mark.parametrize("cancel_requested", [False, True])
async def test_recover_fails_abandoned_active_jobs_and_reschedules_queued_jobs(
    tmp_path: Path, active_state: GenerationState, cancel_requested: bool,
) -> None:
    service, registry, layout, _ = _service(tmp_path, _Lease(_events()))
    active = registry.create_job(
        job_id="active-job",
        model_id="model-one",
        engine_id="fake",
        voice_id="fake-neutral",
        text="active",
        correlation_id="active-correlation",
    )
    registry.transition_job(active.id, GenerationState.LOADING)
    if active_state in {GenerationState.GENERATING, GenerationState.FINALIZING}:
        registry.transition_job(active.id, GenerationState.GENERATING)
    if active_state is GenerationState.FINALIZING:
        registry.transition_job(active.id, GenerationState.FINALIZING)
    if cancel_requested:
        registry.request_cancellation(active.id)
    queued = registry.create_job(
        job_id="queued-job",
        model_id="model-one",
        engine_id="fake",
        voice_id="fake-neutral",
        text="queued",
        correlation_id="queued-correlation",
    )
    (layout.staging / ".generation-active-job.pcm").write_bytes(b"partial")
    (layout.audio / ".generation-active-job.wav").write_bytes(b"partial")
    (layout.audio / "generation-active-job.wav").write_bytes(b"published")
    registry.create_artifact(
        job_id=active.id,
        artifact_id="artifact-active-job",
        path="audio/generation-active-job.wav",
        byte_size=len(b"published"),
        sha256="a" * 64,
        sample_rate=48_000,
        channel_count=1,
        frame_count=0,
    )

    await service.recover()
    recovered = registry.get_job(active.id)
    assert recovered.state is GenerationState.FAILED
    assert recovered.error == {
        "code": "recovery_required",
        "message": "Generation was interrupted and requires retry.",
        "retryable": True,
    }
    assert not (layout.staging / ".generation-active-job.pcm").exists()
    assert not (layout.audio / ".generation-active-job.wav").exists()
    assert not (layout.audio / "generation-active-job.wav").exists()
    assert service.list_history() == ()
    assert (await service.wait(queued.id)).state is GenerationState.COMPLETED


@pytest.mark.asyncio
async def test_recover_preserves_replacement_without_publication_identity(tmp_path: Path) -> None:
    service, registry, layout, _ = _service(tmp_path, _Lease(_events()))
    active = registry.create_job(
        job_id="replacement-job",
        model_id="model-one",
        engine_id="fake",
        voice_id="fake-neutral",
        text="replacement",
        correlation_id="replacement-correlation",
    )
    registry.transition_job(active.id, GenerationState.LOADING)
    registry.transition_job(active.id, GenerationState.GENERATING)

    destination = layout.audio / "generation-replacement-job.wav"
    original = tmp_path / "original.wav"
    original.write_bytes(b"original")
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"replacement")
    destination.write_bytes(original.read_bytes())
    os.replace(replacement, destination)

    await service.recover()

    recovered = registry.get_job(active.id)
    assert recovered.state is GenerationState.FAILED
    assert recovered.error == {
        "code": "cleanup_failed",
        "message": "Generation cleanup failed; restart Core to retry cleanup.",
        "retryable": True,
    }
    assert destination.read_bytes() == b"replacement"

    await service.recover()
    assert registry.get_job(active.id).error == recovered.error
    assert destination.read_bytes() == b"replacement"


@pytest.mark.asyncio
async def test_completed_scheduler_tasks_are_removed_from_tracking(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="bounded task")
    assert (await service.wait(queued.id)).state is GenerationState.COMPLETED
    await asyncio.sleep(0)

    assert service._scheduler.tracked_count == 0


@pytest.mark.asyncio
async def test_retention_opt_out_completes_without_history_or_artifact(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(
        model_id="model-one",
        voice_id="fake-neutral",
        text="private",
        retain_artifact=False,
    )
    completed = await service.wait(queued.id)

    assert completed.state is GenerationState.COMPLETED
    assert completed.artifact_id is None
    assert service.list_history() == ()


@pytest.mark.asyncio
async def test_artifact_read_and_delete_remove_only_the_retained_history_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="history")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    assert path.exists()
    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )

    assert service.delete_artifact(artifact.id) is True
    assert not path.exists()
    assert service.list_history() == ()
    assert service.get(completed.id).artifact_id is None
    assert service.delete_artifact(artifact.id) is False


@pytest.mark.asyncio
async def test_artifact_delete_keeps_metadata_when_managed_wav_is_missing(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="history")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    path.unlink()

    assert service.delete_artifact(artifact.id) is False
    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id


@pytest.mark.asyncio
async def test_open_artifact_rejects_same_size_tampering(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="integrity")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(GenerationArtifactNotFoundError):
        service.open_artifact(artifact.id)


@pytest.mark.asyncio
async def test_open_artifact_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("FIFO/no-follow primitives are unavailable")
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="fifo")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    path.unlink()
    os.mkfifo(path)

    with pytest.raises(GenerationArtifactNotFoundError):
        await asyncio.wait_for(asyncio.to_thread(service.open_artifact, artifact.id), timeout=1)


@pytest.mark.asyncio
async def test_open_artifact_does_not_normalize_operational_error_to_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="read error")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    def fail_fdopen(*_args: object, **_kwargs: object):
        raise OSError(service_module.errno.EIO, "managed storage read failed")

    monkeypatch.setattr(service_module.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="managed storage read failed"):
        service.open_artifact(artifact.id)


@pytest.mark.asyncio
async def test_open_artifact_rejects_oversized_file_before_hashing(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="oversized")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    path.write_bytes(path.read_bytes() + b"oversized")

    with pytest.raises(GenerationArtifactNotFoundError):
        await asyncio.wait_for(asyncio.to_thread(service.open_artifact, artifact.id), timeout=1)


@pytest.mark.asyncio
async def test_open_artifact_returns_snapshot_after_same_inode_mutation(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="snapshot")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original = path.read_bytes()

    handle = service.open_artifact(artifact.id)
    path.write_bytes(b"x" * len(original))

    try:
        assert handle.read() == original
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_open_artifact_rejects_symlink_replacement(tmp_path: Path) -> None:
    service, _, layout, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="race")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(GenerationArtifactNotFoundError):
        service.open_artifact(artifact.id)
    assert outside == path.resolve()
    assert layout.audio == path.parent


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_delete_artifact_normalizes_unsafe_managed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="unsafe delete")
    await service.wait(queued.id)
    artifact = service.list_history()[0]

    def unsafe(_artifact: object):
        from tts_studio.storage.layout import UnsafeStoragePathError
        raise UnsafeStoragePathError("redirected")

    monkeypatch.setattr(service, "_artifact_path", unsafe)
    with pytest.raises(GenerationArtifactNotFoundError):
        service.delete_artifact(artifact.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_number", [service_module.errno.EIO, service_module.errno.EMFILE, service_module.errno.EACCES])
async def test_delete_artifact_preserves_operational_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="storage error")
    await service.wait(queued.id)
    artifact = service.list_history()[0]

    def fail_fdopen(*_args: object, **_kwargs: object):
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(service_module.os, "fdopen", fail_fdopen)

    with pytest.raises(GenerationArtifactDeletionError):
        service.delete_artifact(artifact.id)

    assert service.list_history() == (artifact,)


@pytest.mark.asyncio
async def test_delete_artifact_maps_snapshot_read_error_to_deletion_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="snapshot error")
    await service.wait(queued.id)
    artifact = service.list_history()[0]

    class BrokenSnapshot:
        def read(self) -> bytes:
            raise OSError(service_module.errno.EIO, "snapshot read failed")

    class Candidate:
        snapshot = BrokenSnapshot()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        service,
        "_open_validated_artifact_for_deletion",
        lambda _artifact, _path: Candidate(),
    )

    with pytest.raises(GenerationArtifactDeletionError):
        service.delete_artifact(artifact.id)

    assert service.list_history() == (artifact,)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _identity_bound_unlink_available(),
    reason="identity-bound unlink is unavailable on this platform",
)
async def test_delete_rejects_ancestor_replacement_before_unlink(
    tmp_path: Path,
) -> None:
    service, _, layout, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="ancestor race")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    candidate = service._open_validated_artifact_for_deletion(artifact, path)
    displaced_root = tmp_path / "data-displaced"

    try:
        layout.root.rename(displaced_root)
        layout.root.mkdir()
        (layout.root / "audio").mkdir()
        displaced_audio = displaced_root / "audio"
        (layout.root / "audio").rmdir()
        displaced_audio.rename(layout.root / "audio")

        with pytest.raises(GenerationArtifactDeletionError):
            service._require_current_audio_directory(candidate)
    finally:
        candidate.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _identity_bound_unlink_available(),
    reason="identity-bound unlink is unavailable on this platform",
)
async def test_identity_bound_unlink_targets_open_directory_when_path_is_replaced(
    tmp_path: Path,
) -> None:
    service, _, layout, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="directory race")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original = path.read_bytes()
    candidate = service._open_validated_artifact_for_deletion(artifact, path)
    displaced_audio = layout.root / "audio-displaced"

    try:
        layout.audio.rename(displaced_audio)
        layout.audio.mkdir()
        os.link(displaced_audio / path.name, layout.audio / path.name)

        service._unlink_validated_artifact(candidate)

        assert not (displaced_audio / path.name).exists()
        assert (layout.audio / path.name).read_bytes() == original
    finally:
        candidate.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _identity_bound_unlink_available(),
    reason="identity-bound unlink is unavailable on this platform",
)
async def test_delete_artifact_rejects_entry_replacement_after_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, layout, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="entry race")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    replacement = bytes(reversed(path.read_bytes()))
    original_open = service._open_validated_artifact_for_deletion

    def replace_after_open(current_artifact: object, current_path: Path):
        candidate = original_open(current_artifact, current_path)
        staged = layout.audio / "entry-replacement.wav"
        staged.write_bytes(replacement)
        staged.replace(path)
        return candidate

    monkeypatch.setattr(service, "_open_validated_artifact_for_deletion", replace_after_open)

    with pytest.raises(GenerationArtifactNotFoundError):
        await asyncio.wait_for(
            asyncio.to_thread(service.delete_artifact, artifact.id),
            timeout=1,
        )

    assert path.read_bytes() == replacement
    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id


@pytest.mark.asyncio
async def test_delete_artifact_rejects_fifo_replacement_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("FIFO/no-follow primitives are unavailable")
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="delete fifo")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original_checked_directory = StorageLayout.checked_directory
    audio_checks = 0

    def replace_with_fifo(layout: StorageLayout, name: Any) -> Path:
        nonlocal audio_checks
        directory = original_checked_directory(layout, name)
        if layout is service._layout and name == "audio":
            audio_checks += 1
            if audio_checks == 2:
                path.unlink()
                os.mkfifo(path)
        return directory

    monkeypatch.setattr(StorageLayout, "checked_directory", replace_with_fifo)
    deletion = asyncio.create_task(asyncio.to_thread(service.delete_artifact, artifact.id))
    assert await asyncio.wait_for(deletion, timeout=0.5) is False

    assert path.is_fifo()
    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id


def test_identity_bound_unlink_passes_relative_name_and_normalizes_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[bytes] = []

    class FailedFunlinkat:
        argtypes: object = None
        restype: object = None

        def __call__(
            self,
            _directory_fd: int,
            path: bytes,
            _file_fd: int,
            _flags: int,
        ) -> int:
            paths.append(path)
            identity_module.ctypes.set_errno(service_module.errno.EPERM)
            return -1

    monkeypatch.setattr(
        identity_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(funlinkat=FailedFunlinkat()),
    )
    candidate = SimpleNamespace(
        directory_fd=1,
        file_fd=2,
        name="artifact.wav",
    )

    with pytest.raises(GenerationArtifactDeletionError):
        GenerationService._unlink_validated_artifact(candidate)

    assert paths == [b"artifact.wav"]


@pytest.mark.asyncio
async def test_delete_artifact_does_not_use_path_unlink_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="dir race")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    service.read_artifact(artifact.id)

    def forbidden_unlink(_path: Path, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("deletion must use a validated directory descriptor")

    monkeypatch.setattr(Path, "unlink", forbidden_unlink)
    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    assert service.delete_artifact(artifact.id) is True
    assert service.list_history() == ()


@pytest.mark.asyncio
async def test_delete_artifact_does_not_restore_when_registry_reports_row_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="false delete")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original_delete = registry.delete_artifact

    def delete_but_report_absent(artifact_id: str) -> bool:
        assert original_delete(artifact_id) is True
        return False

    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    monkeypatch.setattr(registry, "delete_artifact", delete_but_report_absent)

    assert service.delete_artifact(artifact.id) is False
    assert not path.exists()
    assert service.list_history() == ()
    assert service.get(completed.id).artifact_id is None


@pytest.mark.asyncio
async def test_restore_artifact_retries_short_writes_and_verifies_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="short restore")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    payload = path.read_bytes()
    path.unlink()
    original_write = service_module.os.write

    def short_write(file_fd: int, data: bytes) -> int:
        amount = max(1, len(data) // 3)
        return original_write(file_fd, data[:amount])

    monkeypatch.setattr(service_module.os, "write", short_write)

    service._restore_artifact(path, payload)

    assert path.read_bytes() == payload


@pytest.mark.asyncio
async def test_restore_artifact_removes_partial_file_and_retains_metadata_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="failed restore")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    payload = path.read_bytes()
    path.unlink()
    original_write = service_module.os.write
    writes = 0

    def fail_after_partial_write(file_fd: int, data: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(file_fd, data[: max(1, len(data) // 3)])
        raise OSError("storage unavailable")

    monkeypatch.setattr(service_module.os, "write", fail_after_partial_write)

    with pytest.raises(OSError, match="storage unavailable"):
        service._restore_artifact(path, payload)

    assert not path.exists()
    assert list(path.parent.glob(f".{path.name}.restore-*.tmp")) == []
    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id


@pytest.mark.asyncio
async def test_restore_artifact_surfaces_post_publication_staging_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="cleanup restore")
    await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    payload = path.read_bytes()
    path.unlink()
    original_unlink = service_module.os.unlink

    def fail_restore_cleanup(name: str, *args: object, **kwargs: object) -> None:
        if name.startswith(f".{path.name}.restore-"):
            raise PermissionError("restore staging cleanup denied")
        original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(service_module.os, "unlink", fail_restore_cleanup)

    with pytest.raises(PermissionError, match="restore staging cleanup denied"):
        service._restore_artifact(path, payload, artifact=artifact)

    assert path.read_bytes() == payload
    assert len(list(path.parent.glob(f".{path.name}.restore-*.tmp"))) == 1


@pytest.mark.asyncio
async def test_recover_removes_abandoned_restore_staging_entry(tmp_path: Path) -> None:
    service, _, layout, _ = _service(tmp_path, _Lease(_events()))
    abandoned = layout.audio / ".generation-abandoned.wav.restore-deadbeef.tmp"
    abandoned.write_bytes(b"published rollback copy")

    await service.recover()

    assert not abandoned.exists()


@pytest.mark.asyncio
async def test_delete_artifact_preserves_db_error_after_successful_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="database failure")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original = path.read_bytes()
    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    monkeypatch.setattr(
        registry,
        "delete_artifact",
        lambda _artifact_id: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.delete_artifact(artifact.id)

    assert path.read_bytes() == original
    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id


@pytest.mark.asyncio
async def test_delete_artifact_reports_restore_failure_without_leaving_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, _, _ = _service(tmp_path, _Lease(_events()))
    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="restore failure")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)

    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    monkeypatch.setattr(
        registry,
        "delete_artifact",
        lambda _artifact_id: (_ for _ in ()).throw(OSError("db")),
    )
    monkeypatch.setattr(
        service,
        "_restore_artifact",
        lambda _path, _payload, **_kwargs: (_ for _ in ()).throw(OSError("restore")),
    )

    with pytest.raises(GenerationArtifactDeletionError):
        service.delete_artifact(artifact.id)

    assert not path.exists()
    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id


@pytest.mark.asyncio
async def test_recover_reconciles_history_row_left_by_file_first_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, _, _ = _service(tmp_path, _Lease(_events()))

    queued = await service.create(model_id="model-one", voice_id="fake-neutral", text="reconcile")
    completed = await service.wait(queued.id)
    artifact = service.list_history()[0]
    path = service.read_artifact(artifact.id)
    original_delete = registry.delete_artifact

    def fail_delete(artifact_id: str) -> bool:
        del artifact_id
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    monkeypatch.setattr(registry, "delete_artifact", fail_delete)
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.delete_artifact(artifact.id)
    assert path.exists()
    assert path.read_bytes()
    assert service.list_history() == (artifact,)

    monkeypatch.setattr(registry, "delete_artifact", original_delete)
    await service.recover()

    assert service.list_history() == (artifact,)
    assert service.get(completed.id).artifact_id == artifact.id
