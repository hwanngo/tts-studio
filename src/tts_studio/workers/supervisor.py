"""Launch and supervise isolated engine Worker processes."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from asyncio.subprocess import Process
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

import grpc
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc

from tts_studio.models.registry import ModelInstallation
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.generation import (
    AlignmentCapability,
    GrpcWorkerLease,
    WorkerCapabilities,
    WorkerCapacityError,
    WorkerLease,
    WorkerOperationError,
)
from tts_studio.workers.process import WorkerLaunchSpec, WorkerProcess, WorkerStatus

_TOKEN_METADATA_KEY = "x-tts-worker-token"
_OWNER_CLAIM_ENV = "TTS_STUDIO_WORKER_OWNER_CLAIM"
_PROTOCOL_MAJOR = 1
_PROTOCOL_MIN_MINOR = 0
_PROTOCOL_MAX_MINOR = 1
_CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEALTH_TIMEOUT_SECONDS = 1.0
_DESCRIBE_TIMEOUT_SECONDS = 10.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_ORPHAN_TERMINATION_TIMEOUT_SECONDS = 0.25
_RESTART_MAX_ATTEMPTS = 3
_RESTART_BASE_DELAY_SECONDS = 0.05
_RESTART_MAX_DELAY_SECONDS = 1.0
_SUPERVISION_INTERVAL_SECONDS = 0.5
_MAX_READY_BYTES = 16 * 1024
@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    process_group: int
    state: str
    start_time: str
    command_line: str

    @property
    def command_sha256(self) -> str:
        return hashlib.sha256(self.command_line.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _GroupSnapshot:
    process_group: int
    members: tuple[_ProcessIdentity, ...]


_PLATFORM_ENVIRONMENT_KEYS = frozenset(
    key.casefold()
    for key in (
        "PATH",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    )
)


class WorkerSupervisor:
    """Own the private child processes and gRPC channels used by the Core."""

    def __init__(self, layout: StorageLayout, startup_timeout: float) -> None:
        self._layout = layout
        self._startup_timeout = startup_timeout
        self._workers: dict[str, WorkerProcess] = {}
        self._replicas: dict[str, dict[int, WorkerProcess]] = {}
        self._launches: dict[str, WorkerLaunchSpec] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._leased_engines: set[tuple[str, int]] = set()
        self._watch_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._replica_status: dict[tuple[str, int], WorkerStatus] = {}
        self._restart_attempts: dict[tuple[str, int], int] = {}
        self._startup_diagnostics: dict[str, dict[str, object]] = {}
        self._stopping = False

    async def start(
        self, engine_id: str, launch: WorkerLaunchSpec, *, replica_id: int = 0
    ) -> WorkerProcess:
        """Launch one Worker and admit it only after an authenticated handshake."""
        async with self._lifecycle_lock:
            self._stopping = False
            await self._reconcile_orphans()
            try:
                return await self._start_locked(engine_id, launch, replica_id)
            except Exception as error:
                self._startup_diagnostics[engine_id] = {
                    "status": "failed", "message": _safe_diagnostic(error), "restart_count": 0
                }
                raise

    async def _start_locked(
        self, engine_id: str, launch: WorkerLaunchSpec, replica_id: int = 0
    ) -> WorkerProcess:
        if not isinstance(replica_id, int) or isinstance(replica_id, bool) or replica_id < 0:
            raise ValueError("replica_id must be a non-negative integer")
        if replica_id in self._replicas.get(engine_id, {}):
            raise ValueError(f"worker {engine_id!r} replica {replica_id} is already running")

        process: Process | None = None
        channel: grpc.aio.Channel | None = None
        ready_file: Path | None = None
        token_file: Path | None = None
        owner_file: Path | None = None
        try:
            self._layout.ensure()
            token = secrets.token_urlsafe(32)
            owner_claim = secrets.token_hex(32)
            launch_id = uuid4().hex
            file_key = _engine_file_key(f"{engine_id}-replica-{replica_id}")
            run_directory = self._layout.checked_directory("run")
            token_file = _write_launch_token(
                run_directory / f"worker-{file_key}-{launch_id}.token",
                token,
            )
            ready_file = run_directory / f"worker-{file_key}-{launch_id}.json"
            owner_file = (
                run_directory / f"worker-{file_key}-{launch_id}.owner"
                if os.name != "nt"
                else None
            )
            if owner_file is not None:
                _write_owner_record(
                    owner_file,
                    None,
                    None,
                    owner_claim,
                    ready_file,
                    token_file,
                )
            arguments = [
                *launch.command,
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--token-file",
                str(token_file),
                "--ready-file",
                str(ready_file),
                "--data-dir",
                str(self._layout.root),
                "--owner-claim",
                owner_claim,
            ]

            logs_directory = self._layout.checked_directory("logs")
            stdout_path = logs_directory / f"worker-{file_key}-{launch_id}.stdout.log"
            stderr_path = logs_directory / f"worker-{file_key}-{launch_id}.stderr.log"
            with _open_private_append(stdout_path) as stdout, _open_private_append(
                stderr_path
            ) as stderr:
                if os.name != "nt":
                    process = await asyncio.create_subprocess_exec(
                        *arguments,
                        stdout=stdout,
                        stderr=stderr,
                        cwd=str(launch.cwd),
                        env=_worker_environment(owner_claim),
                        start_new_session=True,
                    )
                else:  # pragma: no cover - exercised on Windows
                    process = await asyncio.create_subprocess_exec(
                        *arguments,
                        stdout=stdout,
                        stderr=stderr,
                        cwd=str(launch.cwd),
                        env=_worker_environment(owner_claim),
                    )
            if owner_file is not None:
                _finalize_owner_record(
                    owner_file,
                    process.pid,
                    owner_claim,
                    ready_file,
                    token_file,
                )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._startup_timeout
            await _wait_for_ready_file(engine_id, process, ready_file, deadline)
            self._layout.checked_directory("run")
            _unlink_managed_run_file(self._layout, token_file)
            host, port = _read_readiness(ready_file)

            channel = grpc.aio.insecure_channel(_grpc_target(host, port))
            stub = engine_pb2_grpc.EngineWorkerStub(channel)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"worker {engine_id!r} did not become ready")
            description = await stub.Describe(
                engine_pb2.DescribeRequest(),
                metadata=((_TOKEN_METADATA_KEY, token),),
                timeout=remaining,
            )
            if description.protocol.major != _PROTOCOL_MAJOR:
                raise RuntimeError(
                    f"worker {engine_id!r} uses unsupported protocol major "
                    f"{description.protocol.major}"
                )
            if not _protocol_compatible(description.protocol):
                raise RuntimeError(
                    f"worker {engine_id!r} uses unsupported protocol minor "
                    f"{description.protocol.minor}"
                )
            if description.engine_id != engine_id:
                raise RuntimeError(
                    f"worker engine identity mismatch: expected {engine_id!r}, "
                    f"received {description.engine_id!r}"
                )

            capabilities = _worker_capabilities(description)

            worker = WorkerProcess(
                engine_id=engine_id,
                token=token,
                token_file=token_file,
                ready_file=ready_file,
                process=process,
                channel=channel,
                stub=stub,
                capabilities=capabilities,
                replica_id=replica_id,
                owner_file=owner_file,
            )
            self._workers.setdefault(engine_id, worker)
            self._replicas.setdefault(engine_id, {})[replica_id] = worker
            self._launches[engine_id] = launch
            key = (engine_id, replica_id)
            self._replica_status[key] = WorkerStatus(engine_id, True, "ready", process.pid)
            restart_count = self._restart_attempts.get(key, 0)
            previous = self._startup_diagnostics.get(engine_id, {})
            diagnostic: dict[str, object] = {
                "status": "ready", "message": "worker is ready", "restart_count": restart_count
            }
            if restart_count and "last_failure" in previous:
                diagnostic["last_failure"] = previous["last_failure"]
            self._startup_diagnostics[engine_id] = diagnostic
            self._watch_tasks[key] = asyncio.create_task(self._watch_worker(worker))
            return worker
        except BaseException:  # noqa: BLE001
            try:
                await _complete_cleanup(
                    _cleanup_resources(
                        self._layout,
                        channel,
                        process,
                        ready_file,
                        token_file,
                        owner_file,
                    )
                )
            finally:
                raise

    async def health(self, engine_id: str) -> WorkerStatus:
        """Return aggregate readiness across every replica of an engine."""
        async with self._lifecycle_lock:
            workers = tuple(self._replicas.get(engine_id, {}).items())
            if not workers:
                replica_statuses = tuple(
                    status
                    for (status_engine_id, _), status in self._replica_status.items()
                    if status_engine_id == engine_id
                )
                if replica_statuses:
                    status = next(
                        (status for status in replica_statuses if not status.ready),
                        replica_statuses[0],
                    )
                    if status.message.startswith("worker exited unexpectedly"):
                        return WorkerStatus(
                            status.engine_id,
                            status.ready,
                            "health check failed: UNAVAILABLE",
                            status.pid,
                        )
                    return status
                if engine_id in self._startup_diagnostics:
                    diagnostic = self._startup_diagnostics[engine_id]
                    message = str(diagnostic["message"])
                    if message.startswith("worker exited unexpectedly"):
                        message = "health check failed: UNAVAILABLE"
                    return WorkerStatus(engine_id, False, message, None)
                return WorkerStatus(engine_id, False, "worker is not running", None)
        if not workers:
            return WorkerStatus(engine_id, False, "worker is not running", None)
        results = await asyncio.gather(*(self._health_replica(engine_id, rid, worker) for rid, worker in workers))
        return next((status for status in results if not status.ready), results[0])

    async def _health_replica(self, engine_id: str, replica_id: int, worker: WorkerProcess) -> WorkerStatus:
        async with self._lifecycle_lock:
            known = self._replica_status.get((engine_id, replica_id))
        if known is not None and not known.ready:
            if known.message.startswith("worker exited unexpectedly"):
                return WorkerStatus(
                    known.engine_id,
                    known.ready,
                    "health check failed: UNAVAILABLE",
                    known.pid,
                )
            return known
        try:
            response = await asyncio.wait_for(
                worker.stub.Health(
                    engine_pb2.HealthRequest(),
                    metadata=((_TOKEN_METADATA_KEY, worker.token),),
                    timeout=_HEALTH_TIMEOUT_SECONDS,
                ),
                timeout=_HEALTH_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001
            code = error.code().name if isinstance(error, grpc.aio.AioRpcError) else type(error).__name__
            status = WorkerStatus(
                engine_id=engine_id,
                ready=False,
                message=f"health check failed: {code}",
                pid=worker.process.pid,
            )
            async with self._lifecycle_lock:
                self._replica_status[(engine_id, replica_id)] = status
            return status

        status = WorkerStatus(
            engine_id=engine_id,
            ready=response.status == engine_pb2.HealthResponse.READY,
            message=_safe_health_message(response.status),
            pid=worker.process.pid,
        )
        async with self._lifecycle_lock:
            self._replica_status[(engine_id, replica_id)] = status
        return status

    async def statuses(self) -> tuple[WorkerStatus, ...]:
        """Return an immutable, engine-ordered aggregate snapshot."""
        async with self._lifecycle_lock:
            engine_ids = tuple(sorted(set(self._replicas) | set(self._startup_diagnostics)))
        return tuple(await asyncio.gather(*(self.health(engine_id) for engine_id in engine_ids)))

    def startup_diagnostics(self) -> dict[str, dict[str, object]]:
        return {engine: dict(value) for engine, value in self._startup_diagnostics.items()}

    async def _reconcile_orphans(self) -> None:
        await asyncio.to_thread(self._reconcile_orphans_sync)

    def _reconcile_orphans_sync(self) -> None:
        try:
            run_directory = self._layout.checked_directory("run")
        except (OSError, RuntimeError):
            return
        registered_pids = {worker.process.pid for worker in self._replica_values()}
        for owner_file in run_directory.glob("worker-*.owner"):
            try:
                metadata = owner_file.lstat()
                if (
                    owner_file.is_symlink()
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > _MAX_READY_BYTES
                ):
                    continue
                record = json.loads(owner_file.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    continue
                pid = record.get("pid")
                if pid is None:
                    pending_pids = _pending_owner_processes(record)
                    if pending_pids is None or len(pending_pids) > 1:
                        continue
                    if not pending_pids:
                        _remove_owner_record_files(
                            self._layout, run_directory, owner_file, record
                        )
                        continue
                    identity = pending_pids[0]
                    pid = identity.pid
                    record = {
                        **record,
                        "pid": pid,
                        "process_group": pid,
                        "process_start": identity.start_time,
                        "command_sha256": identity.command_sha256,
                    }
                if type(pid) is not int or pid <= 0 or pid in registered_pids:
                    continue
                if not _terminate_verified_orphan(pid, record):
                    continue
                _remove_owner_record_files(self._layout, run_directory, owner_file, record)
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, RuntimeError):
                continue

    async def _watch_worker(self, worker: WorkerProcess) -> None:
        key = (worker.engine_id, worker.replica_id)
        failure_message = "worker became unreachable"
        while True:
            wait_task = asyncio.create_task(worker.process.wait())
            try:
                done, _ = await asyncio.wait((wait_task,), timeout=_SUPERVISION_INTERVAL_SECONDS)
            finally:
                if not wait_task.done():
                    wait_task.cancel()
            if done:
                failure_message = f"worker exited unexpectedly (code {worker.process.returncode})"
                break
            async with self._lifecycle_lock:
                if self._stopping:
                    return
            status = await self._health_replica(worker.engine_id, worker.replica_id, worker)
            if not status.ready:
                break
        async with self._lifecycle_lock:
            if (
                self._stopping
                or self._replicas.get(worker.engine_id, {}).get(worker.replica_id) is not worker
            ):
                return
            self._replica_status[key] = WorkerStatus(
                worker.engine_id, False, failure_message, worker.process.pid
            )
            launch = self._launches[worker.engine_id]
            self._replicas[worker.engine_id].pop(worker.replica_id, None)
            if self._workers.get(worker.engine_id) is worker:
                replacement = next(iter(self._replicas[worker.engine_id].values()), None)
                if replacement is not None:
                    self._workers[worker.engine_id] = replacement
                else:
                    self._workers.pop(worker.engine_id, None)
        try:
            await _cleanup_resources(
                self._layout,
                worker.channel,
                worker.process,
                worker.ready_file,
                worker.token_file,
                worker.owner_file,
            )
        except Exception as error:  # noqa: BLE001 - cleanup failure exhausts safe replacement
            async with self._lifecycle_lock:
                self._startup_diagnostics[worker.engine_id] = {
                    "status": "failed",
                    "message": _safe_diagnostic(error),
                    "last_failure": failure_message,
                    "restart_count": self._restart_attempts.get(key, 0),
                }
            return

        for consecutive_attempt in range(1, _RESTART_MAX_ATTEMPTS + 1):
            await asyncio.sleep(
                min(
                    _RESTART_BASE_DELAY_SECONDS * (2 ** (consecutive_attempt - 1)),
                    _RESTART_MAX_DELAY_SECONDS,
                )
            )
            async with self._lifecycle_lock:
                if self._stopping or worker.replica_id in self._replicas.get(worker.engine_id, {}):
                    return
                restart_count = self._restart_attempts.get(key, 0) + 1
                self._restart_attempts[key] = restart_count
                self._startup_diagnostics[worker.engine_id] = {
                    "status": "restarting",
                    "message": failure_message,
                    "last_failure": failure_message,
                    "restart_count": restart_count,
                }
                try:
                    await self._start_locked(worker.engine_id, launch, worker.replica_id)
                except Exception as error:  # noqa: BLE001 - retry bounded replacement launch
                    self._startup_diagnostics[worker.engine_id] = {
                        "status": (
                            "failed"
                            if consecutive_attempt == _RESTART_MAX_ATTEMPTS
                            else "restarting"
                        ),
                        "message": _safe_diagnostic(error),
                        "last_failure": failure_message,
                        "restart_count": restart_count,
                    }
                else:
                    return
        self._watch_tasks.pop(key, None)


    async def validate_model(
        self,
        engine_id: str,
        request: engine_pb2.ValidateModelRequest,
        *,
        timeout: float = 10.0,
    ) -> engine_pb2.ValidateModelResponse:
        """Validate a repository through one authenticated Worker RPC."""
        worker = await self._running_worker(engine_id)
        return await worker.stub.ValidateModel(
            request,
            metadata=((_TOKEN_METADATA_KEY, worker.token),),
            timeout=timeout,
        )

    async def describe(self, engine_id: str) -> engine_pb2.DescribeResponse:
        """Describe a running Worker through its authenticated connection."""
        worker = await self._running_worker(engine_id)
        response = await worker.stub.Describe(
            engine_pb2.DescribeRequest(),
            metadata=((_TOKEN_METADATA_KEY, worker.token),),
            timeout=_DESCRIBE_TIMEOUT_SECONDS,
        )
        if response.protocol.major != _PROTOCOL_MAJOR:
            raise RuntimeError(
                f"worker {engine_id!r} uses unsupported protocol major "
                f"{response.protocol.major}"
            )
        if not _protocol_compatible(response.protocol):
            raise RuntimeError(
                f"worker {engine_id!r} uses unsupported protocol minor "
                f"{response.protocol.minor}"
            )
        if response.engine_id != worker.engine_id:
            raise RuntimeError(
                f"worker engine identity mismatch: expected {worker.engine_id!r}, "
                f"received {response.engine_id!r}"
            )
        capabilities = _worker_capabilities(response)
        async with self._lifecycle_lock:
            if self._workers.get(engine_id) is worker:
                worker.capabilities = capabilities
        return response

    async def download_model(
        self,
        engine_id: str,
        request: engine_pb2.DownloadModelRequest,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        """Stream an authenticated download and propagate consumer cancellation."""
        worker = await self._running_worker(engine_id)
        call = worker.stub.DownloadModel(
            request,
            metadata=((_TOKEN_METADATA_KEY, worker.token),),
            timeout=timeout,
        )
        try:
            async for event in call:
                yield event
        finally:
            call.cancel()

    async def unload_model(self, engine_id: str, model_id: str, cache_path: Path) -> None:
        """Release Worker resources before Core retires the installation files."""
        del cache_path  # File ownership stays with Core; the Worker receives identity only.
        async with self._lifecycle_lock:
            worker = self._workers.get(engine_id)
            if worker is None:
                raise RuntimeError(f"worker {engine_id!r} is not running")
            if any(key[0] == engine_id for key in self._leased_engines):
                raise WorkerCapacityError("one active generation lease is already held")
            workers = tuple(self._replicas.get(engine_id, {}).values()) or (worker,)
            for replica in workers:
                response = await replica.stub.UnloadModel(
                    engine_pb2.UnloadModelRequest(model_id=model_id),
                    metadata=((_TOKEN_METADATA_KEY, replica.token),),
                    timeout=10.0,
                )
                if response.HasField("error"):
                    if response.error.code == "model_not_loaded":
                        replica.loaded_model_id = None
                    raise WorkerOperationError(response.error)
                if not response.unloaded:
                    raise RuntimeError("Worker did not unload the model")
                replica.loaded_model_id = None

    async def ensure_replicas(self, model: ModelInstallation, count: int) -> None:
        """Scale independently launched Worker processes for one engine."""
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 8:
            raise ValueError("replica count must be between 1 and 8")
        engine_id = _model_engine_id(model)
        async with self._lifecycle_lock:
            launch = self._launches.get(engine_id)
            if launch is None:
                raise RuntimeError(f"worker {engine_id!r} is not running")
            existing = self._replicas.setdefault(engine_id, {})
            if len(existing) > count:
                retiring = sorted(existing, reverse=True)[: len(existing) - count]
                if any((engine_id, replica_id) in self._leased_engines for replica_id in retiring):
                    raise WorkerCapacityError("replica scale-down requires an idle Worker")
                for replica_id in retiring:
                    worker = existing[replica_id]
                    await self._retire_worker(worker)
                    existing.pop(replica_id, None)
                if self._workers.get(engine_id) not in existing.values():
                    self._workers[engine_id] = next(iter(existing.values()))
            for replica_id in range(len(existing), count):
                try:
                    await self._start_locked(engine_id, launch, replica_id)
                except Exception as error:
                    self._startup_diagnostics[engine_id] = {
                        "status": "failed",
                        "message": _safe_diagnostic(error),
                        "restart_count": self._restart_attempts.get((engine_id, replica_id), 0),
                    }
                    raise

    async def _retire_worker(self, worker: WorkerProcess) -> None:
        errors: list[BaseException] = []
        try:
            await worker.channel.close()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        terminated = False
        try:
            await _terminate_and_reap(worker.process, worker.owner_file)
            terminated = True
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        if terminated:
            for path in (worker.ready_file, worker.token_file, worker.owner_file):
                try:
                    _unlink_managed_run_file(self._layout, path)
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)
        if errors:
            raise errors[0]

    @asynccontextmanager
    async def acquire(self, model: ModelInstallation) -> AsyncIterator[WorkerLease]:
        """Acquire the single generation replica for a pinned model."""
        engine_id = _model_engine_id(model)
        async with self._lifecycle_lock:
            worker = self._workers.get(engine_id)
            if worker is None:
                raise RuntimeError(f"worker {engine_id!r} is not running")
            replicas = tuple(self._replicas.get(engine_id, {}).items())
            if not replicas:
                replicas = ((0, worker),)
            selected = next(
                ((replica_id, item) for replica_id, item in replicas if (engine_id, replica_id) not in self._leased_engines),
                None,
            )
            if selected is None:
                raise WorkerCapacityError("all Worker replicas are already leased")
            replica_id, worker = selected
            self._leased_engines.add((engine_id, replica_id))
        lease = GrpcWorkerLease(worker)
        try:
            yield lease
        finally:
            try:
                await lease.aclose()
            finally:
                async with self._lifecycle_lock:
                    self._leased_engines.discard((engine_id, replica_id))

    async def _running_worker(self, engine_id: str) -> WorkerProcess:
        async with self._lifecycle_lock:
            worker = self._workers.get(engine_id)
        if worker is None:
            raise RuntimeError(f"worker {engine_id!r} is not running")
        return worker

    async def stop_all(self) -> None:
        """Close every channel, then terminate and reap every owned child."""
        async with self._lifecycle_lock:
            self._stopping = True
            tasks = tuple(self._watch_tasks.values())
            self._watch_tasks.clear()
            for task in tasks:
                task.cancel()
            try:
                await _complete_cleanup(self._stop_registered_workers())
            finally:
                self._replica_status.clear()

    async def _stop_registered_workers(self) -> None:
        workers = list({id(worker): worker for worker in self._replica_values()}.values())
        errors: list[BaseException] = []
        reap_results: list[object] = [RuntimeError("Worker termination did not run") for _ in workers]
        try:
            close_results = await asyncio.gather(
                *(worker.channel.close() for worker in workers),
                return_exceptions=True,
            )
            errors.extend(result for result in close_results if isinstance(result, BaseException))
        finally:
            try:
                reap_results = await asyncio.gather(
                    *(
                        _terminate_and_reap(worker.process, worker.owner_file)
                        for worker in workers
                    ),
                    return_exceptions=True,
                )
                errors.extend(
                    result for result in reap_results if isinstance(result, BaseException)
                )
            finally:
                for worker, reap_result in zip(workers, reap_results, strict=True):
                    self._leased_engines = {
                        key for key in self._leased_engines if key[0] != worker.engine_id
                    }
                    if not isinstance(reap_result, BaseException):
                        for path in (worker.ready_file, worker.token_file, worker.owner_file):
                            try:
                                _unlink_managed_run_file(self._layout, path)
                            except BaseException as error:  # noqa: BLE001
                                errors.append(error)
                    self._replicas.get(worker.engine_id, {}).pop(worker.replica_id, None)
                    if self._workers.get(worker.engine_id) is worker:
                        replacement = next(iter(self._replicas.get(worker.engine_id, {}).values()), None)
                        if replacement is None:
                            self._workers.pop(worker.engine_id, None)
                            self._replicas.pop(worker.engine_id, None)
                            self._launches.pop(worker.engine_id, None)
                        else:
                            self._workers[worker.engine_id] = replacement

        if errors:
            raise errors[0]

    def _replica_values(self) -> Iterator[WorkerProcess]:
        yield from self._workers.values()
        for replicas in self._replicas.values():
            yield from replicas.values()


def _protocol_compatible(protocol: engine_pb2.ProtocolVersion) -> bool:
    return (
        protocol.major == _PROTOCOL_MAJOR
        and _PROTOCOL_MIN_MINOR <= protocol.minor <= _PROTOCOL_MAX_MINOR
    )


def _worker_capabilities(description: engine_pb2.DescribeResponse) -> WorkerCapabilities:
    if not description.engine_id or not description.engine_version:
        raise ValueError("Worker capability metadata requires engine identity and version")
    if description.max_concurrency <= 0:
        raise ValueError("Worker capability metadata max_concurrency must be positive")

    names = [item.name for item in description.capabilities]
    if any(not _CAPABILITY_NAME_RE.fullmatch(name) for name in names):
        raise ValueError("Worker capability metadata contains an invalid capability name")
    if len(names) != len(set(names)):
        raise ValueError("Worker capability metadata contains duplicate capability names")

    alignment = None
    alignment_supported = any(
        item.name == "alignment" and item.supported for item in description.capabilities
    )
    if description.HasField("alignment"):
        if not alignment_supported:
            raise ValueError("Worker alignment metadata requires a supported alignment capability")
        units = tuple(description.alignment.units)
        languages = tuple(description.alignment.languages)
        aligner = description.alignment.aligner
        if not units or any(not unit for unit in units) or len(units) != len(set(units)):
            raise ValueError("Worker alignment metadata has invalid units")
        if any(not language for language in languages) or len(languages) != len(set(languages)):
            raise ValueError("Worker alignment metadata has invalid languages")
        if not aligner:
            raise ValueError("Worker alignment metadata requires an aligner")
        alignment = AlignmentCapability(units=units, languages=languages, aligner=aligner)
    elif alignment_supported:
        raise ValueError("Worker alignment capability is missing typed metadata")

    return WorkerCapabilities(
        engine_id=description.engine_id,
        engine_version=description.engine_version,
        supported=frozenset(item.name for item in description.capabilities if item.supported),
        max_concurrency=description.max_concurrency,
        alignment=alignment,
    )


def _engine_file_key(engine_id: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", engine_id).strip(".-")[:40]
    if not safe_name:
        safe_name = "engine"
    digest = hashlib.sha256(engine_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe_name}-{digest}"


def _model_engine_id(model: ModelInstallation) -> str:
    engine_id = model.compatibility_evidence.get("engine_id")
    if isinstance(engine_id, str) and engine_id:
        return engine_id
    return model.engine_installation_id.split("@", maxsplit=1)[0]


def _worker_environment(owner_claim: str | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in _PLATFORM_ENVIRONMENT_KEYS
    }
    if owner_claim is not None:
        environment[_OWNER_CLAIM_ENV] = owner_claim
    return environment


def _write_owner_record(
    owner_file: Path,
    pid: int | None,
    process_group: int | None,
    owner_claim: str,
    ready_file: Path,
    token_file: Path,
    *,
    process_start: str | None = None,
    command_sha256: str | None = None,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(owner_file, flags, 0o600)
    try:
        _write_owner_document(
            descriptor,
            pid,
            process_group,
            owner_claim,
            ready_file,
            token_file,
            process_start=process_start,
            command_sha256=command_sha256,
        )
    finally:
        os.close(descriptor)
    _fsync_directory(owner_file.parent)


def _finalize_owner_record(
    owner_file: Path,
    pid: int,
    owner_claim: str,
    ready_file: Path,
    token_file: Path,
) -> None:
    identity = _process_identity(pid)
    if identity is None or identity.process_group != pid:
        raise RuntimeError("Worker process identity could not be recorded")
    temporary = owner_file.with_name(
        f".{owner_file.name}.finalize-{uuid4().hex}.tmp"
    )
    try:
        _write_owner_record(
            temporary,
            pid,
            pid,
            owner_claim,
            ready_file,
            token_file,
            process_start=identity.start_time,
            command_sha256=identity.command_sha256,
        )
        os.replace(temporary, owner_file)
        _fsync_directory(owner_file.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_owner_document(
    descriptor: int,
    pid: int | None,
    process_group: int | None,
    owner_claim: str,
    ready_file: Path,
    token_file: Path,
    *,
    process_start: str | None = None,
    command_sha256: str | None = None,
) -> None:
    payload = json.dumps(
        {
            "pid": pid,
            "process_group": process_group,
            "process_start": process_start,
            "command_sha256": command_sha256,
            "owner_claim": owner_claim,
            "ready": ready_file.name,
            "token": token_file.name,
        },
        sort_keys=True,
    ).encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("Worker owner record made no write progress")
        offset += written
    os.fsync(descriptor)


def _remove_owner_record_files(
    layout: StorageLayout,
    run_directory: Path,
    owner_file: Path,
    record: dict[str, object],
) -> None:
    for name in (record.get("ready"), record.get("token"), owner_file.name):
        if (
            isinstance(name, str)
            and name.startswith("worker-")
            and Path(name).name == name
        ):
            _unlink_managed_run_file(layout, run_directory / name)


def _verified_worker_owner(pid: int, record: dict[str, object]) -> bool:
    snapshot = _verified_group_snapshot(record)
    return snapshot is not None and any(member.pid == pid for member in snapshot.members)


def _command_has_owner_claim(command_line: str, owner_claim: str) -> bool:
    claim_pattern = re.compile(
        rf"(?:^|\s)--owner-claim(?:=|\s+){re.escape(owner_claim)}(?:\s|$)"
    )
    return claim_pattern.search(command_line) is not None


def _pending_owner_processes(
    record: dict[str, object],
) -> tuple[_ProcessIdentity, ...] | None:
    owner_claim = _owner_claim(record)
    if owner_claim is None:
        return None
    processes = _process_table()
    if processes is None:
        return None
    return tuple(
        process
        for process in processes
        if process.pid == process.process_group
        and not process.state.startswith("Z")
        and _command_has_owner_claim(process.command_line, owner_claim)
        and (
            sys.platform == "linux"
            or _process_has_owner_claim(process.pid, owner_claim)
        )
    )


def _owner_claim(record: dict[str, object]) -> str | None:
    owner_claim = record.get("owner_claim")
    if not isinstance(owner_claim, str) or not owner_claim or len(owner_claim) > 256:
        return None
    return owner_claim


def _process_table() -> tuple[_ProcessIdentity, ...] | None:
    if os.name == "nt":
        return None
    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        completed = subprocess.run(
            [ps, "-axo", "pid=,pgid=,stat=,lstart=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    processes: list[_ProcessIdentity] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=8)
        if len(fields) != 9:
            continue
        try:
            pid = int(fields[0])
            process_group = int(fields[1])
        except ValueError:
            continue
        processes.append(
            _ProcessIdentity(
                pid=pid,
                process_group=process_group,
                state="Z" if fields[2].startswith("Z") else "",
                start_time=" ".join(fields[3:8]),
                command_line=fields[8],
            )
        )
    return tuple(processes)


def _process_identity(pid: int) -> _ProcessIdentity | None:
    processes = _process_table()
    if processes is None:
        return None
    return next((process for process in processes if process.pid == pid), None)


def _process_group_identities(process_group: int) -> tuple[_ProcessIdentity, ...] | None:
    processes = _process_table()
    if processes is None:
        return None
    return tuple(
        sorted(
            (
                process
                for process in processes
                if process.process_group == process_group
                and not process.state.startswith("Z")
            ),
            key=lambda process: process.pid,
        )
    )


def _process_group_members(process_group: int) -> frozenset[int] | None:
    identities = _process_group_identities(process_group)
    if identities is None:
        return None
    return frozenset(identity.pid for identity in identities)


def _surviving_group_members(
    process_group: int, original_members: frozenset[int]
) -> frozenset[int]:
    identities = _process_group_identities(process_group)
    if identities is None:
        return original_members
    current = {identity.pid for identity in identities}
    return frozenset(original_members & current)


def _process_has_owner_claim(pid: int, owner_claim: str) -> bool:
    proc_environment = Path(f"/proc/{pid}/environ")
    expected = f"{_OWNER_CLAIM_ENV}={owner_claim}".encode()
    try:
        values = proc_environment.read_bytes().split(b"\0")
        if expected in values:
            return True
    except OSError:
        pass
    ps = shutil.which("ps")
    if ps is None:
        return False
    try:
        completed = subprocess.run(
            [ps, "eww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    environment_pattern = re.compile(
        rf"(?:^|\s){re.escape(_OWNER_CLAIM_ENV)}={re.escape(owner_claim)}(?:\s|$)"
    )
    return (
        environment_pattern.search(completed.stdout) is not None
        or _command_has_owner_claim(completed.stdout, owner_claim)
    )


def _verified_group_snapshot(record: dict[str, object]) -> _GroupSnapshot | None:
    pid = record.get("pid")
    process_group = record.get("process_group")
    owner_claim = _owner_claim(record)
    if (
        type(pid) is not int
        or pid <= 0
        or type(process_group) is not int
        or process_group != pid
        or owner_claim is None
        or not hasattr(os, "killpg")
    ):
        return None
    members = _process_group_identities(process_group)
    if members is None:
        return None
    if not members:
        return _GroupSnapshot(process_group, ())
    leader = next((member for member in members if member.pid == pid), None)
    process_start = record.get("process_start")
    command_sha256 = record.get("command_sha256")
    if leader is not None:
        if (
            not isinstance(process_start, str)
            or leader.start_time != process_start
            or not isinstance(command_sha256, str)
            or leader.command_sha256 != command_sha256
            or (
                sys.platform != "linux"
                and (
                    not _process_has_owner_claim(pid, owner_claim)
                    or not _command_has_owner_claim(leader.command_line, owner_claim)
                )
            )
        ):
            return None
    elif not isinstance(process_start, str) or not isinstance(command_sha256, str):
        return None
    return _GroupSnapshot(process_group, members)


def _signal_verified_group(
    record: dict[str, object], expected: _GroupSnapshot, signum: int
) -> bool:
    current = _verified_group_snapshot(record)
    if current != expected:
        return False
    if not current.members:
        return True
    try:
        os.killpg(current.process_group, signum)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _terminate_verified_orphan(pid: int, record: dict[str, object]) -> bool:
    del pid
    snapshot = _verified_group_snapshot(record)
    if snapshot is None:
        return False
    if not snapshot.members:
        return True
    if not _signal_verified_group(record, snapshot, signal.SIGTERM):
        return False
    deadline = time.monotonic() + _ORPHAN_TERMINATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = _verified_group_snapshot(record)
        if current is not None and not current.members:
            return True
        time.sleep(0.01)
    current = _verified_group_snapshot(record)
    if current is None:
        return False
    if not current.members:
        return True
    return _signal_verified_group(record, current, signal.SIGKILL)


def _write_launch_token(token_file: Path, token: str) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(token_file, flags, 0o600)
    try:
        try:
            _restrict_file_mode(descriptor, token_file)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
                descriptor = -1
            token_file.unlink(missing_ok=True)
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return token_file


@contextmanager
def _open_private_append(path: Path) -> Iterator[BinaryIO]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _restrict_file_mode(descriptor, path)
        with os.fdopen(descriptor, "ab") as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restrict_file_mode(descriptor: int, path: Path) -> None:
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(descriptor, 0o600)
    else:
        os.chmod(path, 0o600)


def _unlink_managed_run_file(layout: StorageLayout, path: Path | None) -> None:
    if path is None:
        return
    run_directory = layout.checked_directory("run")
    if path.parent != run_directory:
        raise RuntimeError("refusing to remove a file outside the managed run directory")
    path.unlink(missing_ok=True)


async def _wait_for_ready_file(
    engine_id: str,
    process: Process,
    ready_file: Path,
    deadline: float,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            _read_readiness(ready_file)
            return
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError, ValueError, TypeError):
            if ready_file.exists():
                raise
        if process.returncode is not None:
            raise RuntimeError(f"worker {engine_id!r} exited before publishing readiness")
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"worker {engine_id!r} did not become ready")
        await asyncio.sleep(min(0.01, remaining))


def _read_readiness(ready_file: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(ready_file, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RuntimeError("worker ready file must be a regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("worker ready file must be a regular file")
        if metadata.st_size > _MAX_READY_BYTES:
            raise ValueError("worker ready file is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = stream.read(_MAX_READY_BYTES + 1)
        if len(payload.encode("utf-8")) > _MAX_READY_BYTES:
            raise ValueError("worker ready file is too large")
        current = ready_file.stat()
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("worker ready file identity changed")
        document: Any = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("worker published an invalid ready file") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not isinstance(document, dict):
        raise TypeError("worker published an invalid ready file")
    host = document.get("host")
    port = document.get("port")
    if not isinstance(host, str):
        raise TypeError("worker ready-file host must be a loopback IP address")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("worker ready-file host must be a loopback IP address") from error
    if not address.is_loopback:
        raise ValueError("worker ready-file host must be a loopback IP address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("worker ready-file port must be an integer between 1 and 65535")
    return host, port


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_health_message(status: int) -> str:
    return "" if status == engine_pb2.HealthResponse.READY else "worker is unhealthy"


def _safe_diagnostic(error: BaseException) -> str:
    return f"{type(error).__name__}: worker startup failed"


def _grpc_target(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


async def _complete_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    cleanup_task = asyncio.create_task(cleanup)
    cancellation: asyncio.CancelledError | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as error:
            cancellation = error

    cleanup_task.result()
    if cancellation is not None:
        raise cancellation


async def _cleanup_resources(
    layout: StorageLayout,
    channel: grpc.aio.Channel | None,
    process: Process | None,
    ready_file: Path | None,
    token_file: Path | None,
    owner_file: Path | None = None,
) -> None:
    errors: list[BaseException] = []
    if channel is not None:
        try:
            await channel.close()
        except BaseException as error:  # noqa: BLE001 - finish verified process cleanup
            errors.append(error)
    terminated = process is None
    if process is not None:
        try:
            await _terminate_and_reap(process, owner_file)
            terminated = True
        except BaseException as error:  # noqa: BLE001 - retain ownership record on uncertainty
            errors.append(error)
    if terminated:
        for path in (ready_file, token_file, owner_file):
            try:
                _unlink_managed_run_file(layout, path)
            except BaseException as error:  # noqa: BLE001 - report cleanup failure after termination
                errors.append(error)
    if errors:
        raise errors[0]


async def _terminate_and_reap(
    process: Process, owner_file: Path | None = None
) -> None:
    containment = (
        _verified_process_group(process.pid, owner_file)
        if owner_file is not None and os.name != "nt"
        else None
    )
    if owner_file is not None and os.name != "nt" and containment is None:
        raise RuntimeError("verified Worker process-group containment is unavailable")
    if containment is None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        return

    record, snapshot = containment
    if snapshot.members and not _signal_verified_group(record, snapshot, signal.SIGTERM):
        raise RuntimeError("Worker process-group identity changed before termination")
    try:
        await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        current = _verified_group_snapshot(record)
        if current is None or (
            current.members
            and not _signal_verified_group(record, current, signal.SIGKILL)
        ):
            raise RuntimeError("Worker process-group identity changed before forced termination")
        await process.wait()

    deadline = asyncio.get_running_loop().time() + _ORPHAN_TERMINATION_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        current = _verified_group_snapshot(record)
        if current is not None and not current.members:
            return
        await asyncio.sleep(0.01)
    current = _verified_group_snapshot(record)
    if current is None or (
        current.members and not _signal_verified_group(record, current, signal.SIGKILL)
    ):
        raise RuntimeError("Worker helper process-group identity changed before termination")
    kill_deadline = asyncio.get_running_loop().time() + _ORPHAN_TERMINATION_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < kill_deadline:
        current = _verified_group_snapshot(record)
        if current is None:
            raise RuntimeError("Worker process-group identity became unverifiable")
        if not current.members:
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("verified Worker process group did not terminate")


def _verified_process_group(
    pid: int, owner_file: Path
) -> tuple[dict[str, object], _GroupSnapshot] | None:
    record = _read_owner_record(owner_file)
    if record is None:
        return None
    recorded_pid = record.get("pid")
    process_group = record.get("process_group")
    if recorded_pid is None and process_group is None:
        identity = _process_identity(pid)
        if identity is None or identity.process_group != pid:
            return None
        record = {
            **record,
            "pid": pid,
            "process_group": pid,
            "process_start": identity.start_time,
            "command_sha256": identity.command_sha256,
        }
    elif recorded_pid != pid or process_group != pid:
        return None
    snapshot = _verified_group_snapshot(record)
    if snapshot is None:
        return None
    return record, snapshot


def _read_owner_record(owner_file: Path) -> dict[str, object] | None:
    try:
        metadata = owner_file.lstat()
        if (
            owner_file.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_READY_BYTES
        ):
            return None
        record = json.loads(owner_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None
