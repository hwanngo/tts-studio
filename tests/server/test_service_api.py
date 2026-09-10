from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app
from tts_studio.services.lifecycle import LifecycleUnsupportedError


class FakeLifecycle:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def status(self):
        return SimpleNamespace(
            status="installed", installed=True, running=False, healthy=None, message="ready"
        )

    def install(self):
        self.calls.append("install")
        if self.error:
            raise self.error
        return self.result or SimpleNamespace(
            operation="install", changed=True, message="installed"
        )

    def uninstall(self):
        self.calls.append("uninstall")
        return self.result or SimpleNamespace(
            operation="uninstall", changed=True, message="uninstalled"
        )

    def restart(self):
        self.calls.append("restart")
        if self.error:
            raise self.error
        return self.result or SimpleNamespace(
            operation="restart", changed=True, message="restarted"
        )


@pytest.mark.asyncio
async def test_service_route_delegates_lifecycle_and_requires_confirmation(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    lifecycle = FakeLifecycle()
    app.state.lifecycle_adapter = lifecycle
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        status = await client.get("/api/v1/service")
        missing = await client.post("/api/v1/service/restart", json={"confirm": False})
        installed = await client.post("/api/v1/service/install", json={"confirm": True})
    assert status.status_code == 200
    assert status.json()["status"] == "installed"
    assert missing.status_code == 422
    assert installed.status_code == 200
    assert lifecycle.calls == ["install"]


@pytest.mark.asyncio
async def test_service_route_reports_unsupported_operation(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    lifecycle = FakeLifecycle(error=LifecycleUnsupportedError("restart is unsupported"))
    app.state.lifecycle_adapter = lifecycle
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/service/restart", json={"confirm": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "service_unsupported"


@pytest.mark.asyncio
async def test_default_install_path_does_not_activate_duplicate_in_process_core(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/service/install", json={"confirm": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "service_unsupported"
    assert "already running" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_default_restart_path_reports_unsupported_operation(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/service/restart", json={"confirm": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "service_unsupported"
