from pathlib import Path
from types import SimpleNamespace

import pytest

from tts_studio.models.registry import ModelInstallation
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.generation import WorkerCapabilities, WorkerCapacityError
from tts_studio.workers.supervisor import WorkerSupervisor


def _model() -> ModelInstallation:
    return ModelInstallation(
        id="model",
        repository_id="repo",
        requested_revision=None,
        resolved_commit="a" * 40,
        engine_installation_id="fake@1",
        compatibility_evidence={"engine_id": "fake"},
        runtime_variant="fp32",
        manifest={},
        checksum_summary={},
        byte_size=1,
        cache_path="models/model",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={},
        last_error=None,
        created_at="now",
        updated_at="now",
        desired_replicas=2,
    )


def _worker(replica_id: int) -> object:
    return SimpleNamespace(
        replica_id=replica_id,
        capabilities=WorkerCapabilities("fake", "1", frozenset(), 1),
        loaded_model_id=None,
        stub=SimpleNamespace(),
        token=f"token-{replica_id}",
    )


@pytest.mark.asyncio
async def test_acquire_selects_independent_worker_replicas(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    first = _worker(0)
    second = _worker(1)
    supervisor._workers["fake"] = first  # type: ignore[assignment]
    supervisor._replicas["fake"] = {0: first, 1: second}  # type: ignore[assignment]

    async with (
        supervisor.acquire(_model()) as first_lease,
        supervisor.acquire(_model()) as second_lease,
    ):
        assert first_lease._worker is not second_lease._worker  # type: ignore[attr-defined]
        with pytest.raises(WorkerCapacityError):
            async with supervisor.acquire(_model()):
                pass
