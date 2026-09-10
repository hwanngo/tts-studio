import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tts_studio.config import Settings
from tts_studio.runtime import snapshot_runtime
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.process import WorkerStatus


class FakeSupervisor:
    async def statuses(self) -> tuple[WorkerStatus, ...]:
        return (WorkerStatus("fake", True, "ready", 42),)

    async def describe(self, engine_id: str) -> object:
        assert engine_id == "fake"
        return SimpleNamespace(
            capabilities=(
                SimpleNamespace(name="streaming", supported=True),
                SimpleNamespace(name="clone", supported=False),
            ),
            engine_version="1.2.3",
            max_concurrency=2,
        )


class BrokenSupervisor:
    async def statuses(self) -> tuple[WorkerStatus, ...]:
        raise RuntimeError("worker transport details")


class StatusOnlySupervisor:
    async def statuses(self) -> tuple[WorkerStatus, ...]:
        return (WorkerStatus("status-only", True, "ready", 7),)


class DescribeFailureSupervisor(StatusOnlySupervisor):
    async def describe(self, engine_id: str) -> object:
        raise RuntimeError(f"private details for {engine_id}")


class FakeRuntimeManager:
    def status(self) -> object:
        return SimpleNamespace(active_generations={"fake": "gen-1"}, state="active")


class ConcurrentDescribeSupervisor:
    def __init__(self) -> None:
        self.started: set[str] = set()
        self.all_started = asyncio.Event()

    async def statuses(self) -> tuple[WorkerStatus, ...]:
        return tuple(
            WorkerStatus(engine_id, True, "ready", index)
            for index, engine_id in enumerate(("one", "two", "three"), 1)
        )

    async def describe(self, engine_id: str) -> object:
        self.started.add(engine_id)
        if len(self.started) == 3:
            self.all_started.set()
        await self.all_started.wait()
        return SimpleNamespace(supported=("health",), engine_version="1", max_concurrency=1)


class HangingDescribeSupervisor(StatusOnlySupervisor):
    async def describe(self, engine_id: str) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_snapshot_contains_safe_effective_runtime_and_storage_fields(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    StorageLayout.from_root(settings.data_dir).ensure()

    snapshot = await snapshot_runtime(settings, FakeSupervisor(), FakeRuntimeManager())

    assert snapshot.version == "0.1.0"
    assert snapshot.host == "127.0.0.1"
    assert snapshot.port == 7860
    assert snapshot.data_dir == str((tmp_path / "data").resolve())
    assert snapshot.generation_status == "active"
    assert snapshot.active_generations == {"fake": "gen-1"}
    assert snapshot.workers[0].engine_id == "fake"
    assert snapshot.workers[0].capabilities == ("streaming",)
    assert snapshot.workers[0].engine_version == "1.2.3"
    assert snapshot.workers[0].max_concurrency == 2
    assert snapshot.storage_accessible is True
    assert snapshot.database_accessible is True


@pytest.mark.asyncio
async def test_snapshot_handles_supervisor_failure_without_leaking_error(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    StorageLayout.from_root(settings.data_dir).ensure()

    snapshot = await snapshot_runtime(settings, BrokenSupervisor())

    assert snapshot.workers == ()
    assert snapshot.storage_accessible is True


@pytest.mark.asyncio
@pytest.mark.parametrize("supervisor", [StatusOnlySupervisor(), DescribeFailureSupervisor()])
async def test_snapshot_keeps_capabilities_unknown_without_description(
    tmp_path: Path, supervisor: object
) -> None:
    settings = Settings.resolve(tmp_path / "data")
    StorageLayout.from_root(settings.data_dir).ensure()

    snapshot = await snapshot_runtime(settings, supervisor)

    worker = snapshot.workers[0]
    assert worker.capabilities is None
    assert worker.engine_version is None
    assert worker.max_concurrency is None


@pytest.mark.asyncio
async def test_snapshot_marks_generation_unknown_without_stable_manager(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    StorageLayout.from_root(settings.data_dir).ensure()

    snapshot = await snapshot_runtime(settings, FakeSupervisor())

    assert snapshot.generation_status == "unknown"
    assert snapshot.active_generations is None


@pytest.mark.asyncio
async def test_snapshot_probes_worker_capabilities_concurrently(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    StorageLayout.from_root(settings.data_dir).ensure()
    supervisor = ConcurrentDescribeSupervisor()

    snapshot = await asyncio.wait_for(
        snapshot_runtime(settings, supervisor, capability_timeout=0.1),
        timeout=0.2,
    )

    assert supervisor.started == {"one", "two", "three"}
    assert [worker.capabilities for worker in snapshot.workers] == [("health",)] * 3


@pytest.mark.asyncio
async def test_snapshot_bounds_degraded_capability_probes(tmp_path: Path) -> None:
    settings = Settings.resolve(tmp_path / "data")
    StorageLayout.from_root(settings.data_dir).ensure()

    snapshot = await asyncio.wait_for(
        snapshot_runtime(settings, HangingDescribeSupervisor(), capability_timeout=0.01),
        timeout=0.1,
    )

    assert snapshot.workers[0].status == "ready"
    assert snapshot.workers[0].capabilities is None
