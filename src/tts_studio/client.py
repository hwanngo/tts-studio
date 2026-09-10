"""Typed client for the Core's public HTTP interface."""

from __future__ import annotations

import os
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter

from tts_studio.generated.api import (
    AlignmentResponse,
    AudioArtifactResponse,
    ClearRetentionResponse,
    DownloadJobResponse,
    ErrorBody,
    ErrorEnvelope,
    GenerationJobResponse,
    ModelInstallationResponse,
    ModelValidationResponse,
    ProviderResponse,
    RuntimeResponse,
    SavedVoiceResponse,
    ServiceOperationResponse,
    ServiceResponse,
    SettingsResponse,
    SystemStatus,
    VoiceResponse,
)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
_VALIDATION_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_DOWNLOAD_CREATION_TIMEOUT = httpx.Timeout(
    connect=5.0,
    read=30.0,
    write=5.0,
    pool=5.0,
)
_CANCELLATION_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_REMOVAL_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)
_GENERATION_CREATION_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
_ARTIFACT_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)
class _UnsetType:
    """Private marker distinguishing omitted arguments from explicit nulls."""


_UNSET = _UnsetType()


class CoreUnavailable(Exception):
    """Raised when the local Core cannot be reached."""


class CoreApiError(Exception):
    """Raised when the Core rejects a public API request."""

    def __init__(self, status_code: int, error: ErrorBody) -> None:
        super().__init__(error.message)
        self.status_code = status_code
        self.error = error


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class CoreClient:
    """Synchronous client for a running Core instance."""

    def __init__(self, base_url: str, *, api_token: str | None = None) -> None:
        self._base_url = base_url
        token_env = os.environ.get("TTS_STUDIO_API_TOKEN_ENV")
        self._api_token = api_token or (os.environ.get(token_env) if token_env else None)

    def system_status(self) -> SystemStatus:
        """Fetch the Core's current public system status."""
        return self._request_model("GET", "/api/v1/system", SystemStatus)

    def settings(self) -> SettingsResponse:
        """Fetch persisted settings and safe runtime metadata."""
        return self._request_model("GET", "/api/v1/settings", SettingsResponse)

    def update_settings(
        self,
        *,
        retain_audio_by_default: bool | None | _UnsetType = _UNSET,
        artifact_max_age_days: int | None | _UnsetType = _UNSET,
        artifact_max_storage_bytes: int | None | _UnsetType = _UNSET,
    ) -> SettingsResponse:
        """Update safe settings fields; secret values are never accepted."""
        settings = {
            key: value
            for key, value in {
                "retain_audio_by_default": retain_audio_by_default,
                "artifact_max_age_days": artifact_max_age_days,
                "artifact_max_storage_bytes": artifact_max_storage_bytes,
            }.items()
            if value is not _UNSET
        }
        return self._request_model("PATCH", "/api/v1/settings", SettingsResponse, json=settings)

    def clear_retention(self) -> ClearRetentionResponse:
        """Delete every retained artifact after explicit confirmation."""
        return self._request_model(
            "POST", "/api/v1/settings/retention/clear", ClearRetentionResponse,
            json={"confirm": True},
        )

    def runtime_status(self) -> RuntimeResponse:
        """Fetch safe Core and Worker runtime diagnostics."""
        return self._request_model("GET", "/api/v1/runtime", RuntimeResponse)

    def service_status(self) -> ServiceResponse:
        """Fetch service installation and health status."""
        return self._request_model("GET", "/api/v1/service", ServiceResponse)

    def install_service(self) -> ServiceOperationResponse:
        return self._service_operation("install")

    def uninstall_service(self) -> ServiceOperationResponse:
        return self._service_operation("uninstall")

    def restart_service(self) -> ServiceOperationResponse:
        return self._service_operation("restart")

    def _service_operation(self, operation: str) -> ServiceOperationResponse:
        return self._request_model(
            "POST", f"/api/v1/service/{operation}", ServiceOperationResponse,
            json={"confirm": True},
        )

    def validate_model(
        self, repository_id: str, requested_revision: str | None = None
    ) -> ModelValidationResponse:
        payload: dict[str, str] = {"repository_id": repository_id}
        if requested_revision is not None:
            payload["requested_revision"] = requested_revision
        return self._request_model(
            "POST",
            "/api/v1/models/validate",
            ModelValidationResponse,
            json=payload,
            timeout=_VALIDATION_TIMEOUT,
        )

    def list_models(self) -> list[ModelInstallationResponse]:
        response = self._request("GET", "/api/v1/models")
        try:
            return TypeAdapter(list[ModelInstallationResponse]).validate_python(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def start_download(
        self,
        repository_id: str,
        *,
        requested_revision: str | None = None,
        variant: str | None = None,
    ) -> DownloadJobResponse:
        payload: dict[str, str] = {"repository_id": repository_id}
        if requested_revision is not None:
            payload["requested_revision"] = requested_revision
        if variant is not None:
            payload["variant"] = variant
        return self._request_model(
            "POST",
            "/api/v1/downloads",
            DownloadJobResponse,
            json=payload,
            timeout=_DOWNLOAD_CREATION_TIMEOUT,
        )

    def get_download(self, job_id: str) -> DownloadJobResponse:
        return self._request_model(
            "GET",
            f"/api/v1/downloads/{quote(job_id, safe='')}",
            DownloadJobResponse,
        )

    def cancel_download(self, job_id: str) -> DownloadJobResponse:
        return self._request_model(
            "POST",
            f"/api/v1/downloads/{quote(job_id, safe='')}/cancel",
            DownloadJobResponse,
            timeout=_CANCELLATION_TIMEOUT,
        )

    def remove_model(self, model_id: str) -> None:
        self._request(
            "POST",
            f"/api/v1/models/{quote(model_id, safe='')}/remove",
            timeout=_REMOVAL_TIMEOUT,
        )

    def list_voices(self, model_id: str) -> list[VoiceResponse]:
        response = self._request(
            "GET", "/api/v1/voices", params={"model_id": model_id}
        )
        try:
            return TypeAdapter(list[VoiceResponse]).validate_python(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def preview_voice(self, model_id: str, voice_id: str, text: str) -> bytes:
        """Synthesize a temporary runtime Voice preview and return WAV bytes."""
        response = self._request(
            "POST",
            "/api/v1/voices/preview",
            json={"model_id": model_id, "voice_id": voice_id, "text": text},
            timeout=_GENERATION_CREATION_TIMEOUT,
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "audio/wav":
            raise _invalid_response(response)
        return response.content

    def list_saved_voices(self, model_id: str) -> list[SavedVoiceResponse]:
        response = self._request(
            "GET", "/api/v1/saved-voices", params={"model_id": model_id}
        )
        try:
            return TypeAdapter(list[SavedVoiceResponse]).validate_python(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def list_providers(self) -> list[ProviderResponse]:
        response = self._request("GET", "/api/v1/providers")
        try:
            return TypeAdapter(list[ProviderResponse]).validate_python(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def create_provider(self, *, label: str, base_url: str, model: str, api_key_env: str) -> ProviderResponse:
        return self._request_model(
            "POST", "/api/v1/providers", ProviderResponse,
            json={"kind": "openai_compatible", "label": label, "base_url": base_url,
                  "model": model, "api_key_env": api_key_env},
        )

    def update_provider(self, provider_id: str, *, label: str, base_url: str, model: str, api_key_env: str) -> ProviderResponse:
        return self._request_model(
            "PATCH", f"/api/v1/providers/{quote(provider_id, safe='')}", ProviderResponse,
            json={"kind": "openai_compatible", "label": label, "base_url": base_url,
                  "model": model, "api_key_env": api_key_env},
        )

    def validate_provider(self, provider_id: str) -> ProviderResponse:
        return self._request_model(
            "POST", f"/api/v1/providers/{quote(provider_id, safe='')}/validate", ProviderResponse,
        )

    def delete_provider(self, provider_id: str) -> None:
        self._request("DELETE", f"/api/v1/providers/{quote(provider_id, safe='')}")

    def create_generation(
        self,
        model_id: str,
        voice_id: str,
        text: str,
        *,
        retain_artifact: bool | _UnsetType = _UNSET,
        speed: float | None = None,
        pitch: float | None = None,
        volume: float | None = None,
    ) -> GenerationJobResponse:
        payload: dict[str, object] = {
            "model_id": model_id,
            "voice_id": voice_id,
            "text": text,
        }
        if retain_artifact is not _UNSET:
            payload["retain_artifact"] = retain_artifact
        for name, value in (("speed", speed), ("pitch", pitch), ("volume", volume)):
            if value is not None:
                payload[name] = value
        return self._request_model(
            "POST",
            "/api/v1/generations",
            GenerationJobResponse,
            json=payload,
            timeout=_GENERATION_CREATION_TIMEOUT,
        )

    def get_generation(self, job_id: str) -> GenerationJobResponse:
        return self._request_model(
            "GET",
            f"/api/v1/generations/{quote(job_id, safe='')}",
            GenerationJobResponse,
        )

    def list_generations(self) -> list[GenerationJobResponse]:
        response = self._request("GET", "/api/v1/generations")
        try:
            return TypeAdapter(list[GenerationJobResponse]).validate_python(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def cancel_generation(self, job_id: str) -> GenerationJobResponse:
        return self._request_model(
            "POST",
            f"/api/v1/generations/{quote(job_id, safe='')}/cancel",
            GenerationJobResponse,
            timeout=_CANCELLATION_TIMEOUT,
        )

    def request_alignment(self, job_id: str) -> AlignmentResponse:
        return self._request_model(
            "POST",
            f"/api/v1/generations/{quote(job_id, safe='')}/alignment",
            AlignmentResponse,
            timeout=_GENERATION_CREATION_TIMEOUT,
        )

    def get_alignment(self, job_id: str) -> AlignmentResponse:
        return self._request_model(
            "GET",
            f"/api/v1/generations/{quote(job_id, safe='')}/alignment",
            AlignmentResponse,
        )

    def list_history(self) -> list[AudioArtifactResponse]:
        response = self._request("GET", "/api/v1/history")
        try:
            return TypeAdapter(list[AudioArtifactResponse]).validate_python(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def delete_history(self, artifact_id: str) -> None:
        self._request(
            "DELETE",
            f"/api/v1/history/{quote(artifact_id, safe='')}",
        )

    def download_artifact(self, artifact_id: str) -> bytes:
        response = self._request(
            "GET",
            f"/api/v1/artifacts/{quote(artifact_id, safe='')}/audio",
            timeout=_ARTIFACT_DOWNLOAD_TIMEOUT,
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "audio/wav":
            raise _invalid_response(response)
        return response.content

    def _request_model(
        self,
        method: str,
        path: str,
        model: type[ResponseModel],
        **kwargs: Any,
    ) -> ResponseModel:
        response = self._request(method, path, **kwargs)
        try:
            return model.model_validate(response.json())
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise _invalid_response(response) from error

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._api_token is not None:
                headers = dict(kwargs.pop("headers", {}))
                headers["Authorization"] = f"Bearer {self._api_token}"
                kwargs["headers"] = headers
            with httpx.Client(base_url=self._base_url, timeout=_DEFAULT_TIMEOUT) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise CoreUnavailable from error

        if response.is_error:
            raise CoreApiError(response.status_code, _parse_error(response))
        return response


def _parse_error(response: httpx.Response) -> ErrorBody:
    try:
        return ErrorEnvelope.model_validate(response.json()).error
    except (ValueError, TypeError):
        correlation_id = response.headers.get("X-Correlation-ID", "unavailable")
        return ErrorBody(
            code="api_error",
            message="The Core could not complete the request.",
            source="http",
            retryable=response.status_code >= 500,
            correlation_id=correlation_id,
        )


def _invalid_response(response: httpx.Response) -> CoreApiError:
    return CoreApiError(
        502,
        ErrorBody(
            code="invalid_core_response",
            message="The Core returned an invalid response.",
            source="http",
            retryable=True,
            correlation_id=response.headers.get("X-Correlation-ID", "unavailable"),
        ),
    )
