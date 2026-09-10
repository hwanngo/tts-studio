from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app


@pytest.mark.asyncio
async def test_settings_round_trip_and_safe_network_diagnostics(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/settings")
        assert response.status_code == 200
        assert response.json() == {
            "retain_audio_by_default": True,
            "artifact_max_age_days": None,
            "artifact_max_storage_bytes": None,
            "api_token_env": None,
            "host": "127.0.0.1",
            "port": 7860,
            "restart_required": False,
            "retention": {
                "retained_count": 0,
                "retained_bytes": 0,
                "max_age_days": None,
                "max_storage_bytes": None,
            },
        }
        patched = await client.patch(
            "/api/v1/settings",
            json={
                "retain_audio_by_default": False,
                "artifact_max_age_days": 30,
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["retain_audio_by_default"] is False
        assert body["artifact_max_age_days"] == 30
        assert body["api_token_env"] is None
        assert body["restart_required"] is False

    recreated = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        recreated.router.lifespan_context(recreated),
        AsyncClient(transport=ASGITransport(app=recreated), base_url="http://test") as client,
    ):
        persisted = await client.get("/api/v1/settings")
    assert persisted.status_code == 200
    assert persisted.json()["retain_audio_by_default"] is False
    assert persisted.json()["artifact_max_age_days"] == 30
    assert persisted.json()["api_token_env"] is None
    assert persisted.json()["restart_required"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"artifact_max_age_days": 0},
        {"artifact_max_storage_bytes": -1},
        {"api_token_env": "TTS_TOKEN"},
        {"api_token_env": "not-safe-name"},
        {"host": "0.0.0.0"},
        {"port": 0},
        {"api_token": "secret-value"},
    ],
)
async def test_settings_patch_rejects_invalid_or_secret_fields(
    tmp_path: Path, payload: dict, caplog
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.patch("/api/v1/settings", json=payload)
    assert response.status_code == 422
    assert "secret-value" not in response.text
    assert "secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_clear_retention_requires_confirmation(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/settings/retention/clear", json={"confirm": False})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "confirmation_required"


@pytest.mark.asyncio
async def test_clear_retention_delegates_confirmed_no_work_and_returns_safe_result(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    calls: list[str] = []

    def clear_retention():
        calls.append("clear")
        return SimpleNamespace(deleted=0, skipped=0, failed=0, issues=())

    app.state.settings_service = SimpleNamespace(clear_retention=clear_retention)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/settings/retention/clear", json={"confirm": True})
    assert response.status_code == 200
    assert response.json() == {"deleted": 0, "skipped": 0, "failed": 0, "issues": []}
    assert calls == ["clear"]


@pytest.mark.asyncio
async def test_settings_routes_preserve_authentication_behavior(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("TTS_API_TOKEN", "secret")
    app = create_app(Settings(data_dir=tmp_path / "data", api_token_env="TTS_API_TOKEN"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/settings")
        authenticated = await client.get(
            "/api/v1/settings",
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 401
    assert response.headers["X-Correlation-ID"]
    assert "secret" not in response.text
    assert authenticated.status_code == 200
    assert authenticated.json()["api_token_env"] == "TTS_API_TOKEN"
    assert authenticated.json()["restart_required"] is False
    assert "secret" not in authenticated.text
    assert "secret" not in caplog.text
