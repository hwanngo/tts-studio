"""OpenAI-compatible speech HTTP adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from http import HTTPStatus
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from tts_studio.generation.domain import AlignmentJob, AlignmentState, GenerationState
from tts_studio.generation.service import (
    GenerationArtifactInvalidError,
    GenerationArtifactNotFoundError,
    GenerationCapabilityError,
    GenerationJobNotFoundError,
    GenerationModelNotFoundError,
    GenerationRequestError,
    GenerationService,
    GenerationVoiceNotFoundError,
)
from tts_studio.providers.limits import MAX_SYNTHESIS_TEXT_CHARS
from tts_studio.server.errors import ErrorEnvelope, PublicApiError
from tts_studio.server.routes.generation import (
    AlignmentUnitResponse,
    _alignment_unit_response,
    _ArtifactHandleOwner,
    _ArtifactStreamingResponse,
    _open_artifact_for_streaming,
    _snapshot_size,
)
from tts_studio.voices.registry import SavedVoiceNotFoundError
from tts_studio.workers.generation import WorkerOperationError

router = APIRouter(prefix="/v1", tags=["openai-compatible"])

_MAX_JSON_AUDIO_BYTES = 32 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_JSON_ALIGNMENT_UNITS = 10_000
_ALIGNMENT_WAIT_SECONDS = 30.0
_ALIGNMENT_POLL_SECONDS = 0.01


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    input: str = Field(min_length=1, max_length=MAX_SYNTHESIS_TEXT_CHARS)
    voice: str = Field(min_length=1)
    response_format: str = Field(
        default="wav",
        json_schema_extra={"enum": ["wav", "json"]},
    )


class SpeechJsonResponse(BaseModel):
    schema_version: int
    job_id: str
    artifact_id: str
    transcript: str
    sample_rate_hz: int
    total_frames: int
    unit: str
    aligner: str
    units: list[AlignmentUnitResponse]
    audio: str
    audio_format: Literal["wav"]
    media_type: Literal["audio/wav"]
    byte_size: int
    encoding: Literal["base64"]


class OpenAiSpeechRequestError(PublicApiError):
    def __init__(self, message: str) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="unsupported_speech_request",
            message=message,
            source="openai_compatibility",
        )


class OpenAiSpeechModelError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="model_not_found",
            message="The requested speech model was not found.",
            source="openai_compatibility",
        )


class OpenAiSpeechVoiceError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="voice_not_found",
            message="The requested speech voice was not found.",
            source="openai_compatibility",
        )


class OpenAiSpeechCapabilityError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="capability_unsupported",
            message="The selected Worker does not support speech generation.",
            source="openai_compatibility",
            retryable=True,
        )


class OpenAiSpeechWorkerError(PublicApiError):
    def __init__(self, code: str = "worker_unavailable", *, retryable: bool = True) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code=code or "worker_unavailable",
            message="The engine Worker could not complete speech generation.",
            source="worker",
            retryable=retryable,
        )


class OpenAiSpeechArtifactError(PublicApiError):
    def __init__(self, code: str, *, status_code: int = HTTPStatus.UNPROCESSABLE_ENTITY) -> None:
        super().__init__(
            status_code=status_code,
            code=code,
            message="The retained Audio Artifact could not be used.",
            source="generation",
        )


@router.post(
    "/audio/speech",
    response_model=SpeechJsonResponse,
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/SpeechJsonResponse"}},
                "audio/wav": {"schema": {"type": "string", "format": "binary"}},
            }
        },
        422: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        503: {"model": ErrorEnvelope},
    },
)
async def create_speech(
    payload: SpeechRequest, request: Request
) -> Response:
    if payload.response_format not in {"wav", "json"}:
        raise OpenAiSpeechRequestError("Only response_format='wav' or 'json' is supported locally.")
    service: GenerationService = request.app.state.generation_service
    saved_voice_id: str | None = None
    try:
        request.app.state.saved_voice_service.get(payload.voice)
    except SavedVoiceNotFoundError:
        voice_id: str | None = payload.voice
    else:
        voice_id = None
        saved_voice_id = payload.voice
    try:
        job = await service.create(
            model_id=payload.model,
            voice_id=voice_id,
            saved_voice_id=saved_voice_id,
            text=payload.input,
            retain_artifact=True,
            correlation_id=request.state.correlation_id,
        )
        completed = await service.wait(job.id)
        completed_state = getattr(completed, "state", GenerationState.COMPLETED)
        if getattr(completed_state, "value", completed_state) != GenerationState.COMPLETED.value or completed.artifact_id is None:
            failure = getattr(completed, "error", None) or {}
            raise OpenAiSpeechWorkerError(
                str(failure.get("code", "worker_unavailable")),
                retryable=bool(failure.get("retryable", True)),
            )
        artifact_id = completed.artifact_id
    except GenerationModelNotFoundError as error:
        raise OpenAiSpeechModelError from error
    except GenerationVoiceNotFoundError as error:
        raise OpenAiSpeechVoiceError from error
    except GenerationRequestError as error:
        raise OpenAiSpeechRequestError(str(error)) from error
    except GenerationCapabilityError as error:
        raise OpenAiSpeechCapabilityError from error
    except WorkerOperationError as error:
        raise OpenAiSpeechWorkerError(error.code, retryable=error.retryable) from error
    except (GenerationJobNotFoundError, GenerationArtifactNotFoundError) as error:
        raise OpenAiSpeechRequestError("Speech generation did not produce an audio artifact.") from error

    if payload.response_format == "wav":
        try:
            handle = await _open_artifact_for_streaming(service, artifact_id)
        except GenerationArtifactNotFoundError as error:
            raise OpenAiSpeechRequestError("Speech generation did not produce an audio artifact.") from error
        return _ArtifactStreamingResponse(
            handle,
            media_type="audio/wav",
            headers={"Content-Disposition": f'attachment; filename="{job.id}.wav"'},
        )

    try:
        await service.request_alignment(job.id, request.state.correlation_id)
        alignment = await _wait_for_alignment(service, job.id)
    except GenerationArtifactNotFoundError as error:
        raise OpenAiSpeechArtifactError("artifact_not_found", status_code=HTTPStatus.NOT_FOUND) from error
    except GenerationArtifactInvalidError as error:
        raise OpenAiSpeechArtifactError("artifact_invalid") from error
    except GenerationCapabilityError as error:
        raise OpenAiSpeechWorkerError("alignment_unavailable", retryable=True) from error
    except GenerationRequestError as error:
        raise OpenAiSpeechRequestError(str(error)) from error
    except WorkerOperationError as error:
        raise OpenAiSpeechWorkerError(error.code, retryable=error.retryable) from error
    if alignment.state is not AlignmentState.COMPLETED or alignment.result is None:
        failure = alignment.error or {}
        raise OpenAiSpeechWorkerError(
            str(failure.get("code", "alignment_failed")),
            retryable=bool(failure.get("retryable", False)),
        )

    result = alignment.result
    if len(result.units) > _MAX_JSON_ALIGNMENT_UNITS:
        raise OpenAiSpeechRequestError("The speech response is too large.")
    try:
        audio = await _read_bounded_artifact(service, artifact_id)
    except GenerationArtifactNotFoundError as error:
        raise OpenAiSpeechArtifactError("artifact_not_found", status_code=HTTPStatus.NOT_FOUND) from error
    except GenerationArtifactInvalidError as error:
        raise OpenAiSpeechArtifactError("artifact_invalid") from error
    except OSError as error:
        raise OpenAiSpeechArtifactError("artifact_invalid") from error
    encoded_size = 4 * ((len(audio) + 2) // 3)
    if encoded_size > _MAX_JSON_RESPONSE_BYTES:
        raise OpenAiSpeechRequestError("The speech response is too large.")
    response = SpeechJsonResponse(
        schema_version=result.schema_version,
        job_id=result.job_id,
        artifact_id=result.artifact_id,
        transcript=result.transcript,
        sample_rate_hz=result.sample_rate_hz,
        total_frames=result.total_frames,
        unit=result.unit,
        aligner=result.aligner,
        units=[_alignment_unit_response(unit, result.sample_rate_hz) for unit in result.units],
        audio=base64.b64encode(audio).decode("ascii"),
        audio_format="wav",
        media_type="audio/wav",
        byte_size=len(audio),
        encoding="base64",
    )
    if len(response.model_dump_json().encode("utf-8")) > _MAX_JSON_RESPONSE_BYTES:
        raise OpenAiSpeechRequestError("The speech response is too large.")
    return JSONResponse(content=response.model_dump(mode="json"))


async def _read_bounded_artifact(service: GenerationService, artifact_id: str) -> bytes:
    metadata = next((item for item in service.list_history() if item.id == artifact_id), None)
    if metadata is None:
        raise GenerationArtifactNotFoundError
    if metadata.byte_size > _MAX_JSON_AUDIO_BYTES:
        raise OpenAiSpeechRequestError("The speech response is too large.")
    encoded_size = 4 * ((metadata.byte_size + 2) // 3)
    if encoded_size > _MAX_JSON_RESPONSE_BYTES:
        raise OpenAiSpeechRequestError("The speech response is too large.")
    handle = await _open_artifact_for_streaming(service, artifact_id)
    owner = _ArtifactHandleOwner(handle)
    try:
        size = await run_in_threadpool(_snapshot_size, owner)
        if size > _MAX_JSON_AUDIO_BYTES:
            raise OpenAiSpeechRequestError("The speech response is too large.")
        encoded_size = 4 * ((size + 2) // 3)
        if encoded_size > _MAX_JSON_RESPONSE_BYTES:
            raise OpenAiSpeechRequestError("The speech response is too large.")
        audio = await run_in_threadpool(_read_exact, owner, size)
        if (
            len(audio) != size
            or size != metadata.byte_size
            or hashlib.sha256(audio).hexdigest() != metadata.sha256
        ):
            raise GenerationArtifactInvalidError("The artifact changed while it was being read.")
        return audio
    finally:
        await owner.close()


def _read_exact(handle: _ArtifactHandleOwner, size: int) -> bytes:
    handle.seek(0)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = handle.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


async def _wait_for_alignment(service: GenerationService, job_id: str) -> AlignmentJob:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ALIGNMENT_WAIT_SECONDS
    while True:
        alignment = service.get_alignment(job_id)
        if alignment is None:
            raise OpenAiSpeechWorkerError("alignment_failed", retryable=True)
        if alignment.state in {AlignmentState.COMPLETED, AlignmentState.FAILED}:
            return alignment
        if loop.time() >= deadline:
            raise OpenAiSpeechWorkerError("alignment_deadline_exceeded", retryable=True)
        await asyncio.sleep(_ALIGNMENT_POLL_SECONDS)
