from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.process import WorkerLaunchSpec, WorkerStatus
from tts_studio.workers.supervisor import WorkerSupervisor

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FAKE_WORKER_LAUNCH = WorkerLaunchSpec(
    command=(
        "uv",
        "run",
        "--project",
        "workers/fake",
        "tts-studio-fake-worker",
    ),
    cwd=_REPOSITORY_ROOT,
)


class SnapshotSupervisor:
    """A route-level supervisor double with no private worker registry."""

    __slots__ = ("stop_called",)

    def __init__(self) -> None:
        self.stop_called = False

    async def statuses(self) -> tuple[WorkerStatus, ...]:
        return (WorkerStatus(engine_id="fake", ready=True, message="", pid=1),)

    async def stop_all(self) -> None:
        self.stop_called = True


@pytest.mark.asyncio
async def test_system_route_reports_resolved_local_state(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "status": "healthy",
        "data_dir": str((tmp_path / "data").resolve()),
        "workers": [],
    }


@pytest.mark.asyncio
async def test_system_route_uses_the_supervisor_status_snapshot(tmp_path: Path) -> None:
    supervisor = SnapshotSupervisor()
    app = create_app(
        Settings.resolve(tmp_path / "data"), supervisor=cast(WorkerSupervisor, supervisor)
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.json()["workers"] == [{"engine_id": "fake", "status": "ready", "message": ""}]
    assert supervisor.stop_called is True


@pytest.mark.asyncio
async def test_system_route_reports_ready_worker_and_lifespan_stops_it(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    supervisor = WorkerSupervisor(StorageLayout.from_root(settings.data_dir), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    app = create_app(settings, supervisor=supervisor)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/system")

        assert response.status_code == 200
        assert response.json()["workers"] == [
            {"engine_id": "fake", "status": "ready", "message": ""}
        ]

    assert worker.process.returncode is not None


@pytest.mark.asyncio
async def test_system_route_keeps_core_healthy_when_worker_is_unhealthy(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    supervisor = WorkerSupervisor(StorageLayout.from_root(settings.data_dir), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    app = create_app(settings, supervisor=supervisor)

    async with app.router.lifespan_context(app):
        worker.process.terminate()
        await worker.process.wait()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/system")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.json()["workers"] == [
            {
                "engine_id": "fake",
                "status": "unhealthy",
                "message": "health check failed: UNAVAILABLE",
            }
        ]


@pytest.mark.asyncio
async def test_system_route_reports_unavailable_when_data_root_is_not_accessible(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    layout = app.state.storage_layout
    for directory in (
        layout.database,
        layout.models,
        layout.audio,
        layout.voices,
        layout.workers,
        layout.logs,
        layout.run,
        layout.staging,
    ):
        directory.rmdir()
    layout.root.rmdir()
    layout.root.touch()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/system")
    finally:
        layout.root.unlink()

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
