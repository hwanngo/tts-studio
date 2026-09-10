from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app


@pytest.fixture
def static_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    assets = site / "assets"
    assets.mkdir(parents=True)
    (site / "index.html").write_text("<title>TTS Studio</title>", encoding="utf-8")
    (assets / "app-a1b2c3.js").write_text("console.log('TTS Studio');", encoding="utf-8")
    return site


@pytest.mark.asyncio
async def test_serves_index_and_spa_fallback(tmp_path: Path, static_site: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"), static_root=static_site)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert "TTS Studio" in (await client.get("/")).text
        assert "TTS Studio" in (await client.get("/overview")).text
        response = await client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_serves_hashed_frontend_assets(tmp_path: Path, static_site: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"), static_root=static_site)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/assets/app-a1b2c3.js")

    assert response.status_code == 200
    assert response.text == "console.log('TTS Studio');"
    assert response.headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("path", ["/api/not-a-route", "/v1/not-a-route"])
@pytest.mark.asyncio
async def test_unknown_api_routes_remain_json_404s(
    tmp_path: Path,
    static_site: Path,
    path: str,
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"), static_root=static_site)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error == {
        "code": "not_found",
        "message": "API route not found.",
        "source": "http",
        "retryable": False,
        "correlation_id": response.headers["x-correlation-id"],
        "details": {},
    }
    assert str(UUID(error["correlation_id"])) == error["correlation_id"]


@pytest.mark.asyncio
async def test_missing_web_assets_leave_api_available(tmp_path: Path) -> None:
    app = create_app(
        Settings.resolve(tmp_path / "data"),
        static_root=tmp_path / "missing-static-site",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        root_response = await client.get("/")
        api_response = await client.get("/api/v1/system")

    assert root_response.status_code == 503
    assert "Web assets are unavailable" in root_response.text
    assert api_response.status_code == 200
    assert api_response.headers["content-type"].startswith("application/json")
