"""Implementation of the fake engine worker RPC service."""

import asyncio
import hashlib
import os
import struct
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import grpc
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_worker_sdk.auth import require_worker_token

from tts_studio_fake_worker.model_service import FakeModelService

_DEADLINE_WARNING_SECONDS = 0.005
_MAX_ALIGNMENT_TRANSCRIPT_CHARS = 2_000
_MAX_ALIGNMENT_UNITS = 10_000
_MAX_ALIGNMENT_RESULT_BYTES = 1 << 20


class FakeEngineWorker(engine_pb2_grpc.EngineWorkerServicer):
    """A deterministic engine adapter for exercising Core-to-Worker behavior."""

    def __init__(
        self,
        token: str,
        staging_root: Path | None = None,
        *,
        alignment_mode: str = "success",
    ) -> None:
        self._token = token
        self._models = FakeModelService(staging_root) if staging_root is not None else None
        self._alignment_mode = alignment_mode
        self._termination_errors: list[engine_pb2.WorkerError] = []

    @property
    def termination_errors(self) -> tuple[engine_pb2.WorkerError, ...]:
        return tuple(
            engine_pb2.WorkerError.FromString(error.SerializeToString())
            for error in self._termination_errors
        )

    async def Describe(
        self,
        request: engine_pb2.DescribeRequest,
        context: grpc.aio.ServicerContext[engine_pb2.DescribeRequest, engine_pb2.DescribeResponse],
    ) -> engine_pb2.DescribeResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
            engine_id="fake",
            engine_version="0.2.0",
            capabilities=[
                engine_pb2.Capability(name="health", supported=True),
                engine_pb2.Capability(name="model_validation", supported=True),
                engine_pb2.Capability(name="model_download", supported=True),
                engine_pb2.Capability(name="download_cancellation", supported=True),
                engine_pb2.Capability(name="model_lifecycle", supported=True),
                engine_pb2.Capability(name="preset_voices", supported=True),
                engine_pb2.Capability(name="streaming_synthesis", supported=True),
                engine_pb2.Capability(name="synthesis_cancellation", supported=True),
                engine_pb2.Capability(name="reference_cloning", supported=True),
                engine_pb2.Capability(name="alignment", supported=True),
            ],
            max_concurrency=1,
            alignment=engine_pb2.AlignmentCapability(
                units=["word"], languages=["und"], aligner="fake-aligner/1.0"
            ),
        )

    async def Health(
        self,
        request: engine_pb2.HealthRequest,
        context: grpc.aio.ServicerContext[engine_pb2.HealthRequest, engine_pb2.HealthResponse],
    ) -> engine_pb2.HealthResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.HealthResponse(status=engine_pb2.HealthResponse.READY)

    async def ValidateModel(
        self,
        request: engine_pb2.ValidateModelRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.ValidateModelRequest, engine_pb2.ValidateModelResponse
        ],
    ) -> engine_pb2.ValidateModelResponse:
        await require_worker_token(context, self._token)
        if self._models is None:
            return engine_pb2.ValidateModelResponse(
                compatible=False,
                engine_id="fake",
                engine_version="0.2.0",
                error=engine_pb2.WorkerError(
                    code="adapter_unavailable",
                    message="Fake adapter staging is not configured",
                    retryable=False,
                ),
            )
        return self._models.validate_model(request)

    async def ValidateReference(
        self,
        request: engine_pb2.ValidateReferenceRequest,
        context: grpc.aio.ServicerContext,
    ) -> engine_pb2.ValidateReferenceResponse:
        await require_worker_token(context, self._token)
        return self._generation_service().validate_reference(request)

    async def DownloadModel(
        self,
        request: engine_pb2.DownloadModelRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.DownloadModelRequest, engine_pb2.DownloadModelEvent
        ],
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        await require_worker_token(context, self._token)
        if self._models is None:
            yield engine_pb2.DownloadModelEvent(
                error=engine_pb2.WorkerError(
                    code="adapter_unavailable",
                    message="Fake adapter staging is not configured",
                    retryable=False,
                )
            )
            return
        async for event in self._models.download_model(request):
            yield event

    async def LoadModel(
        self,
        request: engine_pb2.LoadModelRequest,
        context: grpc.aio.ServicerContext,
    ) -> engine_pb2.LoadModelResponse:
        await require_worker_token(context, self._token)
        return self._generation_service().load_model(request)

    async def UnloadModel(
        self,
        request: engine_pb2.UnloadModelRequest,
        context: grpc.aio.ServicerContext,
    ) -> engine_pb2.UnloadModelResponse:
        await require_worker_token(context, self._token)
        return self._generation_service().unload_model(request.model_id)

    async def ListVoices(
        self,
        request: engine_pb2.ListVoicesRequest,
        context: grpc.aio.ServicerContext,
    ) -> engine_pb2.ListVoicesResponse:
        await require_worker_token(context, self._token)
        return self._generation_service().list_voices(request.model_id)

    async def Align(
        self,
        request: engine_pb2.AlignRequest,
        context: grpc.aio.ServicerContext,
    ) -> engine_pb2.AlignResponse:
        await require_worker_token(context, self._token)
        service = self._generation_service()
        if request.model_id not in service._loaded_models:
            return engine_pb2.AlignResponse(
                error=engine_pb2.WorkerError(
                    code="model_not_loaded",
                    message="The requested model is not loaded",
                    retryable=False,
                    details={"adapter": "fake"},
                )
            )
        if self._alignment_mode == "error":
            return engine_pb2.AlignResponse(error=_alignment_error("alignment_failed", "Fake alignment failed"))
        if self._alignment_mode == "timeout":
            return engine_pb2.AlignResponse(
                error=_alignment_error(
                    "alignment_deadline_exceeded",
                    "Alignment deadline exceeded",
                    retryable=True,
                )
            )
        if self._alignment_mode == "cancelled":
            return engine_pb2.AlignResponse(
                error=_alignment_error(
                    "alignment_cancelled", "Alignment was cancelled", retryable=False
                )
            )
        try:
            request.transcript.encode("utf-8")
        except UnicodeEncodeError:
            return engine_pb2.AlignResponse(
                error=_alignment_error("alignment_request_invalid", "Alignment transcript is invalid")
            )
        tokens = request.transcript.split()
        if (
            not request.transcript.strip()
            or "\0" in request.transcript
            or len(request.transcript) > _MAX_ALIGNMENT_TRANSCRIPT_CHARS
            or len(tokens) > _MAX_ALIGNMENT_UNITS
        ):
            return engine_pb2.AlignResponse(
                error=_alignment_error("alignment_request_invalid", "Alignment request exceeds limits")
            )
        try:
            with (
                service._open_audio(request.audio_path) as descriptor,
                os.fdopen(os.dup(descriptor), "rb") as audio_file,
                wave.open(audio_file, "rb") as stream,
            ):
                if (
                    stream.getnchannels() != 1
                    or stream.getsampwidth() != 2
                    or stream.getcomptype() != "NONE"
                ):
                    raise ValueError("unsupported audio")
                sample_rate = stream.getframerate()
                total_frames = stream.getnframes()
        except (OSError, ValueError, wave.Error):
            return engine_pb2.AlignResponse(error=_alignment_error("alignment_failed", "The audio file is unavailable"))
        result = _alignment_result(request.transcript, sample_rate, total_frames, tokens=tokens)
        if self._alignment_mode == "malformed":
            result.units.clear()
        if len(result.SerializeToString()) > _MAX_ALIGNMENT_RESULT_BYTES:
            return engine_pb2.AlignResponse(
                error=_alignment_error("alignment_failed", "Alignment result exceeds limits")
            )
        return engine_pb2.AlignResponse(result=result)

    async def Synthesize(
        self,
        request: engine_pb2.SynthesizeRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[engine_pb2.SynthesisEvent]:
        await require_worker_token(context, self._token)
        service = self._generation_service()
        try:
            reference_bytes = service.reference_bytes(request)
        except (OSError, ValueError):
            reference_bytes = None
        error = service.synthesize_error(request, reference_bytes=reference_bytes)
        if error is not None:
            yield engine_pb2.SynthesisEvent(error=error)
            return

        termination_recorded = [False]
        synthesis_completed = [False]
        context.add_done_callback(
            lambda done_context: self._record_done_termination(
                done_context, termination_recorded, synthesis_completed
            )
        )

        yield engine_pb2.SynthesisEvent(
            header=engine_pb2.AudioHeader(
                sample_rate_hz=48000,
                channels=1,
                sample_format=engine_pb2.S16LE,
            )
        )
        pcm = _deterministic_pcm(
            request,
            service.variant_for_model(request.model_id),
            reference_bytes,
        )
        chunk_size = 1920
        total_frames = len(pcm) // 2
        for sequence, offset in enumerate(range(0, len(pcm), chunk_size)):
            termination_error = _termination_error(context)
            if termination_error is not None:
                yield self._termination_event(termination_error, termination_recorded)
                if termination_error.code == "synthesis_deadline_exceeded":
                    await asyncio.sleep(0.001)
                    await context.abort(
                        grpc.StatusCode.DEADLINE_EXCEEDED,
                        termination_error.message,
                    )
                return
            await asyncio.sleep(0.001)
            termination_error = _termination_error(context)
            if termination_error is not None:
                yield self._termination_event(termination_error, termination_recorded)
                if termination_error.code == "synthesis_deadline_exceeded":
                    await asyncio.sleep(0.001)
                    await context.abort(
                        grpc.StatusCode.DEADLINE_EXCEEDED,
                        termination_error.message,
                    )
                return
            yield engine_pb2.SynthesisEvent(
                chunk=engine_pb2.PcmChunk(
                    sequence=sequence, pcm=pcm[offset : offset + chunk_size]
                )
            )
        yield engine_pb2.SynthesisEvent(
            progress=engine_pb2.SynthesisProgress(
                message="Fake synthesis complete", duration_frames=total_frames
            )
        )
        yield engine_pb2.SynthesisEvent(
            result=engine_pb2.SynthesisResult(
                total_frames=total_frames, duration_ms=round(total_frames / 48)
            )
        )
        synthesis_completed[0] = True

    def _generation_service(self) -> FakeModelService:
        if self._models is None:
            self._models = FakeModelService(None)
        return self._models

    def _termination_event(
        self, error: engine_pb2.WorkerError, termination_recorded: list[bool]
    ) -> engine_pb2.SynthesisEvent:
        self._record_termination_error(error, termination_recorded)
        return engine_pb2.SynthesisEvent(error=error)

    def _record_done_termination(
        self,
        context: grpc.aio.ServicerContext,
        termination_recorded: list[bool],
        synthesis_completed: list[bool],
    ) -> None:
        if synthesis_completed[0] or termination_recorded[0]:
            return
        error = _termination_error(context)
        if error is None:
            error = _cancelled_error()
        self._record_termination_error(error, termination_recorded)

    def _record_termination_error(
        self, error: engine_pb2.WorkerError, termination_recorded: list[bool]
    ) -> None:
        if termination_recorded[0]:
            return
        termination_recorded[0] = True
        snapshot = engine_pb2.WorkerError()
        snapshot.CopyFrom(error)
        self._termination_errors.append(snapshot)


def _deterministic_pcm(
    request: engine_pb2.SynthesizeRequest,
    variant: str,
    reference_bytes: bytes | None = None,
) -> bytes:
    if reference_bytes is not None:
        seed = hashlib.sha256(reference_bytes + request.text.encode("utf-8")).digest()
    else:
        seed = hashlib.sha256(
            f"fake|{request.model_id}|{variant}|{request.voice_id}|{request.text}".encode()
        ).digest()
    frames = max(480, min(48000 * 10, len(request.text.encode()) * 240))
    output = bytearray()
    for index in range(frames):
        sample = int.from_bytes(seed[(index % 16) : (index % 16) + 2], "little", signed=False)
        output.extend(struct.pack("<h", sample - 32768))
    return bytes(output)


def _alignment_error(
    code: str, message: str, *, retryable: bool = False
) -> engine_pb2.WorkerError:
    return engine_pb2.WorkerError(
        code=code,
        message=message,
        retryable=retryable,
        details={"adapter": "fake", "operation": "alignment"},
    )


def _alignment_result(
    transcript: str,
    sample_rate: int,
    total_frames: int,
    *,
    tokens: list[str] | None = None,
) -> engine_pb2.AlignmentResult:
    tokens = transcript.split() if tokens is None else tokens
    result = engine_pb2.AlignmentResult(
        schema_version=1,
        transcript=transcript,
        sample_rate_hz=sample_rate,
        total_frames=total_frames,
        unit="word",
        aligner="fake-aligner/1.0",
    )
    search_from = 0
    for index, token in enumerate(tokens):
        char_start = transcript.find(token, search_from)
        char_end = char_start + len(token)
        source_start = len(transcript[:char_start].encode("utf-8"))
        source_end = len(transcript[:char_end].encode("utf-8"))
        start_frames = total_frames * index // len(tokens)
        end_frames = total_frames * (index + 1) // len(tokens)
        result.units.append(
            engine_pb2.AlignmentUnit(
                text=token,
                source_start=source_start,
                source_end=source_end,
                start_frames=start_frames,
                end_frames=end_frames,
                confidence=1.0,
                estimated=True,
            )
        )
        search_from = char_end
    return result


def _termination_error(context: grpc.aio.ServicerContext) -> engine_pb2.WorkerError | None:
    remaining = context.time_remaining()
    if remaining is not None and remaining <= _DEADLINE_WARNING_SECONDS:
        return engine_pb2.WorkerError(
            code="synthesis_deadline_exceeded",
            message="Synthesis deadline exceeded",
            retryable=True,
            details={"adapter": "fake", "terminal_status": "DEADLINE_EXCEEDED"},
        )
    if context.cancelled():
        return _cancelled_error()
    return None


def _cancelled_error() -> engine_pb2.WorkerError:
    return engine_pb2.WorkerError(
        code="synthesis_cancelled",
        message="Synthesis was cancelled",
        retryable=False,
        details={"adapter": "fake", "terminal_status": "CANCELLED"},
    )
