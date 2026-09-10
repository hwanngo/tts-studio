"""System-status endpoint for the Core."""

import os
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from tts_studio import __version__
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.process import WorkerStatus
from tts_studio.workers.supervisor import WorkerSupervisor

router = APIRouter(prefix="/api/v1")


class WorkerSummary(BaseModel):
    """A safe current observation of a supervised Worker."""

    engine_id: str
    status: Literal["ready", "unhealthy"]
    message: str


class SystemStatus(BaseModel):
    """The public health of the Core and its supervised Workers."""

    version: str
    status: Literal["healthy", "unavailable"]
    data_dir: str
    workers: list[WorkerSummary]


@router.get("/system", response_model=SystemStatus)
async def system_status(request: Request) -> SystemStatus:
    """Report Core storage availability and current Worker readiness."""
    layout: StorageLayout = request.app.state.storage_layout
    supervisor: WorkerSupervisor = request.app.state.supervisor

    workers = [_worker_summary(health) for health in await supervisor.statuses()]
    return SystemStatus(
        version=__version__,
        status="healthy" if _data_root_is_accessible(layout) else "unavailable",
        data_dir=str(layout.root),
        workers=workers,
    )


def _worker_summary(health: WorkerStatus) -> WorkerSummary:
    return WorkerSummary(
        engine_id=health.engine_id,
        status="ready" if health.ready else "unhealthy",
        message=health.message,
    )


def _data_root_is_accessible(layout: StorageLayout) -> bool:
    try:
        return layout.root.is_dir() and os.access(layout.root, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        return False
