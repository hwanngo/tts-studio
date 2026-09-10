"""User-owned service definition installation without platform side effects."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_SAFE_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class ServicePlatform(StrEnum):
    DARWIN = "darwin"
    LINUX = "linux"
    WINDOWS = "windows"


def validate_service_name(name: str) -> str:
    """Validate a name accepted by launchd, systemd, and Task Scheduler."""

    if not _SAFE_SERVICE_NAME.fullmatch(name):
        raise ValueError("service name contains unsafe characters")
    return name


@dataclass(frozen=True, slots=True)
class ServicePaths:
    """The definition target for a named per-user service."""

    platform: ServicePlatform
    name: str
    home: Path
    definition: Path | None

    @classmethod
    def resolve(cls, platform: ServicePlatform, name: str, home: Path) -> ServicePaths:
        if not home.is_absolute():
            raise ValueError("home must be an absolute path")
        validate_service_name(name)
        if platform is ServicePlatform.DARWIN:
            definition = home / "Library" / "LaunchAgents" / f"{name}.plist"
        elif platform is ServicePlatform.LINUX:
            definition = home / ".config" / "systemd" / "user" / f"{name}.service"
        else:
            definition = None
        return cls(platform=platform, name=name, home=home, definition=definition)


def write_definition(paths: ServicePaths, content: str) -> None:
    """Atomically write a definition, refusing symlinks and non-files."""

    if paths.definition is None:
        return
    target = paths.definition
    _ensure_safe_parent(target.parent, paths.home)
    _ensure_contained(target, paths.home)
    if target.is_symlink():
        raise ValueError("service definition target cannot be a symlink")
    if target.exists() and not target.is_file():
        raise ValueError("service definition target must be a regular file")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=".tts-studio-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def uninstall_definition(paths: ServicePaths) -> bool:
    """Remove a regular definition if present; return whether it was removed."""

    if paths.definition is None:
        return False
    target = paths.definition
    _ensure_contained(target, paths.home)
    if not _validate_safe_parent(target.parent, paths.home):
        return False
    if target.is_symlink():
        raise ValueError("service definition target cannot be a symlink")
    if not target.exists():
        return False
    if not target.is_file():
        raise ValueError("service definition target must be a regular file")
    target.unlink()
    return True


def _ensure_safe_parent(parent: Path, home: Path) -> None:
    """Create missing parents without following links or accepting files."""

    _ensure_contained(parent, home)
    _validate_safe_parent(parent, home, create=True)


def _validate_safe_parent(parent: Path, home: Path, *, create: bool = False) -> bool:
    """Validate every parent component, optionally creating missing ones."""

    current = home
    if current.is_symlink():
        raise ValueError("service definition parent cannot be a symlink")
    if not current.exists():
        if not create:
            return False
        current.mkdir()
    elif not current.is_dir():
        raise ValueError("service definition parent must be a directory")
    for component in parent.relative_to(home).parts:
        current /= component
        if current.is_symlink():
            raise ValueError("service definition parent cannot be a symlink")
        if current.exists():
            if not current.is_dir():
                raise ValueError("service definition parent must be a directory")
        else:
            if not create:
                return False
            current.mkdir()
    return True


def _ensure_contained(path: Path, home: Path) -> None:
    resolved_home = home.resolve()
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_home)
    except ValueError as exc:
        raise ValueError("service definition path escapes the user home") from exc
