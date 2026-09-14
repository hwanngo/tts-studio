"""Stable, correlation-aware error responses for public HTTP interfaces."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from tts_studio.generation.limits import MAX_GENERATION_BODY_BYTES

_CORRELATION_HEADER = "X-Correlation-ID"
_LOGGER = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    """One safe, machine-readable public failure."""

    code: str
    message: str
    source: str
    retryable: bool
    correlation_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """The stable top-level shape shared by every public API failure."""

    error: ErrorBody


class PublicApiError(Exception):
    """A controlled failure that is safe to translate into the public envelope."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        source: str,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.source = source
        self.retryable = retryable
        self.details = dict(details or {})
        self.headers = dict(headers or {})


class AuthenticationError(PublicApiError):
    """A safe authentication category for future protected public routes."""

    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNAUTHORIZED,
            code="authentication_failed",
            message="Authentication failed.",
            source="authentication",
            headers={"WWW-Authenticate": "Bearer"},
        )


class RepositoryIdInvalidApiError(PublicApiError):
    """A repository identifier rejected before any adapter is contacted."""

    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="repository_id_invalid",
            message="The repository ID is invalid.",
            source="model_registry",
            details={"field": "repository_id"},
        )


class AdapterUnavailableApiError(PublicApiError):
    """No installed adapter could answer a compatibility request."""

    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            code="adapter_unavailable",
            message="No installed engine adapter is available to validate this repository.",
            source="model_registry",
            retryable=True,
        )


class ModelNotFoundApiError(PublicApiError):
    """A safe missing Model Installation response."""

    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="model_not_found",
            message="The Model Installation was not found.",
            source="model_registry",
        )


class DownloadNotFoundApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.NOT_FOUND,
            code="download_not_found",
            message="The Download Job was not found.",
            source="model_registry",
        )


class ModelIncompatibleApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="model_incompatible",
            message="No installed engine adapter can run this repository.",
            source="model_registry",
        )


class ModelValidationApiError(PublicApiError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(
            status_code=(
                HTTPStatus.SERVICE_UNAVAILABLE if retryable else HTTPStatus.UNPROCESSABLE_ENTITY
            ),
            code=code,
            message="The engine adapter could not validate the model request.",
            source="model_registry",
            retryable=retryable,
        )


class ModelVariantUnavailableApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="model_variant_unavailable",
            message="The requested model variant is unavailable.",
            source="model_registry",
            details={"field": "variant"},
        )


class ModelInUseApiError(PublicApiError):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTPStatus.CONFLICT,
            code="model_in_use",
            message="The Model Installation is in use.",
            source="model_registry",
            retryable=True,
        )


def install_error_handlers(
    app: FastAPI,
    *,
    api_token: str | None = None,
    listener_host: str = "127.0.0.1",
    listener_port: int = 7860,
) -> None:
    """Install the common public error models, handlers, and correlation header."""

    expected_authority = _listener_authority(listener_host, listener_port)

    @app.middleware("http")
    async def correlate_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = str(uuid4())
        request.state.correlation_id = correlation_id
        if not _host_is_allowed(request, listener_host, listener_port, expected_authority):
            return _error_response(
                request,
                status_code=HTTPStatus.BAD_REQUEST,
                code="host_not_allowed",
                message="Request Host is not allowed.",
                source="http",
                retryable=False,
            )
        origin = request.headers.get("origin")
        expected_origin = f"http://{request.headers.get('host', '').casefold()}"
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and origin is not None
            and origin != expected_origin
        ):
            return _error_response(
                request,
                status_code=HTTPStatus.FORBIDDEN,
                code="origin_not_allowed",
                message="Request Origin is not allowed.",
                source="http",
                retryable=False,
            )
        if api_token is not None and _is_api_request(request):
            authorization = request.headers.get("authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            if (
                scheme.casefold() != "bearer"
                or not supplied
                or not secrets.compare_digest(supplied, api_token)
            ):
                return _error_response(
                    request,
                    status_code=HTTPStatus.UNAUTHORIZED,
                    code="authentication_failed",
                    message="Authentication failed.",
                    source="authentication",
                    retryable=False,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        if request.method == "POST" and request.url.path.rstrip("/") in {
            "/api/v1/generations",
            "/v1/audio/speech",
        }:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_GENERATION_BODY_BYTES:
                    return _error_response(
                        request,
                        status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        code="request_body_too_large",
                        message="Generation request body exceeds 128 KiB.",
                        source="http",
                        retryable=False,
                    )
                body.extend(chunk)
            # Starlette's cached Request passes these bounded bytes to the router.
            request._body = bytes(body)
        response = await call_next(request)
        response.headers[_CORRELATION_HEADER] = correlation_id
        return response

    @app.exception_handler(PublicApiError)
    async def public_error_handler(request: Request, error: PublicApiError) -> Response:
        return _error_response(
            request,
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            source=error.source,
            retryable=error.retryable,
            details=error.details,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> Response:
        if not _is_api_request(request):
            return await request_validation_exception_handler(request, error)
        errors = [
            {
                "location": list(item["loc"]),
                "message": item["msg"],
                "type": item["type"],
            }
            for item in error.errors()
        ]
        return _error_response(
            request,
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="request_validation_failed",
            message="Request validation failed.",
            source="request_validation",
            retryable=False,
            details={"errors": errors},
        )

    @app.exception_handler(HTTPException)
    async def api_http_error_handler(request: Request, error: HTTPException) -> Response:
        if not _is_api_request(request):
            return await http_exception_handler(request, error)
        if error.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return _error_response(
                request,
                status_code=error.status_code,
                code="authentication_failed",
                message="Authentication failed.",
                source="authentication",
                retryable=False,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if error.status_code == HTTPStatus.NOT_FOUND:
            return _error_response(
                request,
                status_code=HTTPStatus.NOT_FOUND,
                code="not_found",
                message="API route not found.",
                source="http",
                retryable=False,
            )
        return _error_response(
            request,
            status_code=error.status_code,
            code="http_error",
            message="The API request could not be completed.",
            source="http",
            retryable=False,
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, error: Exception) -> Response:
        if not _is_api_request(request):
            raise error
        correlation_id = _correlation_id(request)
        _LOGGER.error(
            "Unhandled public API failure correlation_id=%s",
            correlation_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return _error_response(
            request,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="An internal error occurred.",
            source="core",
            retryable=False,
        )


def _is_api_request(request: Request) -> bool:
    path = request.url.path
    return path in {"/api", "/v1"} or path.startswith(("/api/", "/v1/"))


def _listener_authority(host: str, port: int) -> str:
    normalized_host = host.casefold()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    if port == 80:
        return normalized_host
    return f"{normalized_host}:{port}"


def _host_is_allowed(
    request: Request,
    listener_host: str,
    listener_port: int,
    expected_authority: str,
) -> bool:
    supplied = request.headers.get("host", "").casefold()
    if supplied == expected_authority:
        return True
    if listener_host in {"0.0.0.0", "::"} and _numeric_authority_matches_listener(
        supplied, listener_host, listener_port
    ):
        return True
    # HTTPX's in-process ASGI transport uses this reserved authority in existing
    # route tests. A network ASGI server supplies its actual listener address.
    return supplied == "test" and request.scope.get("server") == ("test", None)


def _numeric_authority_matches_listener(authority: str, listener_host: str, port: int) -> bool:
    try:
        parsed = urlsplit(f"http://{authority}")
        host = parsed.hostname
        supplied_port = parsed.port or 80
        if host is None or parsed.path or parsed.query or parsed.fragment:
            return False
        address = ip_address(host)
    except ValueError:
        return False
    listener_version = 4 if listener_host == "0.0.0.0" else 6
    return address.version == listener_version and supplied_port == port


def _correlation_id(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else str(uuid4())


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    source: str,
    retryable: bool,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    response_headers = dict(headers or {})
    response_headers[_CORRELATION_HEADER] = correlation_id
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            source=source,
            retryable=retryable,
            correlation_id=correlation_id,
            details=dict(details or {}),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=response_headers,
    )
