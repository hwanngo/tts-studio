"""Public model compatibility and installation routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from tts_studio.models.service import (
    AdapterCompatibility,
    AdapterUnavailableError,
    DownloadJobNotFoundError,
    InvalidRepositoryIdError,
    ModelIncompatibleError,
    ModelInstallationNotFoundError,
    ModelInstallationView,
    ModelInUseError,
    ModelService,
    ModelValidationError,
    ModelVariantUnavailableError,
    ValidationResult,
)
from tts_studio.server.errors import (
    AdapterUnavailableApiError,
    DownloadNotFoundApiError,
    ErrorEnvelope,
    ModelIncompatibleApiError,
    ModelInUseApiError,
    ModelNotFoundApiError,
    ModelValidationApiError,
    ModelVariantUnavailableApiError,
    RepositoryIdInvalidApiError,
)

router = APIRouter(prefix="/api/v1/models", tags=["models"])
downloads_router = APIRouter(prefix="/api/v1/downloads", tags=["downloads"])
_VALIDATION_ERRORS: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.UNPROCESSABLE_ENTITY): {
        "model": ErrorEnvelope,
        "description": "The request or repository ID is invalid.",
    },
    int(HTTPStatus.SERVICE_UNAVAILABLE): {
        "model": ErrorEnvelope,
        "description": "No installed Engine Adapter could answer validation.",
    },
}
_MODEL_LOOKUP_ERRORS: dict[int | str, dict[str, Any]] = {
    int(HTTPStatus.NOT_FOUND): {
        "model": ErrorEnvelope,
        "description": "The Model Installation does not exist.",
    }
}


class ModelValidationRequest(BaseModel):
    repository_id: str
    requested_revision: str | None = Field(default=None, min_length=1, max_length=256)


class ModelVariantResponse(BaseModel):
    id: str
    label: str


class CompatibilityEvidenceResponse(BaseModel):
    code: str
    message: str


class AdapterCompatibilityResponse(BaseModel):
    engine_id: str
    engine_version: str
    available: bool
    compatible: bool
    resolved_commit: str | None
    required_files: list[str]
    available_variants: list[ModelVariantResponse]
    estimated_bytes: int | None
    evidence: list[CompatibilityEvidenceResponse]
    error_code: str | None
    error_retryable: bool | None


class ModelValidationResponse(BaseModel):
    repository_id: str
    requested_revision: str | None
    compatible: bool
    selected_engine_id: str | None
    results: list[AdapterCompatibilityResponse]


class ModelInstallationResponse(BaseModel):
    id: str
    repository_id: str
    requested_revision: str | None
    resolved_commit: str
    engine_installation_id: str
    compatibility_evidence: dict[str, Any]
    runtime_variant: str
    byte_size: int
    cache_path: str
    desired_load_state: str
    observed_load_state: str
    replica_summary: dict[str, Any]
    desired_replicas: int
    last_error: dict[str, Any] | None
    created_at: str
    updated_at: str


class DownloadRequest(BaseModel):
    repository_id: str
    requested_revision: str | None = Field(default=None, min_length=1, max_length=256)
    variant: str | None = Field(default=None, min_length=1, max_length=64)


class ReplicaConfigurationRequest(BaseModel):
    desired_replicas: int = Field(ge=1, le=8)


class DownloadJobResponse(BaseModel):
    id: str
    repository_id: str
    requested_revision: str | None
    engine_installation_id: str
    state: str
    bytes_downloaded: int
    total_bytes: int | None
    phase: str
    staging_path: str
    target_model_id: str | None
    cancellation_requested: bool
    correlation_id: str
    error: dict[str, Any] | None
    created_at: str
    updated_at: str


@router.post(
    "/validate",
    response_model=ModelValidationResponse,
    responses=_VALIDATION_ERRORS,
)
async def validate_model(
    payload: ModelValidationRequest, request: Request
) -> ModelValidationResponse:
    service: ModelService = request.app.state.model_service
    try:
        result = await service.validate(payload.repository_id, payload.requested_revision)
    except InvalidRepositoryIdError as error:
        raise RepositoryIdInvalidApiError from error
    except AdapterUnavailableError as error:
        raise AdapterUnavailableApiError from error
    return _validation_response(result)


@router.get("", response_model=list[ModelInstallationResponse])
async def list_models(request: Request) -> list[ModelInstallationResponse]:
    service: ModelService = request.app.state.model_service
    return [_model_response(model) for model in service.list_models()]


@router.get(
    "/{model_id}",
    response_model=ModelInstallationResponse,
    responses=_MODEL_LOOKUP_ERRORS,
)
async def get_model(model_id: str, request: Request) -> ModelInstallationResponse:
    service: ModelService = request.app.state.model_service
    try:
        model = service.get_model(model_id)
    except ModelInstallationNotFoundError as error:
        raise ModelNotFoundApiError from error
    return _model_response(model)


@router.patch(
    "/{model_id}/replicas", response_model=ModelInstallationResponse, responses=_MODEL_LOOKUP_ERRORS
)
async def update_model_replicas(
    model_id: str, payload: ReplicaConfigurationRequest, request: Request
) -> ModelInstallationResponse:
    try:
        model = await request.app.state.model_service.set_desired_replicas(
            model_id, payload.desired_replicas
        )
    except ModelInstallationNotFoundError as error:
        raise ModelNotFoundApiError from error
    return _model_response(model)


@router.post(
    "/{model_id}/remove",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope},
        int(HTTPStatus.CONFLICT): {"model": ErrorEnvelope},
    },
)
async def remove_model(model_id: str, request: Request) -> Response:
    service: ModelService = request.app.state.model_service
    try:
        await service.remove_model(model_id)
    except ModelInstallationNotFoundError as error:
        raise ModelNotFoundApiError from error
    except ModelInUseError as error:
        raise ModelInUseApiError from error
    return Response(status_code=HTTPStatus.NO_CONTENT)


@downloads_router.post(
    "",
    response_model=DownloadJobResponse,
    status_code=HTTPStatus.ACCEPTED,
    responses={
        int(HTTPStatus.UNPROCESSABLE_ENTITY): {"model": ErrorEnvelope},
        int(HTTPStatus.SERVICE_UNAVAILABLE): {"model": ErrorEnvelope},
    },
)
async def start_download(payload: DownloadRequest, request: Request) -> DownloadJobResponse:
    service: ModelService = request.app.state.model_service
    try:
        job = await service.start_download(
            payload.repository_id,
            requested_revision=payload.requested_revision,
            variant=payload.variant,
            correlation_id=request.state.correlation_id,
        )
    except InvalidRepositoryIdError as error:
        raise RepositoryIdInvalidApiError from error
    except AdapterUnavailableError as error:
        raise AdapterUnavailableApiError from error
    except ModelValidationError as error:
        if error.code == "model_incompatible":
            raise ModelIncompatibleApiError from error
        raise ModelValidationApiError(error.code, retryable=error.retryable) from error
    except ModelIncompatibleError as error:
        raise ModelIncompatibleApiError from error
    except ModelVariantUnavailableError as error:
        raise ModelVariantUnavailableApiError from error
    return _download_response(job)


@downloads_router.get("", response_model=list[DownloadJobResponse])
async def list_downloads(request: Request) -> list[DownloadJobResponse]:
    service: ModelService = request.app.state.model_service
    return [_download_response(job) for job in service.list_downloads()]


@downloads_router.get(
    "/{job_id}",
    response_model=DownloadJobResponse,
    responses={int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope}},
)
async def get_download(job_id: str, request: Request) -> DownloadJobResponse:
    service: ModelService = request.app.state.model_service
    try:
        return _download_response(service.get_download(job_id))
    except DownloadJobNotFoundError as error:
        raise DownloadNotFoundApiError from error


@downloads_router.post(
    "/{job_id}/cancel",
    response_model=DownloadJobResponse,
    responses={int(HTTPStatus.NOT_FOUND): {"model": ErrorEnvelope}},
)
async def cancel_download(job_id: str, request: Request) -> DownloadJobResponse:
    service: ModelService = request.app.state.model_service
    try:
        return _download_response(await service.cancel_download(job_id))
    except DownloadJobNotFoundError as error:
        raise DownloadNotFoundApiError from error


def _validation_response(result: ValidationResult) -> ModelValidationResponse:
    return ModelValidationResponse(
        repository_id=result.repository_id,
        requested_revision=result.requested_revision,
        compatible=result.compatible,
        selected_engine_id=result.selected_engine_id,
        results=[_adapter_response(item) for item in result.results],
    )


def _adapter_response(result: AdapterCompatibility) -> AdapterCompatibilityResponse:
    return AdapterCompatibilityResponse(
        engine_id=result.engine_id,
        engine_version=result.engine_version,
        available=result.available,
        compatible=result.compatible,
        resolved_commit=result.resolved_commit,
        required_files=list(result.required_files),
        available_variants=[
            ModelVariantResponse(id=variant.id, label=variant.label)
            for variant in result.available_variants
        ],
        estimated_bytes=result.estimated_bytes,
        evidence=[
            CompatibilityEvidenceResponse(code=item.code, message=item.message)
            for item in result.evidence
        ],
        error_code=result.error_code,
        error_retryable=result.error_retryable,
    )


def _model_response(model: ModelInstallationView) -> ModelInstallationResponse:
    return ModelInstallationResponse(**model.__dict__)


def _download_response(job: Any) -> DownloadJobResponse:
    payload = dict(job.__dict__)
    payload["state"] = job.state.value
    return DownloadJobResponse(**payload)
