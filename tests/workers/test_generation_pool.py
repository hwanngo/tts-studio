from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.models.registry import ModelInstallation
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.generation import (
    REFERENCE_VALIDATION_DEADLINE_SECONDS,
    AlignmentCapability,
    GrpcWorkerLease,
    WorkerCapabilities,
    WorkerCapacityError,
    WorkerLease,
    WorkerModelMismatchError,
    WorkerOperationError,
    WorkerReplicaPool,
    build_synthesis_request,
)
from tts_studio.workers.process import WorkerLaunchSpec, WorkerProcess
from tts_studio.workers.supervisor import WorkerSupervisor

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FAKE_WORKER_LAUNCH = WorkerLaunchSpec(
    command=(
        "uv",
        "run",
        "--project",
        "workers/fake",
        "tts-studio-fake-worker",
    ),
    cwd=_REPOSITORY_ROOT,
)


def _model() -> ModelInstallation:
    return ModelInstallation(
        id="fixtures/compatible",
        repository_id="fixtures/compatible",
        requested_revision=None,
        resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
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


class _RecordingSynthesisCall:
    def __aiter__(self) -> Any:
        async def events() -> Any:
            if False:
                yield engine_pb2.SynthesisEvent()

        return events()

    def cancel(self) -> None:
        return None


class _RecordingStub:
    def __init__(self) -> None:
        self.timeouts: dict[str, float | None] = {}
        self.metadata: dict[str, Any] = {}
        self.requests: list[Any] = []
        self.load_calls = 0

    async def LoadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del request, metadata
        self.load_calls += 1
        self.timeouts["load"] = timeout
        return engine_pb2.LoadModelResponse(loaded=True)

    async def UnloadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del request, metadata
        self.timeouts["unload"] = timeout
        return engine_pb2.UnloadModelResponse(unloaded=True)

    async def ListVoices(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        del request, metadata
        self.timeouts["voices"] = timeout
        return engine_pb2.ListVoicesResponse()

    async def ValidateReference(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        self.metadata["reference"] = metadata
        self.timeouts["reference"] = timeout
        return engine_pb2.ValidateReferenceResponse(
            valid=True,
            metadata=engine_pb2.ReferenceMetadata(
                sample_rate_hz=16000,
                channels=1,
                duration_ms=1000,
                byte_size=4,
                container="wav",
            ),
        )

    async def Align(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        self.metadata["alignment"] = metadata
        self.timeouts["alignment"] = timeout
        self.requests.append(request)
        return engine_pb2.AlignResponse(
            result=engine_pb2.AlignmentResult(
                schema_version=1,
                transcript=request.transcript,
                sample_rate_hz=48000,
                total_frames=480,
                unit="word",
                aligner="fake-aligner/1.0",
                units=[
                    engine_pb2.AlignmentUnit(
                        text=request.transcript,
                        source_start=0,
                        source_end=len(request.transcript.encode("utf-8")),
                        start_frames=0,
                        end_frames=480,
                        confidence=1.0,
                    )
                ],
            )
        )

    def Synthesize(self, request: Any, *, metadata: Any, timeout: float) -> Any:
        self.requests.append(request)
        del metadata
        self.timeouts["synthesis"] = timeout
        return _RecordingSynthesisCall()


@pytest.mark.asyncio
async def test_authenticated_lease_bounds_all_runtime_rpc_deadlines() -> None:
    stub = _RecordingStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities(
                engine_id="fake",
                engine_version="0.2.0",
                supported=frozenset({"streaming_synthesis"}),
                max_concurrency=1,
            ),
        ),
    )
    lease = GrpcWorkerLease(worker)
    model = _model()

    await lease.load_model(model)
    await lease.list_voices()
    await lease.validate_reference("staging/references/ref", model.id, "hello")
    stream = lease.synthesize(
        engine_pb2.SynthesizeRequest(model_id=model.id, voice_id="fake-neutral", text="test")
    )
    await stream.aclose()
    await lease.unload_model(model)

    assert set(stub.timeouts) == {"load", "unload", "voices", "reference", "synthesis"}
    assert all(timeout is not None and 0 < timeout <= 600 for timeout in stub.timeouts.values())
    assert stub.timeouts["reference"] <= REFERENCE_VALIDATION_DEADLINE_SECONDS
    assert stub.metadata["reference"] == (("x-tts-worker-token", "worker-token"),)
    assert stub.requests[0].WhichOneof("voice_source") == "voice_id"


@pytest.mark.asyncio
async def test_loading_the_same_model_twice_on_one_lease_is_idempotent() -> None:
    stub = _RecordingStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset(), 1),
        ),
    )
    lease = GrpcWorkerLease(worker)
    model = _model()

    await lease.load_model(model)
    await lease.load_model(model)

    assert stub.load_calls == 1


@pytest.mark.asyncio
async def test_authenticated_lease_accepts_reference_oneof_request() -> None:
    stub = _RecordingStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset({"streaming_synthesis"}), 1),
        ),
    )
    lease = GrpcWorkerLease(worker)
    request = build_synthesis_request(
        _model().id,
        "reference text",
        reference_path="staging/references/reference-one",
        transcript="sample transcript",
    )
    await lease.load_model(_model())

    stream = lease.synthesize(request)

    assert stub.requests[0].WhichOneof("voice_source") == "reference"
    assert stub.requests[0].reference.reference_path == "staging/references/reference-one"
    assert stub.requests[0].reference.transcript == "sample transcript"
    # Release the lease-owned stream slot used by this direct seam test.
    await stream.aclose()


@pytest.mark.asyncio
async def test_align_uses_authenticated_bounded_unary_rpc_and_requires_loaded_model() -> None:
    stub = _RecordingStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities(
                "fake",
                "0.2.0",
                frozenset({"alignment"}),
                1,
                AlignmentCapability(("word",), ("und",), "fake-aligner/1.0"),
            ),
        ),
    )
    lease = GrpcWorkerLease(worker)
    request = engine_pb2.AlignRequest(
        model_id=_model().id, audio_path="audio/artifact.wav", transcript="hello"
    )

    with pytest.raises(RuntimeError, match="load_model must precede"):
        await lease.align(request)

    await lease.load_model(_model())
    response = await lease.align(request)

    assert response.result.aligner == "fake-aligner/1.0"
    assert stub.timeouts["alignment"] == 30.0
    assert stub.metadata["alignment"] == (("x-tts-worker-token", "worker-token"),)


@pytest.mark.asyncio
async def test_align_rejects_unadvertised_or_malformed_runtime_responses() -> None:
    request = engine_pb2.AlignRequest(model_id=_model().id, transcript="hello")

    unavailable_worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=_RecordingStub(),
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset(), 1),
        ),
    )
    unavailable = GrpcWorkerLease(unavailable_worker)
    await unavailable.load_model(_model())
    with pytest.raises(WorkerOperationError) as unavailable_error:
        await unavailable.align(request)
    assert unavailable_error.value.code == "alignment_unavailable"

    class MalformedStub(_RecordingStub):
        async def Align(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del metadata, timeout
            return engine_pb2.AlignResponse(
                result=engine_pb2.AlignmentResult(
                    schema_version=1,
                    transcript=request.transcript,
                    sample_rate_hz=48000,
                    total_frames=480,
                    unit="word",
                    aligner="fake-aligner/1.0",
                )
            )

    malformed_worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=MalformedStub(),
            token="worker-token",
            capabilities=WorkerCapabilities(
                "fake", "0.2.0", frozenset({"alignment"}), 1,
                AlignmentCapability(("word",), ("und",), "fake-aligner/1.0"),
            ),
        ),
    )
    malformed = GrpcWorkerLease(malformed_worker)
    await malformed.load_model(_model())
    with pytest.raises(WorkerOperationError) as malformed_error:
        await malformed.align(request)
    assert malformed_error.value.code == "alignment_failed"
    assert malformed_error.value.retryable is False


@pytest.mark.asyncio
async def test_validate_reference_maps_worker_error_to_typed_operation_error() -> None:
    class ErrorStub(_RecordingStub):
        async def ValidateReference(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            return engine_pb2.ValidateReferenceResponse(
                error=engine_pb2.WorkerError(
                    code="reference_invalid", message="invalid reference", retryable=False
                )
            )

    stub = ErrorStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset(), 1),
        ),
    )
    lease = GrpcWorkerLease(worker)
    await lease.load_model(_model())

    with pytest.raises(WorkerOperationError, match="invalid reference") as raised:
        await lease.validate_reference("staging/references/ref", _model().id, None)

    assert raised.value.code == "reference_invalid"


def test_synthesis_request_builder_maps_optional_synthesis_options() -> None:
    request = build_synthesis_request(
        "model", "hello", voice_id="voice", speed=1.25, pitch=-0.5, volume=0.75
    )

    assert request.HasField("options")
    assert request.options.speed == 1.25
    assert request.options.pitch == -0.5
    assert request.options.volume == 0.75


def test_synthesis_request_builder_selects_exactly_one_voice_source() -> None:
    preset = build_synthesis_request("model", "hello", voice_id="voice")
    reference = build_synthesis_request(
        "model", "hello", reference_path="staging/references/ref", transcript="sample"
    )

    assert preset.WhichOneof("voice_source") == "voice_id"
    assert preset.voice_id == "voice"
    assert reference.WhichOneof("voice_source") == "reference"
    assert reference.reference.reference_path == "staging/references/ref"
    assert reference.reference.transcript == "sample"

    with pytest.raises(ValueError, match="exactly one"):
        build_synthesis_request("model", "hello")
    with pytest.raises(ValueError, match="exactly one"):
        build_synthesis_request("model", "hello", voice_id="voice", reference_path="ref")


@pytest.mark.asyncio
async def test_supervisor_acquires_a_typed_authenticated_lease_and_delegates_runtime_facts(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        async with supervisor.acquire(model) as lease:
            assert isinstance(lease, WorkerLease)
            assert isinstance(lease.capabilities, WorkerCapabilities)
            assert lease.capabilities.engine_id == "fake"
            assert "streaming_synthesis" in lease.capabilities.supported

            await lease.load_model(model)
            voices = await lease.list_voices()
            assert [(voice.id, voice.label) for voice in voices] == [
                ("fake-neutral", "Fake Neutral")
            ]
            events = [
                event
                async for event in lease.synthesize(
                    engine_pb2.SynthesizeRequest(
                        model_id=model.id,
                        voice_id=voices[0].id,
                        text="typed lease",
                    )
                )
            ]
            assert events[0].HasField("header")
            assert events[-1].HasField("result")
            await lease.unload_model(model)
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_loading_refreshes_supervisor_and_lease_capabilities(tmp_path: Path) -> None:
    class LazyCapabilityStub(_RecordingStub):
        def __init__(self) -> None:
            super().__init__()
            self.describe_calls = 0

        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            self.describe_calls += 1
            return engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="fake",
                engine_version="0.2.0",
                capabilities=[
                    engine_pb2.Capability(name="streaming_synthesis", supported=True),
                    engine_pb2.Capability(name="reference_cloning", supported=True),
                ],
                max_concurrency=1,
            )

    stub = LazyCapabilityStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            engine_id="fake",
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities(
                "fake", "0.2.0", frozenset({"streaming_synthesis"}), 1
            ),
        ),
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    supervisor._workers["fake"] = worker

    async with supervisor.acquire(_model()) as lease:
        assert "reference_cloning" not in lease.capabilities.supported
        await lease.load_model(_model())
        assert "reference_cloning" in lease.capabilities.supported

    assert "reference_cloning" in worker.capabilities.supported


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
            engine_id="fake",
            engine_version="0.2.0",
            max_concurrency=0,
        ),
        engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
            engine_id="fake",
            engine_version="0.2.0",
            max_concurrency=1,
            capabilities=[engine_pb2.Capability(name="", supported=True)],
        ),
    ],
)
async def test_load_rejects_malformed_refreshed_capabilities(
    response: engine_pb2.DescribeResponse,
) -> None:
    class MalformedStub(_RecordingStub):
        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            return response

    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            engine_id="fake",
            stub=MalformedStub(),
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset(), 1),
        ),
    )
    lease = GrpcWorkerLease(worker)

    with pytest.raises(ValueError):
        await lease.load_model(_model())


@pytest.mark.asyncio
async def test_load_refreshes_alignment_capability_advertised_only_after_model_load() -> None:
    class ModelCapabilityStub(_RecordingStub):
        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            return engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="fake",
                engine_version="0.2.0",
                capabilities=[
                    engine_pb2.Capability(name="streaming_synthesis", supported=True),
                    engine_pb2.Capability(name="alignment", supported=True),
                ],
                max_concurrency=1,
                alignment=engine_pb2.AlignmentCapability(
                    units=["word"], languages=["vi"], aligner="fake-aligner/1"
                ),
            )

    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=ModelCapabilityStub(),
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset(), 1),
        ),
    )
    lease = GrpcWorkerLease(worker)

    assert lease.capabilities.alignment is None
    await lease.load_model(_model())
    assert lease.capabilities.alignment == AlignmentCapability(("word",), ("vi",), "fake-aligner/1")


@pytest.mark.asyncio
async def test_load_clears_stale_startup_alignment_capability() -> None:
    class ModelCapabilityStub(_RecordingStub):
        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            return engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="fake", engine_version="0.2.0", max_concurrency=1
            )

    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=ModelCapabilityStub(),
            token="worker-token",
            capabilities=WorkerCapabilities(
                "fake", "0.2.0", frozenset(), 1,
                AlignmentCapability(("word",), ("vi",), "stale-aligner"),
            ),
        ),
    )
    lease = GrpcWorkerLease(worker)

    await lease.load_model(_model())
    assert lease.capabilities.alignment is None


@pytest.mark.asyncio
async def test_describe_failure_rolls_back_a_successful_load_on_a_strict_worker() -> None:
    class StrictStub(_RecordingStub):
        def __init__(self) -> None:
            super().__init__()
            self.loaded = False
            self.unload_calls = 0

        async def LoadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            response = await super().LoadModel(request, metadata=metadata, timeout=timeout)
            self.loaded = True
            return response

        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            raise RuntimeError("Describe failed")

        async def UnloadModel(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            self.unload_calls += 1
            assert self.loaded is True
            self.loaded = False
            return engine_pb2.UnloadModelResponse(unloaded=True)

    stub = StrictStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="worker-token",
            capabilities=WorkerCapabilities("fake", "0.2.0", frozenset(), 1),
        ),
    )
    lease = GrpcWorkerLease(worker)

    with pytest.raises(RuntimeError, match="Describe failed"):
        await lease.load_model(_model())

    assert stub.loaded is False
    assert stub.unload_calls == 1
    with pytest.raises(RuntimeError, match="load_model must precede"):
        await lease.list_voices()


@pytest.mark.asyncio
async def test_one_replica_rejects_contention_and_releases_after_exception(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first() -> None:
        async with supervisor.acquire(model):
            first_entered.set()
            await release_first.wait()

    first = asyncio.create_task(hold_first())
    try:
        await first_entered.wait()
        with pytest.raises(WorkerCapacityError, match="all Worker replicas are already leased"):
            async with supervisor.acquire(model):
                raise AssertionError("second lease must not be entered")

        release_first.set()
        await first

        with pytest.raises(RuntimeError, match="release me"):
            async with supervisor.acquire(model):
                raise RuntimeError("release me")

        async with supervisor.acquire(model):
            pass
    finally:
        release_first.set()
        await asyncio.gather(first, return_exceptions=True)
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_lease_pins_model_only_after_successful_load_and_rejects_other_models(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    other_model = replace(model, id="another-model")
    invalid_model = replace(model, runtime_variant="unsupported")
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        async with supervisor.acquire(model) as lease:
            with pytest.raises(WorkerOperationError, match="unsupported"):
                await lease.load_model(invalid_model)

            await lease.load_model(model)

            with pytest.raises(WorkerModelMismatchError, match="different model"):
                await lease.unload_model(other_model)
            with pytest.raises(WorkerModelMismatchError, match="different model"):
                lease.synthesize(
                    engine_pb2.SynthesizeRequest(
                        model_id=other_model.id,
                        voice_id="fake-neutral",
                        text="must be rejected",
                    )
                )

            await lease.unload_model(model)
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_lease_rejects_concurrent_synthesis_and_releases_after_cancellation(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        async with supervisor.acquire(model) as lease:
            await lease.load_model(model)
            request = engine_pb2.SynthesizeRequest(
                model_id=model.id,
                voice_id="fake-neutral",
                text="long " * 4000,
            )
            first = lease.synthesize(request)
            assert (await first.__anext__()).HasField("header")

            with pytest.raises(WorkerCapacityError, match="one active synthesis"):
                lease.synthesize(request)

            await first.aclose()
            released = [event async for event in lease.synthesize(request)]
            assert released[-1].HasField("result")
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_lease_releases_synthesis_slot_after_worker_error_and_normal_completion(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        async with supervisor.acquire(model) as lease:
            await lease.load_model(model)
            invalid = engine_pb2.SynthesizeRequest(
                model_id=model.id,
                voice_id="missing",
                text="worker error",
            )
            error_events = [event async for event in lease.synthesize(invalid)]
            assert error_events[0].error.code == "voice_not_found"

            valid = engine_pb2.SynthesizeRequest(
                model_id=model.id,
                voice_id="fake-neutral",
                text="normal completion",
            )
            events = [event async for event in lease.synthesize(valid)]
            assert events[-1].HasField("result")
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_lease_exit_closes_abandoned_stream_before_capacity_reacquisition(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    abandoned = None
    try:
        with pytest.raises(RuntimeError, match="caller raised"):
            async with supervisor.acquire(model) as lease:
                await lease.load_model(model)
                abandoned = lease.synthesize(
                    engine_pb2.SynthesizeRequest(
                        model_id=model.id,
                        voice_id="fake-neutral",
                        text="long " * 4000,
                    )
                )
                assert (await abandoned.__anext__()).HasField("header")
                raise RuntimeError("caller raised")

        assert abandoned is not None
        with pytest.raises(asyncio.CancelledError):
            await abandoned.__anext__()

        async with supervisor.acquire(model) as reacquired:
            await reacquired.load_model(model)
            events = [
                event
                async for event in reacquired.synthesize(
                    engine_pb2.SynthesizeRequest(
                        model_id=model.id,
                        voice_id="fake-neutral",
                        text="reacquired",
                    )
                )
            ]
            assert events[-1].HasField("result")
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_worker_termination_cleans_capacity_and_reaps_process(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    model = _model()
    await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    lease_context = supervisor.acquire(model)
    await lease_context.__aenter__()
    try:
        await supervisor.stop_all()
        assert (await supervisor.health("fake")).ready is False
    finally:
        await lease_context.__aexit__(None, None, None)

    with pytest.raises(RuntimeError, match="not running"):
        async with supervisor.acquire(model):
            pass


def test_protocols_are_runtime_checkable_contracts(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    assert isinstance(supervisor, WorkerReplicaPool)
