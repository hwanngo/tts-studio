"""Public settings routes backed by the Core settings service."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from tts_studio.server.errors import PublicApiError
from tts_studio.settings.domain import CoreSettings
from tts_studio.settings.service import (
    RetentionSummary,
    SettingsPatch,
    SettingsService,
    SettingsValidationError,
)

router = APIRouter(prefix="/api/v1", tags=["settings"])


class SettingsPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retain_audio_by_default: bool | None = None
    artifact_max_age_days: int | None = Field(default=None, gt=0)
    artifact_max_storage_bytes: int | None = Field(default=None, gt=0)


class RetentionSummaryResponse(BaseModel):
    retained_count: int
    retained_bytes: int
    max_age_days: int | None
    max_storage_bytes: int | None


class SettingsResponse(BaseModel):
    retain_audio_by_default: bool
    artifact_max_age_days: int | None
    artifact_max_storage_bytes: int | None
    api_token_env: str | None
    host: str
    port: int
    restart_required: bool
    retention: RetentionSummaryResponse


class ClearRetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


class ClearRetentionResponse(BaseModel):
    deleted: int
    skipped: int
    failed: int
    issues: list[dict[str, str]]


@router.get("/settings", response_model=SettingsResponse)
def get_settings(request: Request) -> SettingsResponse:
    service: SettingsService = request.app.state.settings_service
    return _response(request, service)


@router.patch("/settings", response_model=SettingsResponse)
def patch_settings(request: Request, payload: SettingsPatchRequest) -> SettingsResponse:
    service: SettingsService = request.app.state.settings_service
    values = payload.model_dump(exclude_unset=True)
    try:
        updated = service.update_settings(SettingsPatch(**values))
    except SettingsValidationError as error:
        raise PublicApiError(
            status_code=422,
            code="settings_invalid",
            message="The settings could not be saved.",
            source="settings",
            details={"reason": str(error)},
        ) from error
    return _response(request, service, settings=updated)


@router.post("/settings/retention/clear", response_model=ClearRetentionResponse)
def clear_retention(request: Request, payload: ClearRetentionRequest) -> ClearRetentionResponse:
    if not payload.confirm:
        raise PublicApiError(
            status_code=422,
            code="confirmation_required",
            message="Explicit confirmation is required.",
            source="settings",
            details={"field": "confirm"},
        )
    service: SettingsService = request.app.state.settings_service
    result = service.clear_retention()
    return ClearRetentionResponse(
        deleted=result.deleted,
        skipped=result.skipped,
        failed=result.failed,
        issues=[
            {"artifact_id": issue.artifact_id, "message": issue.message} for issue in result.issues
        ],
    )


def _response(
    request: Request,
    service: SettingsService,
    *,
    settings: CoreSettings | None = None,
) -> SettingsResponse:
    persisted = settings or service.get_settings()
    runtime = request.app.state.settings
    retention: RetentionSummary = service.retention_summary()
    return SettingsResponse(
        retain_audio_by_default=persisted.retain_audio_by_default,
        artifact_max_age_days=persisted.artifact_max_age_days,
        artifact_max_storage_bytes=persisted.artifact_max_storage_bytes,
        api_token_env=runtime.api_token_env,
        host=runtime.host,
        port=runtime.port,
        restart_required=False,
        retention=RetentionSummaryResponse(
            retained_count=retention.retained_count,
            retained_bytes=retention.retained_bytes,
            max_age_days=retention.max_age_days,
            max_storage_bytes=retention.max_storage_bytes,
        ),
    )
