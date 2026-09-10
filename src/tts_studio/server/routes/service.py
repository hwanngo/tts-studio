"""Lifecycle status and explicit service operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from tts_studio.server.errors import PublicApiError
from tts_studio.services.lifecycle import (
    LifecycleOperationError,
    LifecycleUnsupportedError,
    ServiceSnapshot,
)

router = APIRouter(prefix="/api/v1", tags=["service"])


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


class ServiceResponse(BaseModel):
    status: str
    installed: bool | None
    running: bool
    healthy: bool | None
    message: str


class ServiceOperationResponse(BaseModel):
    operation: str
    changed: bool
    message: str


@router.get("/service", response_model=ServiceResponse)
def service_status(request: Request) -> ServiceResponse:
    adapter = _adapter(request)
    try:
        snapshot: ServiceSnapshot = adapter.status()
    except (LifecycleUnsupportedError, LifecycleOperationError, RuntimeError) as error:
        raise _lifecycle_error(error) from error
    return ServiceResponse(
        status=snapshot.status,
        installed=snapshot.installed,
        running=snapshot.running,
        healthy=snapshot.healthy,
        message=snapshot.message,
    )


@router.post("/service/install", response_model=ServiceOperationResponse)
def install_service(request: Request, payload: ConfirmationRequest) -> ServiceOperationResponse:
    return _operate(request, payload, "install")


@router.post("/service/uninstall", response_model=ServiceOperationResponse)
def uninstall_service(request: Request, payload: ConfirmationRequest) -> ServiceOperationResponse:
    return _operate(request, payload, "uninstall")


@router.post("/service/restart", response_model=ServiceOperationResponse)
def restart_service(request: Request, payload: ConfirmationRequest) -> ServiceOperationResponse:
    return _operate(request, payload, "restart")


def _operate(
    request: Request, payload: ConfirmationRequest, operation: str
) -> ServiceOperationResponse:
    if not payload.confirm:
        raise PublicApiError(
            status_code=422,
            code="confirmation_required",
            message="Explicit confirmation is required.",
            source="service",
            details={"field": "confirm"},
        )
    adapter = _adapter(request)
    try:
        result = getattr(adapter, operation)()
    except (LifecycleUnsupportedError, LifecycleOperationError, RuntimeError) as error:
        raise _lifecycle_error(error) from error
    return ServiceOperationResponse(
        operation=result.operation,
        changed=result.changed,
        message=result.message,
    )


def _adapter(request: Request) -> Any:
    adapter = getattr(request.app.state, "lifecycle_adapter", None)
    if adapter is None:
        raise PublicApiError(
            status_code=409,
            code="service_unsupported",
            message="Service lifecycle operations are unsupported in this process.",
            source="service",
        )
    return adapter


def _lifecycle_error(error: Exception) -> PublicApiError:
    if isinstance(error, LifecycleUnsupportedError):
        return PublicApiError(
            status_code=409,
            code="service_unsupported",
            message=str(error),
            source="service",
        )
    return PublicApiError(
        status_code=502,
        code="service_operation_failed",
        message="The service lifecycle operation failed.",
        source="service",
        retryable=True,
    )
