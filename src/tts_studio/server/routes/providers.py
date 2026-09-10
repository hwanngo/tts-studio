from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from tts_studio.providers.domain import ProviderProfile
from tts_studio.providers.registry import ProviderInUseError, ProviderNotFoundError
from tts_studio.providers.service import (
    InvalidProviderProfileError,
    ProviderSecretMissingError,
    ProviderService,
)
from tts_studio.server.errors import ErrorEnvelope, PublicApiError

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "openai_compatible"
    label: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key_env: str = Field(min_length=1, max_length=256)


class ProviderResponse(BaseModel):
    id: str
    kind: str
    label: str
    base_url: str
    model: str
    api_key_env: str
    created_at: str
    updated_at: str


class ProviderApiError(PublicApiError):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(status_code=status_code, code=code, message=message, source="providers", retryable=retryable)


_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}
}


@router.get("", response_model=list[ProviderResponse])
async def list_providers(request: Request) -> list[ProviderResponse]:
    return [_response(item) for item in request.app.state.provider_registry.list()]


@router.post("", response_model=ProviderResponse, status_code=HTTPStatus.CREATED, responses=_ERRORS)
async def create_provider(payload: ProviderRequest, request: Request) -> ProviderResponse:
    try:
        profile = request.app.state.provider_registry.create(**payload.model_dump())
    except InvalidProviderProfileError as error:
        raise ProviderApiError(422, "provider_profile_invalid", str(error)) from error
    return _response(profile)


@router.get("/{provider_id}", response_model=ProviderResponse, responses=_ERRORS)
async def get_provider(provider_id: str, request: Request) -> ProviderResponse:
    try:
        return _response(request.app.state.provider_registry.get(provider_id))
    except ProviderNotFoundError as error:
        raise ProviderApiError(404, "provider_not_found", "The provider profile was not found.") from error


@router.patch("/{provider_id}", response_model=ProviderResponse, responses=_ERRORS)
async def update_provider(provider_id: str, payload: ProviderRequest, request: Request) -> ProviderResponse:
    try:
        profile = request.app.state.provider_registry.update(provider_id, **payload.model_dump())
    except ProviderNotFoundError as error:
        raise ProviderApiError(404, "provider_not_found", "The provider profile was not found.") from error
    except InvalidProviderProfileError as error:
        raise ProviderApiError(422, "provider_profile_invalid", str(error)) from error
    return _response(profile)


@router.delete("/{provider_id}", status_code=HTTPStatus.NO_CONTENT, responses=_ERRORS)
async def delete_provider(provider_id: str, request: Request) -> Response:
    try:
        request.app.state.provider_registry.delete(provider_id)
    except ProviderNotFoundError as error:
        raise ProviderApiError(404, "provider_not_found", "The provider profile was not found.") from error
    except ProviderInUseError as error:
        raise ProviderApiError(409, "provider_in_use", "The provider profile is used by an active Generation Job.", retryable=True) from error
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post("/{provider_id}/validate", response_model=ProviderResponse, responses=_ERRORS)
async def validate_provider(provider_id: str, request: Request) -> ProviderResponse:
    try:
        profile = request.app.state.provider_registry.get(provider_id)
    except ProviderNotFoundError as error:
        raise ProviderApiError(404, "provider_not_found", "The provider profile was not found.") from error
    try:
        ProviderService(request.app.state.provider_registry).resolve_api_key(profile)
    except ProviderSecretMissingError as error:
        raise ProviderApiError(422, "provider_configuration_missing", "The provider credential is not configured.") from error
    return _response(profile)


def _response(profile: ProviderProfile) -> ProviderResponse:
    return ProviderResponse(**profile.__dict__)
