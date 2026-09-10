"""Safe runtime diagnostics for the Core process."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from tts_studio.runtime import RuntimeSnapshot, snapshot_runtime

router = APIRouter(prefix="/api/v1", tags=["runtime"])


class WorkerRuntimeResponse(BaseModel):
    engine_id: str
    status: str
    message: str
    capabilities: list[str] | None
    engine_version: str | None
    max_concurrency: int | None


class RuntimeResponse(BaseModel):
    version: str
    host: str
    port: int
    data_dir: str
    generation_status: str
    active_generations: dict[str, str | None] | None
    workers: list[WorkerRuntimeResponse]
    storage_accessible: bool
    database_accessible: bool
    startup_diagnostics: dict[str, dict[str, object]]


@router.get("/runtime", response_model=RuntimeResponse)
async def runtime_status(request: Request) -> RuntimeResponse:
    snapshot: RuntimeSnapshot = await snapshot_runtime(
        request.app.state.settings,
        request.app.state.supervisor,
        getattr(request.app.state, "runtime_manager", None),
    )
    return RuntimeResponse(
        version=snapshot.version,
        host=snapshot.host,
        port=snapshot.port,
        data_dir=snapshot.data_dir,
        generation_status=snapshot.generation_status,
        active_generations=snapshot.active_generations,
        workers=[
            WorkerRuntimeResponse(
                engine_id=worker.engine_id,
                status=worker.status,
                message=worker.message,
                capabilities=list(worker.capabilities) if worker.capabilities is not None else None,
                engine_version=worker.engine_version,
                max_concurrency=worker.max_concurrency,
            )
            for worker in snapshot.workers
        ],
        storage_accessible=snapshot.storage_accessible,
        database_accessible=snapshot.database_accessible,
        startup_diagnostics=snapshot.startup_diagnostics,
    )
