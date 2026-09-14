from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app


@pytest.mark.asyncio
async def test_provider_routes_return_safe_profile_and_never_accept_secret(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/providers",
            json={
                "kind": "openai_compatible",
                "label": "Local",
                "base_url": "http://127.0.0.1:9000/v1",
                "model": "tts-1",
                "api_key_env": "TTS_PROVIDER_KEY",
                "api_key": "must-not-be-accepted",
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_provider_validation_reports_missing_environment_credential(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    app.state.provider_registry._database.migrate()
    profile = app.state.provider_registry.create(
        kind="openai_compatible",
        label="Local",
        base_url="http://127.0.0.1:9000/v1",
        model="tts-1",
        api_key_env="MISSING_PROVIDER_KEY",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/v1/providers/{profile.id}/validate")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_configuration_missing"
