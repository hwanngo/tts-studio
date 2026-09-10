from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

import pytest
from fastapi import FastAPI, Query
from httpx import ASGITransport, AsyncClient, Response

from tts_studio.config import Settings
from tts_studio.server.app import create_app
from tts_studio.server.errors import AuthenticationError, install_error_handlers
from tts_studio.workers.process import WorkerStatus
from tts_studio.workers.supervisor import WorkerSupervisor


class RaisingSupervisor:
    """A route-level double that raises a controlled status error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def statuses(self) -> tuple[WorkerStatus, ...]:
        raise self._error

    async def stop_all(self) -> None:
        pass


def _assert_error_shape(
    response: Response,
    *,
    status_code: int,
    code: str,
    message: str,
    source: str,
    retryable: bool,
) -> dict[str, object]:
    assert response.status_code == status_code
    correlation_id = response.headers["x-correlation-id"]
    assert str(UUID(correlation_id)) == correlation_id
    document = response.json()
    assert set(document) == {"error"}
    error = document["error"]
    assert error["code"] == code
    assert error["message"] == message
    assert error["source"] == source
    assert error["retryable"] is retryable
    assert error["correlation_id"] == correlation_id
    assert isinstance(error["details"], dict)
    return error


@pytest.mark.asyncio
async def test_request_validation_uses_the_public_error_envelope() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/v1/validated")
    async def validated(limit: Annotated[int, Query(gt=0)]) -> dict[str, int]:
        return {"limit": limit}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/validated?limit=not-an-integer")

    error = _assert_error_shape(
        response,
        status_code=422,
        code="request_validation_failed",
        message="Request validation failed.",
        source="request_validation",
        retryable=False,
    )
    assert error["details"] == {
        "errors": [
            {
                "location": ["query", "limit"],
                "message": "Input should be a valid integer, unable to parse string as an integer",
                "type": "int_parsing",
            }
        ]
    }
    assert "not-an-integer" not in response.text


@pytest.mark.asyncio
async def test_internal_api_failure_is_safe_and_correlated(tmp_path: Path) -> None:
    traceback_marker = "raw traceback must stay local"
    supervisor = RaisingSupervisor(RuntimeError(traceback_marker))
    app = create_app(
        Settings.resolve(tmp_path / "data"),
        supervisor=cast(WorkerSupervisor, supervisor),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/system")

    _assert_error_shape(
        response,
        status_code=500,
        code="internal_error",
        message="An internal error occurred.",
        source="core",
        retryable=False,
    )
    assert traceback_marker not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_typed_authentication_failure_uses_the_public_error_envelope(
    tmp_path: Path,
) -> None:
    supervisor = RaisingSupervisor(AuthenticationError())
    app = create_app(
        Settings.resolve(tmp_path / "data"),
        supervisor=cast(WorkerSupervisor, supervisor),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/system")

    _assert_error_shape(
        response,
        status_code=401,
        code="authentication_failed",
        message="Authentication failed.",
        source="authentication",
        retryable=False,
    )
    assert response.headers["www-authenticate"] == "Bearer"
