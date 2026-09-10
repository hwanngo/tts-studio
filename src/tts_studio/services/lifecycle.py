"""Application-facing lifecycle delegation without platform command assembly."""

from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tts_studio.client import CoreApiError, CoreClient, CoreUnavailable
from tts_studio.runtime import CoreRunRecord, CoreRunStore, core_health_url

from .manager import ServiceManager


class LifecycleError(RuntimeError):
    """Base error for bounded lifecycle operations."""


class LifecycleUnsupportedError(LifecycleError):
    """Raised when an operation cannot safely be controlled in this process."""


class LifecycleOperationError(LifecycleError):
    """Raised when a delegated lifecycle operation fails."""


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    """Safe service state, including explicit unknown/unsupported values."""

    status: Literal[
        "not_installed", "installed", "running", "healthy", "unavailable", "unsupported"
    ]
    installed: bool | None
    running: bool
    healthy: bool | None
    message: str


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Typed result returned after a delegated service operation."""

    operation: Literal["install", "uninstall", "restart"]
    changed: bool
    message: str


HealthProbe = Callable[[str], bool]
StopRecord = Callable[[CoreRunStore, CoreRunRecord, float], None]


class LifecycleAdapter:
    """Coordinate service manager and Core run records for application callers.

    Platform-specific commands remain entirely inside ``ServiceManager``. The
    adapter deliberately does not invent a way to restart the process hosting an
    in-process FastAPI application.
    """

    def __init__(
        self,
        manager: ServiceManager,
        run_store: CoreRunStore,
        *,
        health_probe: HealthProbe | None = None,
        stop_record: StopRecord | None = None,
        timeout: float = 10.0,
        in_process: bool = False,
    ) -> None:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        self.manager = manager
        self.run_store = run_store
        self._health_probe = health_probe or _probe_core
        self._stop_record = stop_record
        self._timeout = timeout
        self._in_process = in_process

    def status(self) -> ServiceSnapshot:
        """Reconcile the managed run record and probe health without secrets."""
        try:
            record = self.run_store.reconcile()
        except Exception as error:
            raise LifecycleOperationError("could not read Core run record") from error
        installed = _installed(self.manager)
        if record is None:
            if installed is False:
                return ServiceSnapshot(
                    "not_installed", installed, False, None, "service is not installed"
                )
            if installed is None:
                return ServiceSnapshot(
                    "unsupported",
                    installed,
                    False,
                    None,
                    "service installation state is unsupported",
                )
            return ServiceSnapshot("installed", installed, False, None, "Core is not running")
        try:
            healthy = bool(self._health_probe(core_health_url(record.host, record.port)))
        except CoreUnavailable, CoreApiError, OSError, ValueError:
            healthy = False
        if healthy:
            return ServiceSnapshot("healthy", installed, True, True, "Core is healthy")
        return ServiceSnapshot("unavailable", installed, True, False, "Core is unavailable")

    def install(self) -> LifecycleResult:
        if self._in_process:
            raise LifecycleUnsupportedError(
                "service install is unsupported while Core is already running in this process"
            )
        try:
            record = self.run_store.reconcile()
            if record is not None:
                raise LifecycleUnsupportedError(
                    "service install is unsupported while Core is already running"
                )
            self.manager.install()
        except LifecycleUnsupportedError:
            raise
        except Exception as error:
            raise LifecycleOperationError("could not install Core service") from error
        return LifecycleResult("install", True, "Core service installed")

    def uninstall(self) -> LifecycleResult:
        try:
            record = self.run_store.reconcile()
            if record is not None:
                if self._stop_record is None:
                    raise LifecycleUnsupportedError(
                        "uninstall cannot stop a Core owned by the current process"
                    )
                self._stop_record(self.run_store, record, self._timeout)
            changed = bool(self.manager.uninstall())
        except LifecycleUnsupportedError:
            raise
        except Exception as error:
            raise LifecycleOperationError("could not uninstall Core service") from error
        message = "Core service uninstalled" if changed else "Core service is not installed"
        return LifecycleResult("uninstall", changed, message)

    def restart(self) -> LifecycleResult:
        raise LifecycleUnsupportedError(
            "restart is unsupported while Core is running in-process; use the service manager or CLI"
        )


def _installed(manager: ServiceManager) -> bool | None:
    definition = getattr(manager.paths, "definition", None)
    if definition is None:
        value = getattr(manager, "installed", None)
        return value if isinstance(value, bool) else None
    path = Path(definition)
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            return False
        return path.resolve(strict=True) == path
    except OSError, ValueError:
        return False


def _probe_core(url: str) -> bool:
    return CoreClient(url).system_status().status == "healthy"
