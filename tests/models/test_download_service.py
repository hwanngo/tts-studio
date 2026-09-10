from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from collections import namedtuple
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.config import Settings
from tts_studio.models import activation as activation_module
from tts_studio.models.activation import (
    ModelActivation,
    ModelVerificationError,
    UnsafeStoragePathError,
)
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.models.service import ModelInUseError, ModelService
from tts_studio.server.app import create_app
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.generation import WorkerOperationError
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor

_COMMIT_A = "a" * 40
_COMMIT_B = "b" * 40


class DownloadSupervisor:
    def __init__(self, layout: StorageLayout) -> None:
        self.layout = layout
        self.commit = _COMMIT_A
        self.validation_commits: list[str] | None = None
        self.download_commits: list[str] = []
        self.validation_repository_id: str | None = None
        self.validation_evidence_message = "Fixture is compatible"
        self.corrupt_checksum = False
        self.download_error: engine_pb2.WorkerError | None = None
        self.reported_size_delta = 0
        self.block_download = False
        self.download_started = asyncio.Event()
        self.release_download = asyncio.Event()
        self.block_unload = False
        self.unload_started = asyncio.Event()
        self.release_unload = asyncio.Event()
        self.unload_observations: list[tuple[str, str, bool]] = []
        self.download_close_started = asyncio.Event()
        self.release_download_close = asyncio.Event()
        self.delay_download_close = False
        self.download_closed = asyncio.Event()

    async def describe(self, engine_id: str) -> engine_pb2.DescribeResponse:
        return engine_pb2.DescribeResponse(
            engine_id=engine_id,
            engine_version="1.0.0",
            max_concurrency=2,
            capabilities=[
                engine_pb2.Capability(name="preset_voices", supported=True),
                engine_pb2.Capability(name="streaming_synthesis", supported=True),
                engine_pb2.Capability(name="../token", supported=True),
                engine_pb2.Capability(name="ignored", supported=False),
            ],
        )

    async def validate_model(
        self,
        engine_id: str,
        request: engine_pb2.ValidateModelRequest,
        *,
        timeout: float = 10.0,
    ) -> engine_pb2.ValidateModelResponse:
        del timeout
        commit = (
            self.validation_commits.pop(0)
            if self.validation_commits
            else self.commit
        )
        return engine_pb2.ValidateModelResponse(
            repository_id=self.validation_repository_id or request.repository_id,
            requested_revision=request.requested_revision,
            resolved_commit=commit,
            compatible=True,
            engine_id=engine_id,
            engine_version="1.0.0",
            required_files=["config.json", "model.bin"],
            available_variants=[engine_pb2.ModelVariant(id="int8", label="INT8")],
            estimated_bytes=11,
            evidence=[
                engine_pb2.CompatibilityEvidence(
                    code="fixture_compatible", message=self.validation_evidence_message
                )
            ],
        )

    async def download_model(
        self,
        engine_id: str,
        request: engine_pb2.DownloadModelRequest,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        del engine_id, timeout
        try:
            self.download_commits.append(request.resolved_commit)
            destination = self.layout.staging / request.staging_destination
            self.download_started.set()
            yield engine_pb2.DownloadModelEvent(
                progress=engine_pb2.DownloadProgress(
                    sequence=1,
                    phase=engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
                    bytes_downloaded=0,
                    total_bytes=11,
                    message="Downloading",
                )
            )
            if self.download_error is not None:
                yield engine_pb2.DownloadModelEvent(error=self.download_error)
                return
            if self.block_download:
                await self.release_download.wait()
            files = {"config.json": b"config", "model.bin": b"model"}
            for name, content in files.items():
                (destination / name).write_bytes(content)
            yield engine_pb2.DownloadModelEvent(
                progress=engine_pb2.DownloadProgress(
                    sequence=2,
                    phase=engine_pb2.DOWNLOAD_PHASE_VERIFYING,
                    bytes_downloaded=11,
                    total_bytes=11,
                    message="Verifying",
                )
            )
            manifest_files = []
            for name, content in files.items():
                checksum = hashlib.sha256(content).hexdigest()
                if self.corrupt_checksum and name == "model.bin":
                    checksum = "0" * 64
                manifest_files.append(
                    engine_pb2.ManifestFile(
                        relative_path=name,
                        byte_size=len(content),
                        sha256=checksum,
                    )
                )
            yield engine_pb2.DownloadModelEvent(
                manifest=engine_pb2.ModelManifest(
                    repository_id=request.repository_id,
                    resolved_commit=request.resolved_commit,
                    variant=request.variant,
                    files=manifest_files,
                    byte_size=11 + self.reported_size_delta,
                )
            )
        finally:
            self.download_close_started.set()
            if self.delay_download_close:
                await self.release_download_close.wait()
            self.download_closed.set()

    async def unload_model(self, engine_id: str, model_id: str, cache_path: Path) -> None:
        self.unload_observations.append((engine_id, model_id, cache_path.exists()))
        self.unload_started.set()
        if getattr(self, "unload_error_code", None) is not None:
            raise WorkerOperationError(
                engine_pb2.WorkerError(
                    code=self.unload_error_code,
                    message="model is already unloaded",
                    retryable=False,
                )
            )
        if self.block_unload:
            await self.release_unload.wait()

    async def stop_all(self) -> None:
        pass


def _service(
    tmp_path: Path, *, storage_limit_bytes: int | None = None
) -> tuple[ModelService, ModelRegistry, StorageLayout, DownloadSupervisor]:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = ModelRegistry(database)
    supervisor = DownloadSupervisor(layout)
    descriptor = AdapterDescriptor(
        engine_id="fake",
        priority=10,
        launch=WorkerLaunchSpec(command=("fake-worker",), cwd=tmp_path),
    )
    return (
        ModelService(
            registry,
            supervisor,
            (descriptor,),
            layout=layout,
            storage_limit_bytes=storage_limit_bytes,
        ),
        registry,
        layout,
        supervisor,
    )


@pytest.mark.asyncio
async def test_download_verifies_bytes_and_checksums_before_atomic_activation(
    tmp_path: Path,
) -> None:
    service, registry, layout, _ = _service(tmp_path)

    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    completed = await service.wait_for_download(queued.id)

    assert completed.state is DownloadState.COMPLETED
    model = registry.list_models()[0]
    assert model.resolved_commit == _COMMIT_A
    assert model.byte_size == 11
    assert model.checksum_summary == {"algorithm": "sha256", "verified_files": 2}
    assert model.cache_path == f"models/{model.id}"
    assert (layout.root / model.cache_path / "model.bin").read_bytes() == b"model"
    assert not (layout.staging / queued.id).exists()


@pytest.mark.asyncio
async def test_download_uses_the_commit_selected_when_the_job_was_created(
    tmp_path: Path,
) -> None:
    service, registry, _, supervisor = _service(tmp_path)
    supervisor.validation_commits = [_COMMIT_A, _COMMIT_B]

    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="immutable-selection"
    )
    completed = await service.wait_for_download(queued.id)

    assert completed.state is DownloadState.COMPLETED
    assert supervisor.download_commits == [_COMMIT_A]
    assert registry.list_models()[0].resolved_commit == _COMMIT_A


@pytest.mark.asyncio
async def test_download_preserves_worker_error_code_and_retryability(
    tmp_path: Path,
) -> None:
    service, registry, _layout, supervisor = _service(tmp_path)
    supervisor.download_error = engine_pb2.WorkerError(
        code="offline", message="network is unavailable", retryable=True
    )

    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="offline-download"
    )
    failed = await service.wait_for_download(queued.id)

    assert failed.state is DownloadState.FAILED
    assert failed.error == {
        "code": "offline",
        "message": "The engine adapter could not download the model.",
        "retryable": True,
    }
    assert registry.list_models() == ()


@pytest.mark.asyncio
async def test_replacement_treats_already_unloaded_worker_as_idempotent(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    initial = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="initial"
    )
    await service.wait_for_download(initial.id)
    previous = registry.list_models()[0]
    previous_path = layout.root / previous.cache_path
    supervisor.commit = _COMMIT_B
    supervisor.unload_error_code = "model_not_loaded"

    replacement = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="replacement"
    )
    completed = await service.wait_for_download(replacement.id)

    assert completed.state is DownloadState.COMPLETED
    assert registry.list_models()[0].resolved_commit == _COMMIT_B
    assert not previous_path.exists()


@pytest.mark.asyncio
async def test_replacement_keeps_active_installation_when_worker_unload_fails(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    initial = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="initial"
    )
    await service.wait_for_download(initial.id)
    previous = registry.list_models()[0]
    previous_path = layout.root / previous.cache_path
    supervisor.commit = _COMMIT_B
    supervisor.unload_error_code = "model_unload_failed"

    replacement = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="replacement-failed"
    )
    failed = await service.wait_for_download(replacement.id)

    assert failed.state is DownloadState.FAILED
    assert registry.list_models() == (previous,)
    assert previous_path.is_dir()


@pytest.mark.asyncio
async def test_validation_rejects_a_compatible_response_for_a_different_repository(
    tmp_path: Path,
) -> None:
    service, _, _, supervisor = _service(tmp_path)
    supervisor.validation_repository_id = "other/repository"

    result = await service.validate("fixtures/compatible")

    assert result.compatible is False
    assert result.selected_engine_id is None


@pytest.mark.asyncio
async def test_download_persists_only_safe_runtime_capability_facts(tmp_path: Path) -> None:
    service, _registry, layout, _ = _service(tmp_path)

    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="capabilities"
    )
    assert (await service.wait_for_download(queued.id)).state is DownloadState.COMPLETED

    with Database(layout.database_path).read() as connection:
        row = connection.execute(
            "SELECT capabilities_json FROM engine_installations"
        ).fetchone()
    assert row is not None
    assert json.loads(row["capabilities_json"]) == {
        "supported": ["preset_voices", "streaming_synthesis"],
        "max_concurrency": 2,
    }


@pytest.mark.asyncio
async def test_download_persists_the_approved_state_machine_in_order(tmp_path: Path) -> None:
    service, _, layout, _ = _service(tmp_path)
    database = Database(layout.database_path)
    with database.transaction() as connection:
        connection.execute(
            "CREATE TABLE task_six_state_audit (sequence INTEGER PRIMARY KEY, state TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER task_six_audit_insert AFTER INSERT ON download_jobs "
            "BEGIN INSERT INTO task_six_state_audit(state) VALUES (NEW.state); END"
        )
        connection.execute(
            "CREATE TRIGGER task_six_audit_update AFTER UPDATE OF state ON download_jobs "
            "WHEN OLD.state <> NEW.state "
            "BEGIN INSERT INTO task_six_state_audit(state) VALUES (NEW.state); END"
        )

    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await service.wait_for_download(job.id)

    with database.read() as connection:
        states = [
            row["state"]
            for row in connection.execute(
                "SELECT state FROM task_six_state_audit ORDER BY sequence"
            ).fetchall()
        ]
    assert states == [
        "queued",
        "validating",
        "downloading",
        "verifying",
        "activating",
        "completed",
    ]


@pytest.mark.asyncio
async def test_checksum_failure_cleans_staging_and_preserves_previous_revision(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    first = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    first_done = await service.wait_for_download(first.id)
    previous = registry.list_models()[0]
    previous_path = layout.root / previous.cache_path
    assert first_done.state is DownloadState.COMPLETED

    supervisor.commit = _COMMIT_B
    supervisor.corrupt_checksum = True
    replacement = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-two"
    )
    failed = await service.wait_for_download(replacement.id)

    assert failed.state is DownloadState.FAILED
    assert failed.error == {
        "code": "checksum_mismatch",
        "message": "Downloaded model verification failed.",
        "retryable": False,
    }
    assert registry.list_models() == (previous,)
    assert previous_path.is_dir()
    assert not (layout.staging / replacement.id).exists()


@pytest.mark.asyncio
async def test_byte_count_failure_never_activates_partial_data(tmp_path: Path) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    supervisor.reported_size_delta = 1

    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    failed = await service.wait_for_download(job.id)

    assert failed.state is DownloadState.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "checksum_mismatch"
    assert registry.list_models() == ()
    assert tuple(layout.models.iterdir()) == ()
    assert tuple(layout.staging.iterdir()) == ()


@pytest.mark.asyncio
async def test_core_rejects_activation_when_filesystem_space_is_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, registry, layout, _ = _service(tmp_path)
    disk_usage = namedtuple("disk_usage", "total used free")
    monkeypatch.setattr(
        "tts_studio.models.activation.shutil.disk_usage",
        lambda _: disk_usage(total=100, used=99, free=1),
    )

    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    failed = await service.wait_for_download(job.id)

    assert failed.state is DownloadState.FAILED
    assert failed.error == {
        "code": "insufficient_storage",
        "message": "There is not enough managed storage to activate this model.",
        "retryable": True,
    }
    assert registry.list_models() == ()
    assert tuple(layout.models.iterdir()) == ()
    assert tuple(layout.staging.iterdir()) == ()
    assert str(layout.root) not in repr(failed.error)


@pytest.mark.asyncio
async def test_core_rejects_activation_when_managed_model_limit_would_be_exceeded(
    tmp_path: Path,
) -> None:
    service, registry, layout, _ = _service(tmp_path, storage_limit_bytes=10)

    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    failed = await service.wait_for_download(job.id)

    assert failed.state is DownloadState.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "insufficient_storage"
    assert registry.list_models() == ()
    assert tuple(layout.models.iterdir()) == ()


@pytest.mark.asyncio
async def test_cancellation_stops_the_stream_and_removes_partial_staging(tmp_path: Path) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    supervisor.block_download = True
    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await asyncio.wait_for(supervisor.download_started.wait(), timeout=1)

    requested = await service.cancel_download(queued.id)
    cancelled = await service.wait_for_download(queued.id)

    assert requested.cancellation_requested is True
    assert cancelled.state is DownloadState.CANCELLED
    assert cancelled.error == {
        "code": "download_cancelled",
        "message": "Model download was cancelled.",
        "retryable": True,
    }
    assert registry.list_models() == ()
    assert not (layout.staging / queued.id).exists()
    assert supervisor.download_closed.is_set()


@pytest.mark.asyncio
async def test_immediate_cancellation_terminalizes_queued_job_before_download_starts(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)

    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="immediate-cancel"
    )
    cancelled = await service.cancel_download(queued.id)

    assert cancelled.state is DownloadState.CANCELLED
    assert registry.get_download_job(queued.id).state is DownloadState.CANCELLED
    assert supervisor.download_commits == []
    assert not (layout.staging / queued.id).exists()


@pytest.mark.asyncio
async def test_download_cancellation_terminalizes_only_after_stream_closes(tmp_path: Path) -> None:
    service, registry, _, supervisor = _service(tmp_path)
    supervisor.block_download = True
    supervisor.delay_download_close = True
    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="cancel-order"
    )
    await asyncio.wait_for(supervisor.download_started.wait(), timeout=1)

    cancel_task = asyncio.create_task(service.cancel_download(queued.id))
    await asyncio.wait_for(supervisor.download_close_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not cancel_task.done()
    assert registry.get_download_job(queued.id).state is not DownloadState.CANCELLED

    supervisor.release_download_close.set()
    cancelled = await cancel_task
    assert cancelled.state is DownloadState.CANCELLED
    assert supervisor.download_closed.is_set()


@pytest.mark.asyncio
async def test_successful_replacement_unloads_then_retires_previous_revision(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    first = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await service.wait_for_download(first.id)
    previous = registry.list_models()[0]
    previous_path = layout.root / previous.cache_path

    supervisor.commit = _COMMIT_B
    replacement = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-two"
    )
    completed = await service.wait_for_download(replacement.id)

    current = registry.list_models()[0]
    assert completed.state is DownloadState.COMPLETED
    assert current.id != previous.id
    assert current.resolved_commit == _COMMIT_B
    assert supervisor.unload_observations == [("fake", previous.id, True)]
    assert not previous_path.exists()
    assert (layout.root / current.cache_path).is_dir()


@pytest.mark.asyncio
async def test_concurrent_replacements_serialize_unload_activation_and_retirement(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    initial = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-initial"
    )
    await service.wait_for_download(initial.id)
    supervisor.commit = _COMMIT_B
    supervisor.block_unload = True

    first = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await asyncio.wait_for(supervisor.unload_started.wait(), timeout=1)
    second = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-two"
    )
    await asyncio.sleep(0.05)

    assert len(supervisor.unload_observations) == 1
    supervisor.release_unload.set()
    first_done, second_done = await asyncio.gather(
        service.wait_for_download(first.id), service.wait_for_download(second.id)
    )

    assert first_done.state is DownloadState.COMPLETED
    assert second_done.state is DownloadState.COMPLETED
    assert len(registry.list_models()) == 1
    assert len(tuple(layout.models.iterdir())) == 1


@pytest.mark.asyncio
async def test_database_failure_after_rename_rolls_back_new_files_and_previous_revision(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    first = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await service.wait_for_download(first.id)
    previous = registry.list_models()[0]
    previous_path = layout.root / previous.cache_path
    database = Database(layout.database_path)
    with database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_task_six_activation "
            "BEFORE INSERT ON model_installations "
            "BEGIN SELECT RAISE(ABORT, 'crash point after rename'); END"
        )

    supervisor.commit = _COMMIT_B
    replacement = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-two"
    )
    failed = await service.wait_for_download(replacement.id)

    assert failed.state is DownloadState.FAILED
    assert registry.list_models() == (previous,)
    assert previous_path.is_dir()
    assert {path.name for path in layout.models.iterdir()} == {previous.id}


@pytest.mark.asyncio
async def test_remove_refuses_a_model_in_use_without_unloading_or_deleting(tmp_path: Path) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await service.wait_for_download(job.id)
    model = registry.list_models()[0]
    registry.update_replica_summary(model.id, {"ready": 1, "active_generations": 1})

    with pytest.raises(ModelInUseError):
        await service.remove_model(model.id)

    assert supervisor.unload_observations == []
    assert registry.list_models()[0].id == model.id
    assert (layout.root / model.cache_path).is_dir()


@pytest.mark.asyncio
async def test_remove_unloads_before_transactional_file_and_metadata_removal(
    tmp_path: Path,
) -> None:
    service, registry, layout, supervisor = _service(tmp_path)
    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await service.wait_for_download(job.id)
    model = registry.list_models()[0]
    model_path = layout.root / model.cache_path

    await service.remove_model(model.id)

    assert supervisor.unload_observations == [("fake", model.id, True)]
    assert registry.list_models() == ()
    assert not model_path.exists()


@pytest.mark.asyncio
async def test_remove_restores_files_when_metadata_deletion_fails(tmp_path: Path) -> None:
    service, registry, layout, _ = _service(tmp_path)
    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await service.wait_for_download(job.id)
    model = registry.list_models()[0]
    model_path = layout.root / model.cache_path
    database = Database(layout.database_path)
    with database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_task_six_removal "
            "BEFORE DELETE ON model_installations "
            "BEGIN SELECT RAISE(ABORT, 'crash point during removal'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="crash point during removal"):
        await service.remove_model(model.id)

    assert registry.list_models() == (model,)
    assert model_path.is_dir()
    assert tuple(layout.staging.iterdir()) == ()


@pytest.mark.parametrize("swap_kind", ["file", "directory"])
def test_activation_rejects_a_staging_symlink_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_kind: str
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("race-job")
    content = b"model"
    if swap_kind == "file":
        relative = "model.bin"
        staged = staging / relative
        staged.write_bytes(content)
        outside = tmp_path / "outside.bin"
        outside.write_bytes(content)
        replacement = staged
        target = outside
    else:
        relative = "weights/model.bin"
        staged_directory = staging / "weights"
        staged_directory.mkdir()
        (staged_directory / "model.bin").write_bytes(content)
        outside = tmp_path / "outside-weights"
        outside.mkdir()
        (outside / "model.bin").write_bytes(content)
        replacement = staged_directory
        target = outside
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(content),
        files=[
            engine_pb2.ManifestFile(
                relative_path=relative,
                byte_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )

    original_walk = activation._walk_regular_files
    swapped = False

    def walk_then_swap(root: Path) -> dict[str, Path]:
        nonlocal swapped
        files = original_walk(root)
        if not swapped:
            swapped = True
            if replacement.is_dir() and not replacement.is_symlink():
                shutil.rmtree(replacement)
            else:
                replacement.unlink()
            try:
                replacement.symlink_to(target, target_is_directory=swap_kind == "directory")
            except OSError as error:
                pytest.skip(f"symlinks are unavailable: {error}")
        return files

    monkeypatch.setattr(activation, "_walk_regular_files", walk_then_swap)

    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=(relative,),
    )
    with pytest.raises(ModelVerificationError, match="managed staging"):
        activation.activate(staging, "race-model", verified)

    assert not (layout.models / "race-model").exists()
    assert staging.exists()


@pytest.mark.parametrize("swap_kind", ["nested_file", "nested_directory"])
def test_activation_publishes_the_opened_snapshot_when_staging_changes_at_rename_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_kind: str
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("promotion-race-job")
    trusted = b"trusted model bytes"
    relative = "weights/model.bin"
    staged_file = staging / relative
    staged_file.parent.mkdir()
    staged_file.write_bytes(trusted)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker bytes")
    if swap_kind == "nested_directory":
        replacement = staged_file.parent
        replacement_target = tmp_path / "outside-weights"
        replacement_target.mkdir()
        (replacement_target / "model.bin").write_bytes(b"attacker bytes")
    else:
        replacement = staged_file
        replacement_target = outside
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(trusted),
        files=[
            engine_pb2.ManifestFile(
                relative_path=relative,
                byte_size=len(trusted),
                sha256=hashlib.sha256(trusted).hexdigest(),
            )
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=(relative,),
    )

    original_rename = activation_module.os.rename
    publication_calls = 0

    def swap_worker_entry_at_publication(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal publication_calls
        assert src_dir_fd is not None
        assert dst_dir_fd is not None
        publication_calls += 1
        if replacement.is_dir() and not replacement.is_symlink():
            shutil.rmtree(replacement)
        else:
            replacement.unlink()
        try:
            replacement.symlink_to(
                replacement_target, target_is_directory=swap_kind == "nested_directory"
            )
        except OSError as error:
            pytest.skip(f"symlinks are unavailable: {error}")
        original_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    original_copy = activation._copy_verified_snapshot

    def copy_then_install_publication_hook(
        source_fd: int,
        promotion_fd: int,
        expected: dict[str, tuple[int, str]] | None,
    ) -> dict[str, tuple[int, str]]:
        copied = original_copy(source_fd, promotion_fd, expected)
        monkeypatch.setattr(activation_module.os, "rename", swap_worker_entry_at_publication)
        return copied

    monkeypatch.setattr(activation, "_copy_verified_snapshot", copy_then_install_publication_hook)

    activated = activation.activate(staging, "promotion-race-model", verified)

    published = activated.path / relative
    assert publication_calls == 1
    assert published.is_file()
    assert not published.is_symlink()
    assert published.read_bytes() == trusted


def test_activation_publishes_the_snapshot_before_a_regular_source_byte_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("changed-bytes-job")
    relative = "weights/model.bin"
    trusted = b"trusted model bytes"
    staged_file = staging / relative
    staged_file.parent.mkdir()
    staged_file.write_bytes(trusted)
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(trusted),
        files=[
            engine_pb2.ManifestFile(
                relative_path=relative,
                byte_size=len(trusted),
                sha256=hashlib.sha256(trusted).hexdigest(),
            )
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=(relative,),
    )

    original_rename = activation_module.os.rename
    publication_calls = 0

    def change_source_at_publication(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal publication_calls
        assert src_dir_fd is not None
        assert dst_dir_fd is not None
        publication_calls += 1
        staged_file.write_bytes(b"attacker bytes")
        original_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    original_copy = activation._copy_verified_snapshot

    def copy_then_install_publication_hook(
        source_fd: int,
        promotion_fd: int,
        expected: dict[str, tuple[int, str]] | None,
    ) -> dict[str, tuple[int, str]]:
        copied = original_copy(source_fd, promotion_fd, expected)
        monkeypatch.setattr(activation_module.os, "rename", change_source_at_publication)
        return copied

    monkeypatch.setattr(activation, "_copy_verified_snapshot", copy_then_install_publication_hook)

    activated = activation.activate(staging, "changed-bytes-model", verified)

    assert publication_calls == 1
    assert (activated.path / relative).read_bytes() == trusted


def test_activation_requires_a_core_verified_manifest_before_publication(tmp_path: Path) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("missing-manifest-job")
    (staging / "model.bin").write_bytes(b"model")

    with pytest.raises(TypeError):
        activation.activate(staging, "missing-manifest-model")

    assert tuple(layout.models.iterdir()) == ()


def test_activation_rejects_bytes_changed_after_verification_and_cleans_promotion(
    tmp_path: Path,
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("verified-bytes-job")
    relative = "weights/model.bin"
    trusted = b"trusted model bytes"
    staged_file = staging / relative
    staged_file.parent.mkdir()
    staged_file.write_bytes(trusted)
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(trusted),
        files=[
            engine_pb2.ManifestFile(
                relative_path=relative,
                byte_size=len(trusted),
                sha256=hashlib.sha256(trusted).hexdigest(),
            )
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=(relative,),
    )
    staged_file.write_bytes(b"attacker bytes")

    with pytest.raises(ModelVerificationError):
        activation.activate(staging, "verified-bytes-model", verified)

    assert not (layout.models / "verified-bytes-model").exists()
    assert not tuple(layout.models.glob("promotion--*"))
    assert staged_file.read_bytes() == b"attacker bytes"


@pytest.mark.parametrize("swap_kind", ["nested_file", "nested_directory"])
def test_activation_isolated_from_staging_symlink_replacement_after_source_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_kind: str
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("post-read-race-job")
    trusted = b"trusted model bytes"
    relative = "weights/model.bin"
    staged_file = staging / relative
    staged_file.parent.mkdir()
    staged_file.write_bytes(trusted)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker bytes")
    if swap_kind == "nested_directory":
        replacement = staged_file.parent
        replacement_target = tmp_path / "outside-weights"
        replacement_target.mkdir()
        (replacement_target / "model.bin").write_bytes(b"attacker bytes")
    else:
        replacement = staged_file
        replacement_target = outside
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(trusted),
        files=[
            engine_pb2.ManifestFile(
                relative_path=relative,
                byte_size=len(trusted),
                sha256=hashlib.sha256(trusted).hexdigest(),
            )
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=(relative,),
    )
    original_rename = activation_module.os.rename
    publication_calls = 0

    def swap_worker_entry_at_publication(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal publication_calls
        assert src_dir_fd is not None
        assert dst_dir_fd is not None
        publication_calls += 1
        if replacement.is_dir() and not replacement.is_symlink():
            shutil.rmtree(replacement)
        else:
            replacement.unlink()
        try:
            replacement.symlink_to(
                replacement_target, target_is_directory=swap_kind == "nested_directory"
            )
        except OSError as error:
            pytest.skip(f"symlinks are unavailable: {error}")
        original_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    original_copy = activation._copy_verified_snapshot

    def copy_then_install_publication_hook(
        source_fd: int,
        promotion_fd: int,
        expected: dict[str, tuple[int, str]] | None,
    ) -> dict[str, tuple[int, str]]:
        copied = original_copy(source_fd, promotion_fd, expected)
        monkeypatch.setattr(activation_module.os, "rename", swap_worker_entry_at_publication)
        return copied

    monkeypatch.setattr(activation, "_copy_verified_snapshot", copy_then_install_publication_hook)

    activated = activation.activate(staging, "post-read-race-model", verified)

    published = activated.path / relative
    assert publication_calls == 1
    assert published.is_file()
    assert not published.is_symlink()
    assert published.read_bytes() == trusted


def test_activation_publishes_a_recursive_tree_of_core_created_regular_files(
    tmp_path: Path,
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("recursive-job")
    files = {
        "config.json": b"config",
        "weights/model.bin": b"model",
        "weights/nested/labels.txt": b"labels",
    }
    for relative, content in files.items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=sum(len(content) for content in files.values()),
        files=[
            engine_pb2.ManifestFile(
                relative_path=relative,
                byte_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for relative, content in files.items()
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=tuple(files),
    )

    activated = activation.activate(staging, "recursive-model", verified)

    published = activated.path
    assert published.is_dir()
    for relative, content in files.items():
        path = published / relative
        assert path.is_file()
        assert not path.is_symlink()
        assert path.read_bytes() == content
    assert (published / "weights").is_dir()
    assert not (published / "weights").is_symlink()
    assert (published / "weights" / "nested").is_dir()
    assert not (published / "weights" / "nested").is_symlink()


def test_activation_rejects_a_destination_collision_without_following_a_symlink(
    tmp_path: Path,
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("collision-job")
    (staging / "model.bin").write_bytes(b"model")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.bin"
    protected.write_bytes(b"keep")
    destination = layout.models / "collision-model"
    try:
        destination.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(b"model"),
        files=[
            engine_pb2.ManifestFile(
                relative_path="model.bin",
                byte_size=len(b"model"),
                sha256=hashlib.sha256(b"model").hexdigest(),
            )
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=("model.bin",),
    )

    with pytest.raises(UnsafeStoragePathError):
        activation.activate(staging, "collision-model", verified)

    assert protected.read_bytes() == b"keep"
    assert destination.is_symlink()


def test_activation_fails_closed_without_safe_descriptor_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("unsupported-platform-job")
    (staging / "model.bin").write_bytes(b"model")
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(b"model"),
        files=[
            engine_pb2.ManifestFile(
                relative_path="model.bin",
                byte_size=len(b"model"),
                sha256=hashlib.sha256(b"model").hexdigest(),
            )
        ],
    )
    verified = activation.verify(
        staging,
        manifest,
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        required_files=("model.bin",),
    )
    monkeypatch.setattr("tts_studio.models.activation.os.name", "nt")

    with pytest.raises(ModelVerificationError) as error:
        activation.activate(staging, "unsupported-platform-model", verified)

    assert error.value.code == "download_failed"
    assert tuple(layout.models.iterdir()) == ()
    assert staging.exists()


def test_manifest_rejects_paths_that_escape_the_staging_directory(tmp_path: Path) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("safe-job")
    outside = layout.root / "outside.bin"
    outside.write_bytes(b"outside")
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=7,
        files=[
            engine_pb2.ManifestFile(
                relative_path="../outside.bin",
                byte_size=7,
                sha256=hashlib.sha256(b"outside").hexdigest(),
            )
        ],
    )

    with pytest.raises(ModelVerificationError, match="managed staging"):
        activation.verify(
            staging,
            manifest,
            repository_id="fixtures/compatible",
            resolved_commit=_COMMIT_A,
            variant="int8",
            required_files=("config.json", "model.bin"),
        )

    assert outside.read_bytes() == b"outside"


def test_manifest_rejects_files_not_declared_by_the_adapter_requirements(
    tmp_path: Path,
) -> None:
    _, _, layout, _ = _service(tmp_path)
    activation = ModelActivation(layout)
    staging = activation.allocate_staging("unexpected-file-job")
    (staging / "config.json").write_bytes(b"config")
    (staging / "model.bin").write_bytes(b"model")
    (staging / "unexpected.bin").write_bytes(b"unexpected")
    manifest = engine_pb2.ModelManifest(
        repository_id="fixtures/compatible",
        resolved_commit=_COMMIT_A,
        variant="int8",
        byte_size=len(b"config") + len(b"model") + len(b"unexpected"),
        files=[
            engine_pb2.ManifestFile(
                relative_path=name,
                byte_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for name, content in {
                "config.json": b"config",
                "model.bin": b"model",
                "unexpected.bin": b"unexpected",
            }.items()
        ],
    )

    with pytest.raises(ModelVerificationError):
        activation.verify(
            staging,
            manifest,
            repository_id="fixtures/compatible",
            resolved_commit=_COMMIT_A,
            variant="int8",
            required_files=("config.json", "model.bin"),
        )


@pytest.mark.asyncio
async def test_http_routes_share_the_transactional_download_and_removal_behavior(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    layout = StorageLayout.from_root(data_dir)
    layout.ensure()
    supervisor = DownloadSupervisor(layout)
    descriptor = AdapterDescriptor(
        engine_id="fake",
        priority=10,
        launch=WorkerLaunchSpec(command=("fake-worker",), cwd=tmp_path),
    )
    app = create_app(
        Settings.resolve(data_dir),
        supervisor=cast(WorkerSupervisor, supervisor),
        adapters=(descriptor,),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        started = await client.post(
            "/api/v1/downloads",
            json={"repository_id": "fixtures/compatible", "variant": "int8"},
        )
        assert started.status_code == 202
        job_id = started.json()["id"]
        completed = await app.state.model_service.wait_for_download(job_id)
        assert completed.state is DownloadState.COMPLETED

        listed = await client.get("/api/v1/downloads")
        detail = await client.get(f"/api/v1/downloads/{job_id}")
        models = await client.get("/api/v1/models")
        assert listed.status_code == detail.status_code == models.status_code == 200
        assert listed.json()[0]["state"] == "completed"
        assert detail.json()["target_model_id"] == models.json()[0]["id"]

        model_id = models.json()[0]["id"]
        app.state.model_registry.update_replica_summary(
            model_id, {"ready": 1, "active_generations": 1}
        )
        blocked = await client.post(f"/api/v1/models/{model_id}/remove")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "model_in_use"
        app.state.model_registry.update_replica_summary(
            model_id, {"ready": 0, "active_generations": 0}
        )
        removed = await client.post(f"/api/v1/models/{model_id}/remove")
        assert removed.status_code == 204
        assert (await client.get("/api/v1/models")).json() == []

        missing = await client.get("/api/v1/downloads/missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "download_not_found"


@pytest.mark.asyncio
async def test_download_persists_only_redacted_compatibility_evidence(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    layout = StorageLayout.from_root(data_dir)
    layout.ensure()
    supervisor = DownloadSupervisor(layout)
    unsafe_message = "See https://user:secret@host/private-model for model details"
    supervisor.validation_evidence_message = unsafe_message
    descriptor = AdapterDescriptor(
        engine_id="fake",
        priority=10,
        launch=WorkerLaunchSpec(command=("fake-worker",), cwd=tmp_path),
    )
    app = create_app(
        Settings.resolve(data_dir),
        supervisor=cast(WorkerSupervisor, supervisor),
        adapters=(descriptor,),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        started = await client.post(
            "/api/v1/downloads",
            json={"repository_id": "fixtures/compatible", "variant": "int8"},
        )
        completed = await app.state.model_service.wait_for_download(started.json()["id"])
        model = (await client.get("/api/v1/models")).json()[0]

    assert started.status_code == 202
    assert completed.state is DownloadState.COMPLETED
    assert model["compatibility_evidence"]["evidence"][0]["message"] == (
        "The adapter supplied compatibility evidence."
    )
    with sqlite3.connect(layout.database_path) as connection:
        persisted = connection.execute(
            "SELECT compatibility_evidence_json FROM model_installations"
        ).fetchone()[0]
    assert unsafe_message not in persisted
    assert "secret" not in persisted


@pytest.mark.asyncio
async def test_http_cancel_route_returns_durable_cancelled_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    layout = StorageLayout.from_root(data_dir)
    layout.ensure()
    supervisor = DownloadSupervisor(layout)
    supervisor.block_download = True
    descriptor = AdapterDescriptor(
        engine_id="fake",
        priority=10,
        launch=WorkerLaunchSpec(command=("fake-worker",), cwd=tmp_path),
    )
    app = create_app(
        Settings.resolve(data_dir),
        supervisor=cast(WorkerSupervisor, supervisor),
        adapters=(descriptor,),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        started = await client.post(
            "/api/v1/downloads",
            json={"repository_id": "fixtures/compatible", "variant": "int8"},
        )
        job_id = started.json()["id"]
        await asyncio.wait_for(supervisor.download_started.wait(), timeout=1)
        cancel = await client.post(f"/api/v1/downloads/{job_id}/cancel")
        cancelled = await app.state.model_service.wait_for_download(job_id)

        assert cancel.status_code == 200
        assert cancel.json()["cancellation_requested"] is True
        assert cancelled.state is DownloadState.CANCELLED
        assert (await client.get(f"/api/v1/downloads/{job_id}")).json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_core_downloads_through_the_real_isolated_fake_worker(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    app = create_app(Settings.resolve(data_dir), include_test_adapters=True)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/downloads",
            json={"repository_id": "fixtures/compatible", "variant": "int8"},
        )
        assert response.status_code == 202
        completed = await app.state.model_service.wait_for_download(response.json()["id"])

        assert completed.state is DownloadState.COMPLETED
        models = (await client.get("/api/v1/models")).json()
        assert len(models) == 1
        assert models[0]["resolved_commit"] == "1c6d281855eeb808859fc335a5ef01f66e82f4a3"
        assert (data_dir / models[0]["cache_path"] / "model.bin").is_file()
