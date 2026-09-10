"""Managed Core process records for detached and login-start modes."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

from tts_studio import __version__
from tts_studio.config import Settings
from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError

_RUN_RECORD_NAME = "core.json"
_CLAIM_NAME = "core.lock"
_LOG_NAME = "core.log"
_MAX_RECORD_BYTES = 16 * 1024


def _lock_exclusive(descriptor: int, *, blocking: bool) -> None:
    if fcntl is not None:
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        return
    if msvcrt is None:
        raise OSError("platform does not provide a file-locking primitive")
    os.lseek(descriptor, 0, os.SEEK_SET)
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    msvcrt.locking(descriptor, mode, 1)


def _unlock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if msvcrt is None:
        raise OSError("platform does not provide a file-locking primitive")
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class CoreRunRecord:
    pid: int
    host: str
    port: int
    data_dir: str
    started_at: str
    owner_token: str = ""

    @classmethod
    def from_mapping(cls, value: object) -> CoreRunRecord:
        if not isinstance(value, dict):
            raise TypeError("Core run record must be an object")
        pid = value.get("pid")
        port = value.get("port")
        host = value.get("host")
        data_dir = value.get("data_dir")
        started_at = value.get("started_at")
        owner_token = value.get("owner_token", "")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 < port <= 65535
            or not all(isinstance(item, str) and item for item in (host, data_dir, started_at))
            or not isinstance(owner_token, str)
        ):
            raise ValueError("Core run record has invalid fields")
        try:
            datetime.fromisoformat(cast(str, started_at))
        except ValueError as error:
            raise ValueError("Core run record has an invalid start time") from error
        return cls(
            pid=pid,
            host=cast(str, host),
            port=port,
            data_dir=cast(str, data_dir),
            started_at=cast(str, started_at),
            owner_token=owner_token,
        )


class CoreClaim:
    def __init__(self, descriptor: int, token: str) -> None:
        self.descriptor = descriptor
        self.token = token
        self._released = False

    def release(self) -> None:
        if not self._released:
            _unlock(self.descriptor)
            os.close(self.descriptor)
            self._released = True


class CoreRunStore:
    """Persist and reconcile one managed Core process record."""

    def __init__(self, layout: StorageLayout) -> None:
        self._layout = layout
        self.path = layout.run / _RUN_RECORD_NAME
        self.claim_path = layout.run / _CLAIM_NAME
        self.log_path = layout.logs / _LOG_NAME

    def claim(self) -> CoreClaim:
        self._layout.ensure()
        descriptor = os.open(
            self.claim_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _lock_exclusive(descriptor, blocking=False)
        except (BlockingIOError, OSError):
            os.close(descriptor)
            raise RuntimeError("Core is already starting or running") from None
        token = uuid4().hex
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, token.encode("ascii"))
            os.fsync(descriptor)
        except BaseException:
            _unlock(descriptor)
            os.close(descriptor)
            raise
        os.chmod(self.claim_path, stat.S_IRUSR | stat.S_IWUSR)
        return CoreClaim(descriptor, token)

    def adopt_claim(self, descriptor: int, token: str) -> CoreClaim:
        if not token or not isinstance(token, str):
            raise ValueError("Core claim token is invalid")
        return CoreClaim(descriptor, token)

    def write(self, record: CoreRunRecord) -> None:
        self._layout.ensure()
        temporary = self._layout.managed_child("run", f"core-tmp-{uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(record.__dict__, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.is_symlink():
                raise UnsafeStoragePathError("Core run record is redirected")
            os.replace(temporary, self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def read(self) -> CoreRunRecord | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        if self.path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise UnsafeStoragePathError("Core run record must be a regular file")
        if metadata.st_size > _MAX_RECORD_BYTES:
            raise ValueError("Core run record is too large")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Core run record is unreadable") from error
        return CoreRunRecord.from_mapping(document)

    def _open_lock(self, *, create: bool = False) -> int | None:
        try:
            descriptor = os.open(
                self.claim_path,
                os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileNotFoundError:
            return None
        try:
            _lock_exclusive(descriptor, blocking=False)
        except (BlockingIOError, OSError):
            os.close(descriptor)
            return None
        return descriptor

    def _claim_is_held(self, token: str) -> bool:
        descriptor = self._open_lock()
        if descriptor is None:
            try:
                return self.claim_path.read_text(encoding="ascii").strip() == token
            except (OSError, UnicodeError):
                return False
        os.close(descriptor)
        return False

    def reconcile(self) -> CoreRunRecord | None:
        observed = self.read()
        if observed is None:
            return None
        if not observed.owner_token and self.is_process_alive(observed.pid):
            return observed
        descriptor = self._open_lock(create=True)
        if descriptor is None:
            latest = self.read()
            return latest if latest is not None else None
        try:
            record = self.read()
            if record is None:
                return None
            if not record.owner_token and self.is_process_alive(record.pid):
                return record
            self.path.unlink(missing_ok=True)
            return None
        finally:
            _unlock(descriptor)
            os.close(descriptor)

    def acquire_owner(self, record: CoreRunRecord) -> CoreClaim | None:
        descriptor = self._open_lock()
        if descriptor is None:
            return None
        try:
            current = self.read()
            if current is None or current.pid != record.pid:
                raise ValueError
            if record.owner_token and current.owner_token != record.owner_token:
                raise ValueError
            return CoreClaim(descriptor, current.owner_token)
        except (OSError, ValueError, TypeError, UnicodeError):
            _unlock(descriptor)
            os.close(descriptor)
            return None

    def owns(self, record: CoreRunRecord) -> bool:
        return bool(record.owner_token) and self._claim_is_held(record.owner_token)

    def clear_if_owner(
        self, pid: int, owner_token: str | None = None, claim: CoreClaim | None = None
    ) -> None:
        descriptor = claim.descriptor if claim is not None else self._open_lock()
        if descriptor is None:
            return
        try:
            record = self.read()
            if record is not None and record.pid == pid and (
                owner_token is None and not record.owner_token
                or owner_token is not None and record.owner_token == owner_token
            ):
                self.path.unlink(missing_ok=True)
        finally:
            if claim is None:
                _unlock(descriptor)
                os.close(descriptor)

    @staticmethod
    def is_process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def tail_logs(self, lines: int = 100) -> str:
        if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 10_000:
            raise ValueError("lines must be between 1 and 10000")
        try:
            metadata = self.log_path.lstat()
        except FileNotFoundError:
            return ""
        if self.log_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise UnsafeStoragePathError("Core log must be a regular file")
        return "".join(self.log_path.read_text(encoding="utf-8").splitlines(keepends=True)[-lines:])


def core_health_url(host: str, port: int) -> str:
    """Return a connectable local health URL for a configured listener."""
    connect_host = host
    try:
        address = ip_address(host)
    except ValueError:
        pass
    else:
        if address.is_unspecified:
            connect_host = "::1" if address.version == 6 else "127.0.0.1"
        if address.version == 6:
            connect_host = f"[{connect_host}]"
    return f"http://{connect_host}:{port}"


@dataclass(frozen=True, slots=True)
class WorkerRuntimeSnapshot:
    engine_id: str
    status: str
    message: str
    capabilities: tuple[str, ...] | None = None
    engine_version: str | None = None
    max_concurrency: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    version: str
    host: str
    port: int
    data_dir: str
    generation_status: str
    active_generations: dict[str, str | None] | None
    workers: tuple[WorkerRuntimeSnapshot, ...]
    storage_accessible: bool
    database_accessible: bool
    startup_diagnostics: dict[str, dict[str, object]] = field(default_factory=dict)


async def snapshot_runtime(
    settings: Settings,
    supervisor: Any,
    runtime_manager: Any | None = None,
    *,
    capability_timeout: float = 1.0,
) -> RuntimeSnapshot:
    if capability_timeout <= 0:
        raise ValueError("capability_timeout must be positive")
    layout = StorageLayout.from_root(settings.data_dir)
    try:
        statuses = tuple(await supervisor.statuses())
    except Exception:  # noqa: BLE001 - diagnostics must not fail on worker RPC errors
        statuses = ()
        worker_failure = True
    else:
        worker_failure = False
    workers = tuple(
        await asyncio.gather(
            *(
                _worker_snapshot(
                    supervisor,
                    status,
                    worker_failure,
                    capability_timeout=capability_timeout,
                )
                for status in statuses
            )
        )
    )
    generation_status, active = _generation_snapshot(runtime_manager)
    diagnostics = getattr(supervisor, "startup_diagnostics", None)
    startup_diagnostics = diagnostics() if callable(diagnostics) else {}
    return RuntimeSnapshot(
        version=__version__,
        host=settings.host,
        port=settings.port,
        data_dir=str(layout.root),
        generation_status=generation_status,
        active_generations=active,
        workers=workers,
        storage_accessible=_accessible(layout.root),
        database_accessible=_database_accessible(layout),
        startup_diagnostics={str(key): dict(value) for key, value in startup_diagnostics.items()},
    )


async def _worker_snapshot(
    supervisor: Any,
    status: Any,
    worker_failure: bool,
    *,
    capability_timeout: float,
) -> WorkerRuntimeSnapshot:
    capabilities: tuple[str, ...] | None = None
    engine_version: str | None = None
    max_concurrency: int | None = None
    describe = getattr(supervisor, "describe", None)
    if callable(describe) and not worker_failure:
        try:
            description = await asyncio.wait_for(
                describe(status.engine_id),
                timeout=capability_timeout,
            )
            supported = getattr(description, "supported", None)
            if supported is None:
                supported = tuple(
                    item.name for item in getattr(description, "capabilities", ()) if item.supported
                )
            capabilities = tuple(sorted(str(item) for item in supported))
            version = getattr(description, "engine_version", None)
            engine_version = version if isinstance(version, str) else None
            concurrency = getattr(description, "max_concurrency", None)
            max_concurrency = concurrency if isinstance(concurrency, int) else None
        except Exception:  # noqa: BLE001 - capability probing is best effort
            capabilities = None
            engine_version = None
            max_concurrency = None
    return WorkerRuntimeSnapshot(
        engine_id=status.engine_id,
        status="unhealthy" if worker_failure else ("ready" if status.ready else "unhealthy"),
        message="worker status unavailable" if worker_failure else status.message,
        capabilities=capabilities,
        engine_version=engine_version,
        max_concurrency=max_concurrency,
    )


def _generation_snapshot(runtime_manager: Any | None) -> tuple[str, dict[str, str | None] | None]:
    if runtime_manager is None or not callable(getattr(runtime_manager, "status", None)):
        return "unknown", None
    try:
        status = runtime_manager.status()
    except OSError, ValueError, RuntimeError, UnsafeStoragePathError:
        return "unavailable", None
    active = getattr(status, "active_generations", None)
    if not isinstance(active, dict):
        return "unknown", None
    normalized = {
        str(engine): value if isinstance(value, str) or value is None else None
        for engine, value in active.items()
    }
    label = getattr(status, "state", getattr(status, "status", None))
    return (
        label if isinstance(label, str) else ("active" if any(normalized.values()) else "none")
    ), normalized


def _accessible(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        return False


def _database_accessible(layout: StorageLayout) -> bool:
    try:
        path = layout.database_path
        return _accessible(layout.checked_directory("database")) and (
            not path.exists()
            or (path.is_file() and not path.is_symlink() and os.access(path, os.R_OK | os.W_OK))
        )
    except OSError, UnsafeStoragePathError:
        return False
