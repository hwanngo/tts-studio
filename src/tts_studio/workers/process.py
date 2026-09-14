"""Runtime records for supervised engine Worker processes."""

from __future__ import annotations

from asyncio import Task
from asyncio.subprocess import Process
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import grpc

if TYPE_CHECKING:
    from tts_studio_protocol.engine.v1.engine_pb2_grpc import EngineWorkerAsyncStub

    from tts_studio.workers.generation import WorkerCapabilities


@dataclass(frozen=True)
class WorkerLaunchSpec:
    """The complete non-secret process context required to start one Worker."""

    command: tuple[str, ...]
    cwd: Path

    def __post_init__(self) -> None:
        if not self.command or not self.command[0]:
            raise ValueError("worker command must not be empty")
        if any(not isinstance(argument, str) for argument in self.command):
            raise TypeError("worker command arguments must be strings")

        try:
            resolved_cwd = self.cwd.expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("worker cwd must be an existing directory") from error
        if not resolved_cwd.is_dir():
            raise ValueError("worker cwd must be an existing directory")
        object.__setattr__(self, "cwd", resolved_cwd)


@dataclass(frozen=True)
class WorkerStatus:
    """A current, non-durable observation of Worker readiness."""

    engine_id: str
    ready: bool
    message: str
    pid: int | None


@dataclass
class WorkerProcess:
    """Resources owned by one launched engine Worker."""

    engine_id: str
    token: str = field(repr=False)
    token_file: Path
    ready_file: Path
    process: Process
    channel: grpc.aio.Channel
    stub: EngineWorkerAsyncStub
    capabilities: WorkerCapabilities
    loaded_model_id: str | None = None
    replica_id: int = 0
    owner_file: Path | None = None
    quarantined: bool = False
    terminated: bool = False
    termination_task: Task[None] | None = field(default=None, repr=False, compare=False)
    hard_stop_task: Task[None] | None = field(default=None, repr=False, compare=False)
