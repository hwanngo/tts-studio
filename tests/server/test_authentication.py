from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app


def test_non_loopback_app_requires_a_configured_api_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="API token"):
        create_app(Settings(data_dir=tmp_path / "data", host="0.0.0.0"))


@pytest.mark.asyncio
async def test_configured_api_token_protects_public_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_STUDIO_TEST_API_TOKEN", "private-token-value")
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            host="0.0.0.0",
            api_token_env="TTS_STUDIO_TEST_API_TOKEN",
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/v1/system")
        invalid = await client.get(
            "/api/v1/system", headers={"Authorization": "Bearer wrong-token"}
        )
        valid = await client.get(
            "/api/v1/system", headers={"Authorization": "Bearer private-token-value"}
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "authentication_failed"
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert valid.status_code == 200
    assert "private-token-value" not in missing.text
    assert "private-token-value" not in invalid.text


@pytest.mark.asyncio
async def test_loopback_api_remains_open_without_token_configuration(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/system")

    assert response.status_code == 200
