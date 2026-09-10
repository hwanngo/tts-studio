"""Typed Core-facing leases for authenticated generation Workers."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.models.registry import ModelInstallation
from tts_studio.workers.process import WorkerProcess

MODEL_LOAD_DEADLINE_SECONDS = 120.0
MODEL_UNLOAD_DEADLINE_SECONDS = 30.0
VOICE_LIST_DEADLINE_SECONDS = 10.0
CAPABILITY_REFRESH_DEADLINE_SECONDS = 10.0
REFERENCE_VALIDATION_DEADLINE_SECONDS = 30.0
ALIGNMENT_DEADLINE_SECONDS = 30.0
_MAX_ALIGNMENT_RESULT_BYTES = 1 << 20
_MAX_ALIGNMENT_UNITS = 10_000
SYNTHESIS_DEADLINE_SECONDS = 300.0


@dataclass(frozen=True)
class AlignmentCapability:
    units: tuple[str, ...]
    languages: tuple[str, ...]
    aligner: str


@dataclass(frozen=True)
class WorkerCapabilities:
    """Runtime capabilities reported by one authenticated Worker replica."""

    engine_id: str
    engine_version: str
    supported: frozenset[str]
    max_concurrency: int
    alignment: AlignmentCapability | None = None

    def supports(self, capability: str) -> bool:
        return capability in self.supported


class WorkerOperationError(RuntimeError):
    """A typed lifecycle failure returned by a Worker."""

    def __init__(self, error: engine_pb2.WorkerError) -> None:
        self.code = error.code
        self.retryable = error.retryable
        super().__init__(error.message or "Worker operation failed")


class WorkerCapacityError(RuntimeError):
    """Raised when the one-replica generation capacity is already leased."""


class WorkerModelMismatchError(RuntimeError):
    """Raised when an operation names a model different from the loaded model."""


@runtime_checkable
class WorkerLease(Protocol):
    capabilities: WorkerCapabilities

    async def load_model(self, model: ModelInstallation) -> None: ...

    async def unload_model(self, model: ModelInstallation) -> None: ...

    async def list_voices(self) -> tuple[engine_pb2.PresetVoice, ...]: ...

    async def validate_reference(
        self, reference_path: str, model_id: str, transcript: str | None
    ) -> engine_pb2.ValidateReferenceResponse: ...

    async def align(self, request: engine_pb2.AlignRequest) -> engine_pb2.AlignResponse: ...

    def synthesize(
        self, request: engine_pb2.SynthesizeRequest
    ) -> AsyncIterator[engine_pb2.SynthesisEvent]: ...


@runtime_checkable
class WorkerReplicaPool(Protocol):
    def acquire(self, model: ModelInstallation) -> AbstractAsyncContextManager[WorkerLease]: ...


class _CancellableSynthesisCall(Protocol):
    def __aiter__(self) -> AsyncIterator[engine_pb2.SynthesisEvent]: ...

    def cancel(self) -> object: ...


class GrpcWorkerLease:
    """Hide generated gRPC stubs and authentication behind WorkerLease."""

    def __init__(self, worker: WorkerProcess) -> None:
        self._worker = worker
        self.capabilities = worker.capabilities
        self._model_id: str | None = getattr(worker, "loaded_model_id", None)
        self._synthesis_active = False
        self._synthesis_stream: _LeaseSynthesisStream | None = None

    async def load_model(self, model: ModelInstallation) -> None:
        self._ensure_model_identity(model.id)
        if self._model_id == model.id:
            return
        response = await self._worker.stub.LoadModel(
            engine_pb2.LoadModelRequest(
                model_id=model.id,
                cache_path=model.cache_path,
                variant=model.runtime_variant,
            ),
            metadata=self._metadata(),
            timeout=MODEL_LOAD_DEADLINE_SECONDS,
        )
        if not response.loaded:
            _raise_worker_error(response.error, "Worker did not load the model")
        self._model_id = model.id
        self._worker.loaded_model_id = model.id
        describe = getattr(self._worker.stub, "Describe", None)
        if describe is not None:
            try:
                facts = await describe(
                    engine_pb2.DescribeRequest(),
                    metadata=self._metadata(),
                    timeout=CAPABILITY_REFRESH_DEADLINE_SECONDS,
                )
                from tts_studio.workers.supervisor import (
                    _protocol_compatible,
                    _worker_capabilities,
                )

                expected_engine_id = getattr(
                    self._worker, "engine_id", self.capabilities.engine_id
                )
                if facts.protocol.major != 1:
                    raise RuntimeError(
                        f"worker {expected_engine_id!r} uses unsupported protocol major "
                        f"{facts.protocol.major}"
                    )
                if not _protocol_compatible(facts.protocol):
                    raise RuntimeError(
                        f"worker {expected_engine_id!r} uses unsupported protocol minor "
                        f"{facts.protocol.minor}"
                    )
                if facts.engine_id != expected_engine_id:
                    raise RuntimeError(
                        f"worker engine identity mismatch: expected {expected_engine_id!r}, "
                        f"received {facts.engine_id!r}"
                    )
                capabilities = _worker_capabilities(facts)
            except BaseException:
                with suppress(BaseException):
                    await self.unload_model(model)
                raise
            self.capabilities = capabilities
            self._worker.capabilities = capabilities

    async def unload_model(self, model: ModelInstallation) -> None:
        self._ensure_loaded_model(model.id)
        response = await self._worker.stub.UnloadModel(
            engine_pb2.UnloadModelRequest(model_id=model.id),
            metadata=self._metadata(),
            timeout=MODEL_UNLOAD_DEADLINE_SECONDS,
        )
        if not response.unloaded:
            try:
                _raise_worker_error(response.error, "Worker did not unload the model")
            except WorkerOperationError as error:
                if error.code == "model_not_loaded":
                    self._model_id = None
                    self._worker.loaded_model_id = None
                raise
        self._model_id = None
        self._worker.loaded_model_id = None

    async def list_voices(self) -> tuple[engine_pb2.PresetVoice, ...]:
        response = await self._worker.stub.ListVoices(
            engine_pb2.ListVoicesRequest(model_id=self._loaded_model_id),
            metadata=self._metadata(),
            timeout=VOICE_LIST_DEADLINE_SECONDS,
        )
        if response.HasField("error"):
            raise WorkerOperationError(response.error)
        return tuple(response.voices)

    async def validate_reference(
        self, reference_path: str, model_id: str, transcript: str | None
    ) -> engine_pb2.ValidateReferenceResponse:
        self._ensure_loaded_model(model_id)
        request = engine_pb2.ValidateReferenceRequest(
            model_id=model_id,
            reference_path=reference_path,
        )
        if transcript is not None:
            request.transcript = transcript
        response = await self._worker.stub.ValidateReference(
            request,
            metadata=self._metadata(),
            timeout=REFERENCE_VALIDATION_DEADLINE_SECONDS,
        )
        if response.HasField("error"):
            raise WorkerOperationError(response.error)
        if not response.valid:
            _raise_worker_error(response.error, "Worker rejected the reference")
        return response

    async def align(self, request: engine_pb2.AlignRequest) -> engine_pb2.AlignResponse:
        self._ensure_loaded_model(request.model_id)
        if (
            not self.capabilities.supports("alignment")
            or self.capabilities.alignment is None
        ):
            raise WorkerOperationError(
                engine_pb2.WorkerError(
                    code="alignment_unavailable",
                    message="Worker does not advertise alignment capability",
                    retryable=False,
                )
            )
        response = await self._worker.stub.Align(
            request,
            metadata=self._metadata(),
            timeout=ALIGNMENT_DEADLINE_SECONDS,
        )
        if response.HasField("error"):
            raise WorkerOperationError(response.error)
        if not response.HasField("result"):
            raise WorkerOperationError(
                engine_pb2.WorkerError(
                    code="alignment_failed",
                    message="Worker returned a malformed alignment response",
                    retryable=False,
                )
            )
        malformed_reason = _alignment_result_error(response.result, request, self.capabilities.alignment)
        if malformed_reason is not None:
            raise WorkerOperationError(
                engine_pb2.WorkerError(
                    code="alignment_failed",
                    message=f"Worker returned a malformed alignment response: {malformed_reason}",
                    retryable=False,
                )
            )
        return response

    def synthesize(
        self, request: engine_pb2.SynthesizeRequest
    ) -> AsyncIterator[engine_pb2.SynthesisEvent]:
        self._ensure_loaded_model(request.model_id)
        if self._synthesis_active:
            raise WorkerCapacityError("one active synthesis is already running")
        self._synthesis_active = True
        call: _CancellableSynthesisCall | None = None
        try:
            call = self._worker.stub.Synthesize(
                request,
                metadata=self._metadata(),
                timeout=SYNTHESIS_DEADLINE_SECONDS,
            )
            stream = _LeaseSynthesisStream(self, call)
            self._synthesis_stream = stream
            return stream
        except BaseException:
            if call is not None:
                with suppress(BaseException):
                    call.cancel()
            self._synthesis_active = False
            raise

    def _ensure_model_identity(self, model_id: str) -> None:
        if self._model_id is not None and model_id != self._model_id:
            raise WorkerModelMismatchError(
                "operation names a different model than the active lease"
            )

    def _ensure_loaded_model(self, model_id: str) -> None:
        if self._model_id is None:
            raise RuntimeError("load_model must precede Worker operations")
        self._ensure_model_identity(model_id)

    def _release_synthesis(self, stream: _LeaseSynthesisStream) -> None:
        if self._synthesis_stream is not stream:
            return
        self._synthesis_stream = None
        self._synthesis_active = False

    async def aclose(self) -> None:
        """Close the active stream before the owning pool releases capacity."""
        stream = self._synthesis_stream
        if stream is not None:
            await stream.aclose()

    @property
    def _loaded_model_id(self) -> str:
        if self._model_id is None:
            raise RuntimeError("load_model must precede list_voices")
        return self._model_id

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        return (("x-tts-worker-token", self._worker.token),)


class _LeaseSynthesisStream(AsyncIterator[engine_pb2.SynthesisEvent]):
    """Release one lease's synthesis slot at every stream terminal path."""

    def __init__(self, lease: GrpcWorkerLease, call: _CancellableSynthesisCall) -> None:
        self._lease = lease
        self._call = call
        self._iterator = call.__aiter__()
        self._closed = False

    def __aiter__(self) -> _LeaseSynthesisStream:
        return self

    async def __anext__(self) -> engine_pb2.SynthesisEvent:
        try:
            return await self._iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._call.cancel()
        finally:
            self._lease._release_synthesis(self)


def _alignment_result_error(
    result: engine_pb2.AlignmentResult,
    request: engine_pb2.AlignRequest,
    capability: AlignmentCapability | None,
) -> str | None:
    """Return a safe reason when a Worker returns invalid alignment data."""
    if len(result.SerializeToString()) > _MAX_ALIGNMENT_RESULT_BYTES:
        return "result exceeds size limit"
    if result.schema_version != 1:
        return "unsupported schema version"
    if result.transcript != request.transcript:
        return "transcript identity mismatch"
    if result.sample_rate_hz <= 0 or result.total_frames <= 0:
        return "audio identity is invalid"
    if not result.unit or capability is None or result.unit not in capability.units:
        return "alignment unit is unsupported"
    if not result.aligner or capability.aligner != result.aligner:
        return "aligner identity mismatch"
    if not result.units or len(result.units) > _MAX_ALIGNMENT_UNITS:
        return "alignment units are empty or too numerous"

    transcript_bytes = request.transcript.encode("utf-8")
    previous_source_end = 0
    previous_frame_end = 0
    for unit in result.units:
        if not unit.text:
            return "alignment unit text is empty"
        if not 0 <= unit.source_start < unit.source_end <= len(transcript_bytes):
            return "source range is out of bounds"
        if unit.source_start < previous_source_end:
            return "source ranges are not ordered"
        try:
            source_text = transcript_bytes[unit.source_start : unit.source_end].decode("utf-8")
        except UnicodeDecodeError:
            return "source range is not valid UTF-8"
        if source_text != unit.text:
            return "source range does not identify unit text"
        if not 0 <= unit.start_frames < unit.end_frames <= result.total_frames:
            return "frame range is out of bounds"
        if unit.start_frames < previous_frame_end:
            return "frame ranges are not ordered"
        if not math.isfinite(unit.confidence) or not 0.0 <= unit.confidence <= 1.0:
            return "confidence is out of bounds"
        previous_source_end = unit.source_end
        previous_frame_end = unit.end_frames
    return None


def _raise_worker_error(error: engine_pb2.WorkerError, fallback: str) -> None:
    if error.code or error.message:
        raise WorkerOperationError(error)
    raise RuntimeError(fallback)


def build_synthesis_request(
    model_id: str,
    text: str,
    *,
    voice_id: str | None = None,
    reference_path: str | None = None,
    transcript: str | None = None,
    provider_config: tuple[str, str, str] | None = None,
    speed: float | None = None,
    pitch: float | None = None,
    volume: float | None = None,
) -> engine_pb2.SynthesizeRequest:
    """Build a synthesis request with exactly one Core-selected voice source."""
    if bool(voice_id) == bool(reference_path):
        raise ValueError("synthesis request requires exactly one voice source")

    request = engine_pb2.SynthesizeRequest(model_id=model_id, text=text)
    if provider_config is not None:
        request.provider.base_url, request.provider.model, request.provider.api_key = provider_config
    if any(value is not None for value in (speed, pitch, volume)):
        request.options.CopyFrom(
            engine_pb2.SynthesisOptions(
                **{
                    name: value
                    for name, value in (
                        ("speed", speed),
                        ("pitch", pitch),
                        ("volume", volume),
                    )
                    if value is not None
                }
            )
        )
    if voice_id:
        request.voice_id = voice_id
    else:
        request.reference.reference_path = reference_path
        if transcript is not None:
            request.reference.transcript = transcript
    return request
