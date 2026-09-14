from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app


@pytest.mark.asyncio
async def test_runtime_route_reports_safe_runtime_snapshot(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/runtime")
    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "127.0.0.1"
    assert body["port"] == 7860
    assert body["data_dir"] == str((tmp_path / "data").resolve())
    assert body["storage_accessible"] is True
    assert body["database_accessible"] is True
    assert all("engine_id" in worker and "message" in worker for worker in body["workers"])
    assert "secret" not in response.text.lower()
