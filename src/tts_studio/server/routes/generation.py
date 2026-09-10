"""Public HTTP adapters for Core-owned Generation Jobs and Audio Artifacts."""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from threading import Lock
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from tts_studio.generation.domain import (
    AlignmentJob,
    AlignmentResult,
    AlignmentUnit,
    AudioArtifact,
    GenerationJob,
    SynthesisOptions,
)
from tts_studio.generation.service import (
    GenerationArtifactDeletionError,
    GenerationArtifactInvalidError,
    GenerationArtifactNotFoundError,
    GenerationCapabilityError,
    GenerationJobNotFoundError,
    GenerationModelNotFoundError,
    GenerationReferenceNotFoundError,
    GenerationRequestError,
    GenerationService,
    GenerationVoiceNotFoundError,
)
from tts_studio.server.errors import ErrorEnvelope, PublicApiError
from tts_studio.workers.generation import WorkerOperationError

router = APIRouter(prefix="/api/v1", tags=["generation"])
_LOGGER = logging.getLogger(__name__)


class _ArtifactHandleOwner:
    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._close_task: asyncio.Task[None] | None = None
        self._lock = Lock()

    def read(self, size: int) -> bytes:
        return self._handle.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        return self._handle.tell()

    async def close(self) -> None:
        with self._lock:
            close_task = self._close_task
            if close_task is None:
                close_task = asyncio.create_task(run_in_threadpool(self._handle.close))
                close_task.add_done_callback(_consume_background_exception)
                self._close_task = close_task
        await asyncio.shield(close_task)


class _ArtifactStreamingResponse(StreamingResponse):
    def __init__(
        self,
        handle: Any,
        *,
        start: int = 0,
        length: int | None = None,
        **kwargs: Any,
    ) -> None:
        self._artifact_owner = (
            handle if isinstance(handle, _ArtifactHandleOwner) else _ArtifactHandleOwner(handle)
        )
        super().__init__(
            _stream_artifact(self._artifact_owner, start=start, length=length),
            **kwargs,
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        try:
            await super().__call__(*args, **kwargs)
        finally:
            await self._artifact_owner.close()


async def _adopt_and_close_artifact(open_task: asyncio.Task[Any]) -> None:
    try:
        handle = await asyncio.shield(open_task)
    except asyncio.CancelledError:
        return
    except Exception:
        _LOGGER.debug(
            "Artifact open failed after its request was cancelled.",
            exc_info=True,
        )
        return
    try:
        await asyncio.shield(run_in_threadpool(handle.close))
    except Exception:
        _LOGGER.warning(
            "Artifact snapshot cleanup failed after request cancellation.",
            exc_info=True,
        )


async def _open_artifact_for_streaming(
    service: GenerationService,
    artifact_id: str,
) -> Any:
    open_task = asyncio.create_task(
        asyncio.to_thread(service.open_artifact, artifact_id)
    )
    try:
        return await asyncio.shield(open_task)
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(_adopt_and_close_artifact(open_task))
        cleanup_task.add_done_callback(_consume_background_exception)
        raise


def _consume_background_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except BaseException:
        _LOGGER.warning("Artifact cancellation cleanup task failed.", exc_info=True)


class VoiceResponse(BaseModel):
    id: str
    label: str
    capabilities: list[str]


class GenerationRequest(BaseModel):
    model_id: str
    voice_id: str | None = None
    reference_id: str | None = None
    saved_voice_id: str | None = None
    text: str
    retain_artifact: bool | None = None
    speed: float | None = Field(
        default=None,
        allow_inf_nan=False,
        json_schema_extra={"minimum": 0.25, "maximum": 4.0},
    )
    pitch: float | None = Field(
        default=None,
        allow_inf_nan=False,
        json_schema_extra={"minimum": -1.0, "maximum": 1.0},
    )
    volume: float | None = Field(
        default=None,
        allow_inf_nan=False,
        json_schema_extra={"minimum": 0.0, "maximum": 2.0},
    )

    @field_validator("speed", "pitch", "volume", mode="before")
    @classmethod
    def reject_non_numeric_options(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("generation options must be JSON numbers")
        return value


class VoicePreviewRequest(BaseModel):
    model_id: str = Field(min_length=1)
    voice_id: str = Field(min_length=1)
    text: str = Field(max_length=500)


class AlignmentUnitResponse(BaseModel):
    text: str
    source_start: int
    source_end: int
    start_ms: float
    end_ms: float
    confidence: float
    estimated: bool


class AlignmentResultResponse(BaseModel):
    schema_version: int
    job_id: str
    artifact_id: str
    transcript: str
    sample_rate_hz: int
    total_frames: int
    unit: str
    aligner: str
    units: list[AlignmentUnitResponse]


class AlignmentErrorResponse(BaseModel):
    code: str
    retryable: bool


class AlignmentResponse(BaseModel):
    job_id: str
    artifact_id: str
    state: str
    result: AlignmentResultResponse | None
    error: AlignmentErrorResponse | None
    created_at: str
    updated_at: str


class GenerationJobResponse(BaseModel):
    id: str
    model_id: str
    engine_id: str
    voice_id: str | None
    reference_id: str | None
    saved_voice_id: str | None
    text: str
    retain_artifact: bool
    state: str
    bytes_written: int
    frame_count: int
    sample_rate: int | None
    channel_count: int | None
    artifact_id: str | None
    artifact_url: str | None
    correlation_id: str
    cancellation_requested: bool
    error: dict[str, Any] | None
    created_at: str
    updated_at: str


class AudioArtifactResponse(BaseModel):
    id: str
    job_id: str
    byte_size: int
    sha256: str
    sample_rate: int
    channel_count: int
    frame_count: int
    duration_ms: int | None
    created_at: str
    retained_at: str
    audio_url: str


class GenerationModelNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="model_not_found",
            message="The Model Installation was not found.",
            source="model_registry",
        )


class GenerationNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="generation_not_found",
            message="The Generation Job was not found.",
            source="generation",
        )


class VoiceNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="voice_not_found",
            message="The requested runtime Voice was not reported by the Worker.",
            source="generation",
        )


class GenerationRequestInvalidApiError(PublicApiError):
    def __init__(self, message: str = "The generation request is invalid.") -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="generation_request_invalid",
            message=message,
            source="generation",
        )


class ReferenceRequestInvalidApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="reference_request_invalid",
            message="Provide exactly one of voice_id or reference_id.",
            source="generation",
        )


class GenerationReferenceNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="reference_not_found",
            message="The Reference Recording was not found.",
            source="references",
        )


class GenerationCapabilityUnsupportedApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="capability_unsupported",
            message="The selected Worker does not support speech generation.",
            source="generation",
            retryable=True,
        )


class GenerationArtifactNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="artifact_not_found",
            message="The Audio Artifact was not found.",
            source="generation",
        )


class AlignmentRequestInvalidApiError(PublicApiError):
    def __init__(self, message: str = "The alignment request is invalid.") -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="alignment_request_invalid",
            message=message,
            source="generation",
        )


class AlignmentCapabilityUnsupportedApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="alignment_unavailable",
            message="The selected Worker does not support alignment.",
            source="generation",
            retryable=True,
        )


class AlignmentArtifactInvalidApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="artifact_invalid",
            message="The retained Audio Artifact is invalid.",
            source="generation",
        )


class AlignmentWorkerErrorApiError(PublicApiError):
    def __init__(self, error: WorkerOperationError) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code=error.code or "alignment_failed",
            message="The engine Worker could not complete alignment.",
            source="worker",
            retryable=error.retryable,
        )


class AlignmentNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="alignment_not_found",
            message="Alignment has not been requested for this Generation Job.",
            source="generation",
        )


class GenerationArtifactDeleteFailedApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="artifact_delete_failed",
            message="The Audio Artifact could not be deleted safely.",
            source="generation",
        )


class GenerationArtifactRangeApiError(PublicApiError):
    def __init__(self, size: int) -> None:
        super().__init__(
            status_code=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
            code="artifact_range_invalid",
            message="The requested audio byte range is not satisfiable.",
            source="generation",
            headers={"Content-Range": f"bytes */{size}"},
        )


class GenerationWorkerUnavailableApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="worker_unavailable",
            message="The engine Worker could not complete the generation request.",
            source="worker",
            retryable=True,
        )


class PreviewWorkerErrorApiError(PublicApiError):
    def __init__(self, error: WorkerOperationError) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code=error.code or "worker_unavailable",
            message="The engine Worker could not complete the voice preview.",
            source="worker",
            retryable=error.retryable,
        )


class VoiceListWorkerErrorApiError(PublicApiError):
    def __init__(self, error: WorkerOperationError) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code=error.code or "worker_unavailable",
            message="The engine Worker could not list runtime Voices.",
            source="worker",
            retryable=error.retryable,
        )


_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope}
}
_GENERATION_ERRORS: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope},
    int(HTTPStatus.UNPROCESSABLE_ENTITY): {"model": ErrorEnvelope},
    int(HTTPStatus.SERVICE_UNAVAILABLE): {"model": ErrorEnvelope},
}
_ARTIFACT_DELETE_ERRORS: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope},
    int(HTTPStatus.SERVICE_UNAVAILABLE): {"model": ErrorEnvelope},
}
_ARTIFACT_DOWNLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope},
    int(HTTPStatus.PARTIAL_CONTENT): {
        "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}
    },
    int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE): {"model": ErrorEnvelope},
}


@router.get("/voices", response_model=list[VoiceResponse], responses=_GENERATION_ERRORS)
async def list_voices(
    request: Request,
    model_id: str = Query(..., min_length=1),
) -> list[VoiceResponse]:
    service: GenerationService = request.app.state.generation_service
    try:
        voices = await service.list_voices(model_id)
    except GenerationModelNotFoundError as error:
        raise GenerationModelNotFoundApiError from error
    except GenerationCapabilityError as error:
        raise GenerationCapabilityUnsupportedApiError from error
    except WorkerOperationError as error:
        raise VoiceListWorkerErrorApiError(error) from error
    return [
        VoiceResponse(id=voice.id, label=voice.label, capabilities=list(voice.capabilities))
        for voice in voices
    ]


@router.post(
    "/voices/preview",
    response_class=Response,
    responses={
        int(HTTPStatus.OK): {
            "content": {
                "audio/wav": {
                    "schema": {"type": "string", "format": "binary"},
                }
            }
        },
        **_GENERATION_ERRORS,
    },
)
async def preview_voice(payload: VoicePreviewRequest, request: Request) -> Response:
    service: GenerationService = request.app.state.generation_service
    try:
        audio = await service.preview(
            model_id=payload.model_id,
            voice_id=payload.voice_id,
            text=payload.text,
        )
    except GenerationModelNotFoundError as error:
        raise GenerationModelNotFoundApiError from error
    except GenerationVoiceNotFoundError as error:
        raise VoiceNotFoundApiError from error
    except GenerationRequestError as error:
        raise GenerationRequestInvalidApiError(str(error)) from error
    except GenerationCapabilityError as error:
        raise GenerationCapabilityUnsupportedApiError from error
    except WorkerOperationError as error:
        raise PreviewWorkerErrorApiError(error) from error
    return Response(content=audio, media_type="audio/wav")


@router.post(
    "/generations",
    response_model=GenerationJobResponse,
    status_code=HTTPStatus.ACCEPTED,
    responses=_GENERATION_ERRORS,
)
async def create_generation(
    payload: GenerationRequest, request: Request
) -> GenerationJobResponse:
    if (
        (payload.voice_id is not None and not payload.voice_id)
        or (payload.reference_id is not None and not payload.reference_id)
        or (payload.saved_voice_id is not None and not payload.saved_voice_id)
        or sum(value is not None for value in (payload.voice_id, payload.reference_id, payload.saved_voice_id)) != 1
    ):
        raise ReferenceRequestInvalidApiError
    service: GenerationService = request.app.state.generation_service
    try:
        job = await service.create(
            model_id=payload.model_id,
            voice_id=payload.voice_id,
            reference_id=payload.reference_id,
            saved_voice_id=payload.saved_voice_id,
            text=payload.text,
            retain_artifact=payload.retain_artifact,
            correlation_id=request.state.correlation_id,
            options=SynthesisOptions(speed=payload.speed, pitch=payload.pitch, volume=payload.volume)
            if any(value is not None for value in (payload.speed, payload.pitch, payload.volume)) else None,
        )
    except GenerationModelNotFoundError as error:
        raise GenerationModelNotFoundApiError from error
    except GenerationVoiceNotFoundError as error:
        raise VoiceNotFoundApiError from error
    except GenerationReferenceNotFoundError as error:
        raise GenerationReferenceNotFoundApiError from error
    except GenerationRequestError as error:
        raise GenerationRequestInvalidApiError(str(error)) from error
    except GenerationCapabilityError as error:
        raise GenerationCapabilityUnsupportedApiError from error
    except WorkerOperationError as error:
        raise GenerationWorkerUnavailableApiError from error
    return _job_response(job)


@router.get(
    "/generations",
    response_model=list[GenerationJobResponse],
    responses=_NOT_FOUND,
)
async def list_generations(request: Request) -> list[GenerationJobResponse]:
    service: GenerationService = request.app.state.generation_service
    return [_job_response(job) for job in service.list()]


@router.get(
    "/generations/{job_id}",
    response_model=GenerationJobResponse,
    responses=_NOT_FOUND,
)
async def get_generation(job_id: str, request: Request) -> GenerationJobResponse:
    service: GenerationService = request.app.state.generation_service
    try:
        return _job_response(service.get(job_id))
    except GenerationJobNotFoundError as error:
        raise GenerationNotFoundApiError from error


@router.post(
    "/generations/{job_id}/cancel",
    response_model=GenerationJobResponse,
    responses=_NOT_FOUND,
)
async def cancel_generation(job_id: str, request: Request) -> GenerationJobResponse:
    service: GenerationService = request.app.state.generation_service
    try:
        return _job_response(await service.cancel(job_id))
    except GenerationJobNotFoundError as error:
        raise GenerationNotFoundApiError from error


@router.post(
    "/generations/{job_id}/alignment",
    response_model=AlignmentResponse,
    responses=_GENERATION_ERRORS,
)
async def request_generation_alignment(job_id: str, request: Request) -> AlignmentResponse:
    service: GenerationService = request.app.state.generation_service
    try:
        alignment = await service.request_alignment(job_id, request.state.correlation_id)
    except GenerationJobNotFoundError as error:
        raise GenerationNotFoundApiError from error
    except GenerationArtifactNotFoundError as error:
        raise GenerationArtifactNotFoundApiError from error
    except GenerationArtifactInvalidError as error:
        raise AlignmentArtifactInvalidApiError from error
    except GenerationCapabilityError as error:
        raise AlignmentCapabilityUnsupportedApiError from error
    except GenerationRequestError as error:
        raise AlignmentRequestInvalidApiError(str(error)) from error
    except WorkerOperationError as error:
        raise AlignmentWorkerErrorApiError(error) from error
    return _alignment_response(alignment)


@router.get(
    "/generations/{job_id}/alignment",
    response_model=AlignmentResponse,
    responses=_GENERATION_ERRORS,
)
async def get_generation_alignment(job_id: str, request: Request) -> AlignmentResponse:
    service: GenerationService = request.app.state.generation_service
    try:
        alignment = service.get_alignment(job_id)
    except GenerationJobNotFoundError as error:
        raise GenerationNotFoundApiError from error
    if alignment is None:
        raise AlignmentNotFoundApiError
    return _alignment_response(alignment)


@router.get("/generations/{job_id}/pcm", response_class=StreamingResponse, responses=_NOT_FOUND)
async def stream_generation_pcm(job_id: str, request: Request) -> StreamingResponse:
    service: GenerationService = request.app.state.generation_service
    try:
        stream = service.subscribe_pcm(job_id)
        service.get(job_id)
    except GenerationJobNotFoundError as error:
        raise GenerationNotFoundApiError from error
    return StreamingResponse(stream, media_type="application/octet-stream", headers={"X-Audio-Sample-Rate": "48000", "X-Audio-Channels": "1", "X-Audio-Encoding": "s16le"})


@router.get(
    "/artifacts/{artifact_id}/audio",
    response_class=StreamingResponse,
    responses=_ARTIFACT_DOWNLOAD_RESPONSES,
)
async def download_artifact(artifact_id: str, request: Request) -> Response:
    service: GenerationService = request.app.state.generation_service
    try:
        handle = await _open_artifact_for_streaming(service, artifact_id)
    except GenerationArtifactNotFoundError as error:
        raise GenerationArtifactNotFoundApiError from error

    owner = _ArtifactHandleOwner(handle)
    try:
        size = await run_in_threadpool(_snapshot_size, owner)
    except BaseException:
        cleanup_task = asyncio.create_task(owner.close())
        cleanup_task.add_done_callback(_consume_background_exception)
        raise
    range_value = request.headers.get("range")
    byte_range = _parse_single_range(range_value, size)
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{artifact_id}.wav"',
    }
    if range_value is not None and byte_range is None:
        await owner.close()
        raise GenerationArtifactRangeApiError(size)

    if byte_range is None:
        status_code = HTTPStatus.OK
        start = 0
        length = size
    else:
        start, end = byte_range
        status_code = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        base_headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    base_headers["Content-Length"] = str(length)

    if request.method == "HEAD":
        await owner.close()
        return Response(status_code=status_code, headers=base_headers, media_type="audio/wav")

    return _ArtifactStreamingResponse(
        owner,
        start=start,
        length=length,
        status_code=status_code,
        media_type="audio/wav",
        headers=base_headers,
    )


@router.head(
    "/artifacts/{artifact_id}/audio",
    response_class=Response,
    responses=_NOT_FOUND,
    include_in_schema=False,
)
async def head_artifact(artifact_id: str, request: Request) -> Response:
    return await download_artifact(artifact_id, request)


def _snapshot_size(handle: Any) -> int:
    position = handle.tell()
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(position)
    return size


_MAX_RANGE_DIGITS = 20


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and len(value) <= _MAX_RANGE_DIGITS and value.isascii() and value.isdecimal()


def _parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None or not value.startswith("bytes=") or "," in value:
        return None
    spec = value[6:]
    if spec.count("-") != 1:
        return None
    first, last = spec.split("-", 1)
    if (first and not _is_ascii_digits(first)) or (last and not _is_ascii_digits(last)):
        return None
    if first == "" and last == "":
        return None
    if first == "":
        suffix_length = int(last)
        if suffix_length <= 0 or size == 0:
            return None
        return max(0, size - suffix_length), size - 1
    start = int(first)
    if start < 0 or start >= size:
        return None
    end = size - 1 if last == "" else int(last)
    if end < start:
        return None
    return start, min(end, size - 1)


def _stream_artifact(handle: Any, *, start: int = 0, length: int | None = None):
    handle.seek(start)
    remaining = length
    while remaining != 0:
        chunk_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
        chunk = handle.read(chunk_size)
        if not chunk:
            return
        yield chunk
        if remaining is not None:
            remaining -= len(chunk)


@router.get(
    "/history",
    response_model=list[AudioArtifactResponse],
    responses=_NOT_FOUND,
)
async def list_history(request: Request) -> list[AudioArtifactResponse]:
    service: GenerationService = request.app.state.generation_service
    return [_artifact_response(artifact) for artifact in service.list_history()]


@router.delete(
    "/history/{artifact_id}",
    status_code=HTTPStatus.NO_CONTENT,
    responses=_ARTIFACT_DELETE_ERRORS,
)
async def delete_history(artifact_id: str, request: Request) -> Response:
    service: GenerationService = request.app.state.generation_service
    try:
        deleted = await run_in_threadpool(service.delete_artifact, artifact_id)
    except GenerationArtifactNotFoundError as error:
        raise GenerationArtifactNotFoundApiError from error
    except GenerationArtifactDeletionError as error:
        raise GenerationArtifactDeleteFailedApiError from error
    if not deleted:
        raise GenerationArtifactNotFoundApiError
    return Response(status_code=HTTPStatus.NO_CONTENT)


def _job_response(job: GenerationJob) -> GenerationJobResponse:
    artifact_id = job.artifact_id if job.state.value == "completed" else None
    artifact_url = (
        f"/api/v1/artifacts/{quote(artifact_id, safe='')}/audio"
        if artifact_id is not None
        else None
    )
    return GenerationJobResponse(
        id=job.id,
        model_id=job.model_id,
        engine_id=job.engine_id,
        voice_id=job.voice_id,
        reference_id=job.reference_id,
        saved_voice_id=job.saved_voice_id,
        text=job.text,
        retain_artifact=job.retain_artifact,
        state=job.state.value,
        bytes_written=job.bytes_written,
        frame_count=job.frame_count,
        sample_rate=job.sample_rate,
        channel_count=job.channel_count,
        artifact_id=artifact_id,
        artifact_url=artifact_url,
        correlation_id=job.correlation_id,
        cancellation_requested=job.cancellation_requested,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _alignment_response(alignment: AlignmentJob) -> AlignmentResponse:
    result = alignment.result
    return AlignmentResponse(
        job_id=alignment.job_id,
        artifact_id=alignment.artifact_id,
        state=alignment.state.value,
        result=_alignment_result_response(result) if result is not None else None,
        error=(
            AlignmentErrorResponse(
                code=str(alignment.error.get("code", "alignment_failed")),
                retryable=bool(alignment.error.get("retryable", False)),
            )
            if alignment.error is not None
            else None
        ),
        created_at=alignment.created_at,
        updated_at=alignment.updated_at,
    )


def _alignment_result_response(result: AlignmentResult) -> AlignmentResultResponse:
    return AlignmentResultResponse(
        schema_version=result.schema_version,
        job_id=result.job_id,
        artifact_id=result.artifact_id,
        transcript=result.transcript,
        sample_rate_hz=result.sample_rate_hz,
        total_frames=result.total_frames,
        unit=result.unit,
        aligner=result.aligner,
        units=[_alignment_unit_response(unit, result.sample_rate_hz) for unit in result.units],
    )


def _alignment_unit_response(unit: AlignmentUnit, sample_rate_hz: int) -> AlignmentUnitResponse:
    start_ms, end_ms = unit.milliseconds(sample_rate_hz)
    return AlignmentUnitResponse(
        text=unit.text,
        source_start=unit.source_start,
        source_end=unit.source_end,
        start_ms=start_ms,
        end_ms=end_ms,
        confidence=unit.confidence,
        estimated=unit.estimated,
    )


def _artifact_response(artifact: AudioArtifact) -> AudioArtifactResponse:
    audio_url = f"/api/v1/artifacts/{quote(artifact.id, safe='')}/audio"
    return AudioArtifactResponse(
        id=artifact.id,
        job_id=artifact.job_id,
        byte_size=artifact.byte_size,
        sha256=artifact.sha256,
        sample_rate=artifact.sample_rate,
        channel_count=artifact.channel_count,
        frame_count=artifact.frame_count,
        duration_ms=artifact.duration_ms,
        created_at=artifact.created_at,
        retained_at=artifact.retained_at,
        audio_url=audio_url,
    )
