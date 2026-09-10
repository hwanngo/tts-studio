"""Native HTTP adapters for transient Reference Recordings."""

from __future__ import annotations

import re
import tempfile
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from tts_studio.models.registry import RegistryRecordNotFoundError
from tts_studio.references.domain import (
    CleanupFailedError,
    ReferenceInUseError,
    UploadTooLargeError,
)
from tts_studio.references.registry import ReferenceRecordNotFoundError
from tts_studio.references.service import ReferenceService
from tts_studio.server.errors import ErrorEnvelope, PublicApiError
from tts_studio.storage.layout import ReferenceStorageUnavailableError
from tts_studio.voices.domain import SavedVoice
from tts_studio.voices.registry import SavedVoiceNotFoundError
from tts_studio.voices.service import SavedVoiceDeletionError
from tts_studio.workers.generation import WorkerOperationError

router = APIRouter(prefix="/api/v1", tags=["references"])


class ReferenceEvidence(BaseModel):
    code: str
    message: str


class ReferenceResponse(BaseModel):
    id: str
    model_id: str
    byte_size: int
    sha256: str
    container: str
    sample_rate_hz: int
    channels: int
    duration_ms: int
    transcript_present: bool
    state: str
    evidence: list[ReferenceEvidence]
    expires_at: str


class SavedVoiceResponse(BaseModel):
    id: str
    model_id: str
    label: str
    transcript_present: bool
    created_at: str


class SavedVoiceRequest(BaseModel):
    model_id: str
    label: str
    reference_id: str


_REFERENCE_ERRORS: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.CONFLICT): {"model": ErrorEnvelope},
    int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope},
    int(HTTPStatus.UNPROCESSABLE_ENTITY): {"model": ErrorEnvelope},
    int(HTTPStatus.SERVICE_UNAVAILABLE): {"model": ErrorEnvelope},
}


@router.get("/saved-voices", response_model=list[SavedVoiceResponse])
async def list_saved_voices(request: Request, model_id: str) -> list[SavedVoiceResponse]:
    service = request.app.state.saved_voice_service
    return [_saved_voice_response(voice) for voice in service.list_for_model(model_id)]


@router.post("/saved-voices", response_model=SavedVoiceResponse, status_code=HTTPStatus.CREATED)
async def create_saved_voice(payload: SavedVoiceRequest, request: Request) -> SavedVoiceResponse:
    try:
        voice = request.app.state.saved_voice_service.create_from_reference(
            model_id=payload.model_id, label=payload.label, reference_id=payload.reference_id
        )
    except (ValueError, SavedVoiceNotFoundError) as error:
        raise ReferenceRequestInvalidApiError(str(error)) from error
    return _saved_voice_response(voice)


@router.delete(
    "/saved-voices/{voice_id}",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope},
        int(HTTPStatus.SERVICE_UNAVAILABLE): {"model": ErrorEnvelope},
    },
)
async def delete_saved_voice(voice_id: str, request: Request) -> None:
    try:
        request.app.state.saved_voice_service.delete(voice_id)
    except SavedVoiceNotFoundError as error:
        raise ReferenceNotFoundApiError from error
    except SavedVoiceDeletionError as error:
        raise ReferenceCleanupFailedApiError from error


def _saved_voice_response(voice: SavedVoice) -> SavedVoiceResponse:
    return SavedVoiceResponse(
        id=voice.id, model_id=voice.model_id, label=voice.label,
        transcript_present=voice.transcript is not None, created_at=voice.created_at,
    )


class ReferenceRequestInvalidApiError(PublicApiError):
    def __init__(self, message: str = "A model_id and one audio file are required.") -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="reference_request_invalid",
            message=message,
            source="references",
        )


class ReferenceInvalidApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="reference_invalid",
            message="The Reference Recording is invalid.",
            source="references",
        )


class ReferenceTooLargeApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="reference_request_invalid",
            message="The Reference Recording exceeds the 20 MiB limit.",
            source="references",
        )


class ReferenceNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="reference_not_found",
            message="The Reference Recording was not found.",
            source="references",
        )


class ReferenceCapabilityUnsupportedApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="reference_capability_unsupported",
            message="The selected Worker does not support reference cloning.",
            source="references",
            retryable=True,
        )


class ReferenceStorageUnsupportedApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="reference_storage_unsupported",
            message="Reference Recording storage is unavailable on this platform.",
            source="references",
            retryable=True,
        )


class ReferenceWorkerUnavailableApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="worker_unavailable",
            message="The engine Worker could not validate the Reference Recording.",
            source="worker",
            retryable=True,
        )


class ReferenceCleanupFailedApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="reference_cleanup_failed",
            message="The Reference Recording could not be safely removed.",
            source="references",
            retryable=True,
        )


class ReferenceInUseApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.CONFLICT,
            code="reference_in_use",
            message="The Reference Recording is owned by an active Generation Job.",
            source="references",
            retryable=True,
        )


@router.post(
    "/references",
    response_model=ReferenceResponse,
    status_code=HTTPStatus.CREATED,
    responses=_REFERENCE_ERRORS,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["model_id", "file"],
                        "properties": {
                            "model_id": {"type": "string"},
                            "transcript": {"type": "string"},
                            "file": {"type": "string", "format": "binary"},
                        },
                    }
                }
            },
        }
    },
)
async def upload_reference(
    request: Request,
) -> ReferenceResponse:
    try:
        upload = await _parse_multipart(request)
    except UploadTooLargeError as error:
        raise ReferenceTooLargeApiError from error
    except ValueError as error:
        raise ReferenceRequestInvalidApiError(str(error)) from error
    model_id = upload.model_id
    transcript = upload.transcript
    if not model_id or upload.file is None:
        upload.close()
        raise ReferenceRequestInvalidApiError
    try:
        model = request.app.state.model_registry.get_model(model_id)
    except RegistryRecordNotFoundError as error:
        upload.close()
        raise ReferenceRequestInvalidApiError(
            "The model_id is not an installed Model Installation."
        ) from error

    reference_service: ReferenceService = request.app.state.reference_service
    recording = None
    try:
        async with request.app.state.supervisor.acquire(model) as lease:
            await lease.load_model(model)
            try:
                if not lease.capabilities.supports("reference_cloning"):
                    raise ReferenceCapabilityUnsupportedApiError
                upload.file.seek(0)
                recording = reference_service.create_upload(
                    model_id=model.id,
                    payload=upload.file,
                    transcript=transcript,
                )
                try:
                    validation = await lease.validate_reference(
                        recording.relative_path, model.id, transcript
                    )
                    recording = reference_service.mark_validated(recording.id, validation.metadata)
                except BaseException:
                    reference_service.delete(recording.id)
                    recording = None
                    raise
            finally:
                await lease.unload_model(model)
    except UploadTooLargeError as error:
        raise ReferenceTooLargeApiError from error
    except WorkerOperationError as error:
        if recording is not None:
            try:
                reference_service.delete(recording.id)
            except CleanupFailedError as cleanup_error:
                raise ReferenceCleanupFailedApiError from cleanup_error
        if error.code == "reference_invalid":
            raise ReferenceInvalidApiError from error
        if error.code == "reference_unsupported":
            raise ReferenceCapabilityUnsupportedApiError from error
        raise ReferenceWorkerUnavailableApiError from error
    except ReferenceCapabilityUnsupportedApiError:
        raise
    except ReferenceStorageUnavailableError as error:
        raise ReferenceStorageUnsupportedApiError from error
    except CleanupFailedError as error:
        raise ReferenceCleanupFailedApiError from error
    finally:
        upload.close()
    if recording is None:
        raise ReferenceInvalidApiError
    return _response(recording)


class _MultipartUpload:
    def __init__(self, file: tempfile.SpooledTemporaryFile[bytes] | None) -> None:
        self.model_id: str | None = None
        self.transcript: str | None = None
        self.file = file

    def close(self) -> None:
        if self.file is not None:
            self.file.close()


async def _parse_multipart(request: Request) -> _MultipartUpload:
    content_type = request.headers.get("content-type", "")
    match = re.search(r"(?:^|;)\s*boundary=(?:\"([^\"]+)\"|([^;\s]+))", content_type)
    if not content_type.lower().startswith("multipart/form-data") or match is None:
        raise ValueError("Content-Type must be multipart/form-data.")
    boundary = (match.group(1) or match.group(2)).encode("ascii", "strict")
    marker = b"\r\n--" + boundary
    opening = b"--" + boundary + b"\r\n"
    upload = _MultipartUpload(None)
    buffer = bytearray()
    state = "opening"
    field_name: str | None = None
    field_value = bytearray()
    file_seen = False
    try:
        async for chunk in request.stream():
            buffer.extend(chunk)
            while True:
                if state == "opening":
                    if len(buffer) < len(opening):
                        break
                    if not buffer.startswith(opening):
                        raise ValueError("Malformed multipart body.")
                    del buffer[: len(opening)]
                    state = "headers"
                elif state == "headers":
                    separator = buffer.find(b"\r\n\r\n")
                    if separator < 0:
                        if len(buffer) > 8192:
                            raise ValueError("Multipart headers are too large.")
                        break
                    headers = _part_headers(bytes(buffer[:separator]))
                    del buffer[: separator + 4]
                    field_name = headers.get("name")
                    if field_name not in {"model_id", "transcript", "file"}:
                        raise ValueError("Unsupported multipart field.")
                    if field_name == "file":
                        if file_seen:
                            raise ValueError("Exactly one file is required.")
                        file_seen = True
                        upload.file = tempfile.SpooledTemporaryFile(  # noqa: SIM115
                            max_size=1024 * 1024, mode="w+b"
                        )
                    field_value.clear()
                    state = "body"
                elif state == "body":
                    boundary_index = buffer.find(marker)
                    if boundary_index < 0:
                        flush = max(0, len(buffer) - len(marker))
                        _write_part(upload, field_name, bytes(buffer[:flush]), field_value)
                        del buffer[:flush]
                        break
                    _write_part(upload, field_name, bytes(buffer[:boundary_index]), field_value)
                    del buffer[: boundary_index + len(marker)]
                    if buffer.startswith(b"--"):
                        del buffer[:2]
                        state = "done"
                        break
                    if not buffer.startswith(b"\r\n"):
                        raise ValueError("Malformed multipart boundary.")
                    del buffer[:2]
                    state = "headers"
                else:
                    break
            if state == "done":
                break
        if state != "done":
            raise ValueError("Multipart body is incomplete.")
        if upload.file is not None:
            upload.file.seek(0)
        return upload
    except BaseException:
        upload.close()
        raise


def _part_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.decode("latin-1").split("\r\n"):
        name, separator, value = line.partition(":")
        if not separator:
            raise ValueError("Malformed multipart headers.")
        headers[name.casefold()] = value.strip()
    disposition = headers.get("content-disposition", "")
    name = re.search(r'(?:^|;)\s*name="([^"]+)"', disposition)
    if name is None:
        raise ValueError("Multipart field name is missing.")
    return {"name": name.group(1)}


def _write_part(
    upload: _MultipartUpload, field_name: str | None, data: bytes, field_value: bytearray
) -> None:
    if field_name == "file":
        if upload.file is None:
            raise ValueError("File part is missing.")
        current_size = upload.file.tell()
        if current_size + len(data) > 20 * 1024 * 1024:
            raise UploadTooLargeError("reference upload exceeds 20 MiB")
        upload.file.write(data)
        return
    field_value.extend(data)
    if len(field_value) > 8192:
        raise ValueError("Multipart field is too large.")
    value = field_value.decode("utf-8", "strict")
    if field_name == "model_id":
        upload.model_id = value
    elif field_name == "transcript":
        upload.transcript = value


@router.delete(
    "/references/{reference_id}",
    status_code=HTTPStatus.NO_CONTENT,
    responses=_REFERENCE_ERRORS,
)
async def delete_reference(reference_id: str, request: Request) -> None:
    service: ReferenceService = request.app.state.reference_service
    try:
        service.delete(reference_id)
    except ReferenceRecordNotFoundError as error:
        raise ReferenceNotFoundApiError from error
    except ReferenceInUseError as error:
        raise ReferenceInUseApiError from error
    except CleanupFailedError as error:
        raise ReferenceCleanupFailedApiError from error


def _response(recording: Any) -> ReferenceResponse:
    return ReferenceResponse(
        id=recording.id,
        model_id=recording.model_id,
        byte_size=recording.byte_size,
        sha256=recording.sha256,
        container=recording.container,
        sample_rate_hz=recording.sample_rate_hz,
        channels=recording.channels,
        duration_ms=recording.duration_ms,
        transcript_present=recording.transcript_present,
        state=recording.state.value,
        evidence=[
            ReferenceEvidence(
                code="worker_validated", message="Validated by the selected Worker."
            )
        ],
        expires_at=recording.expires_at,
    )
