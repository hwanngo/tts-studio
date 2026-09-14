from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISDIR
from typing import Literal

ManagedDirectoryName = Literal[
    "database",
    "models",
    "audio",
    "voices",
    "workers",
    "logs",
    "run",
    "staging",
]

_UNSAFE_PATH_ERRNOS = frozenset(
    errno_value
    for errno_value in (
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        getattr(errno, "EMLINK", None),
    )
    if errno_value is not None
)

_MANAGED_DIRECTORY_NAMES: tuple[ManagedDirectoryName, ...] = (
    "database",
    "models",
    "audio",
    "voices",
    "workers",
    "logs",
    "run",
    "staging",
)
_MANAGED_CHILD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class UnsafeStoragePathError(RuntimeError):
    """Raised when a managed path is redirected or has the wrong type."""


class ReferenceStorageUnavailableError(UnsafeStoragePathError):
    """Raised when identity-bound reference storage is unavailable."""


def require_identity_bound_reference_storage() -> None:
    if (
        os.name == "nt"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise ReferenceStorageUnavailableError(
            "identity-bound reference staging is unavailable on this platform"
        )


@dataclass(frozen=True)
class StorageLayout:
    """The fixed set of directories managed below a runtime data root."""

    root: Path
    database: Path = field(init=False)
    models: Path = field(init=False)
    audio: Path = field(init=False)
    voices: Path = field(init=False)
    workers: Path = field(init=False)
    logs: Path = field(init=False)
    run: Path = field(init=False)
    staging: Path = field(init=False)

    def __post_init__(self) -> None:
        resolved_root = self.root.expanduser().resolve()
        object.__setattr__(self, "root", resolved_root)
        for name in _MANAGED_DIRECTORY_NAMES:
            object.__setattr__(self, name, resolved_root / name)

    @classmethod
    def from_root(cls, root: Path) -> StorageLayout:
        return cls(root=root)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise UnsafeStoragePathError(f"managed data root is unsafe: {self.root}")
        _tighten_directory(self.root)
        for name in _MANAGED_DIRECTORY_NAMES:
            directory = getattr(self, name)
            if directory.is_symlink():
                raise UnsafeStoragePathError(
                    f"managed directory {name!r} is unsafe: symbolic links are not allowed"
                )
            try:
                directory.mkdir(exist_ok=True)
            except FileExistsError as error:
                raise UnsafeStoragePathError(
                    f"managed directory {name!r} is unsafe: expected a directory"
                ) from error
            _tighten_directory(directory)
            self.checked_directory(name)

    def open_reference_staging(self) -> int:
        """Open reference staging and bind the descriptor to its checked identity."""
        require_identity_bound_reference_storage()
        references = self.reference_staging
        metadata = references.stat()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(references, flags)
        except OSError as error:
            raise UnsafeStoragePathError(
                "reference staging directory cannot be opened safely"
            ) from error
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            os.close(descriptor)
            raise UnsafeStoragePathError("reference staging directory identity changed")
        return descriptor

    def checked_directory(self, name: ManagedDirectoryName) -> Path:
        """Return one managed directory after revalidating its filesystem identity."""
        directory: Path = getattr(self, name)
        try:
            metadata = directory.lstat()
            resolved = directory.resolve(strict=True)
            resolved.relative_to(self.root)
        except OSError as error:
            if error.errno not in _UNSAFE_PATH_ERRNOS:
                raise
            raise UnsafeStoragePathError(
                f"managed directory {name!r} is unsafe: it must remain below {self.root}"
            ) from error
        except ValueError as error:
            raise UnsafeStoragePathError(
                f"managed directory {name!r} is unsafe: it must remain below {self.root}"
            ) from error

        if directory.is_symlink() or not S_ISDIR(metadata.st_mode) or resolved != directory:
            raise UnsafeStoragePathError(
                f"managed directory {name!r} is unsafe: expected an unredirected directory"
            )
        return directory

    def managed_child(self, name: ManagedDirectoryName, identifier: str) -> Path:
        """Build a direct managed child path from an opaque, path-free identifier."""
        if not isinstance(identifier, str) or _MANAGED_CHILD.fullmatch(identifier) is None:
            raise UnsafeStoragePathError("managed child identifier is invalid")
        return self.checked_directory(name) / identifier

    def checked_child(self, name: ManagedDirectoryName, identifier: str) -> Path:
        """Return an existing unredirected direct child of a checked managed directory."""
        child = self.managed_child(name, identifier)
        try:
            metadata = child.lstat()
            resolved = child.resolve(strict=True)
        except OSError as error:
            if error.errno not in _UNSAFE_PATH_ERRNOS:
                raise
            raise UnsafeStoragePathError("managed child is missing or unsafe") from error
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            child.is_symlink()
            or bool(attributes & reparse_flag)
            or not S_ISDIR(metadata.st_mode)
            or resolved != child
        ):
            raise UnsafeStoragePathError("managed child must be an unredirected directory")
        return child

    @property
    def database_path(self) -> Path:
        """Return the fixed Core SQLite path below the checked database directory."""
        return self.checked_directory("database") / "tts-studio.sqlite3"

    @property
    def reference_staging(self) -> Path:
        """Return the Core-owned, unredirected staging directory for references."""
        staging = self.checked_directory("staging")
        references = staging / "references"
        if references.is_symlink():
            raise UnsafeStoragePathError("reference staging directory cannot be a symbolic link")
        try:
            references.mkdir(exist_ok=True)
        except FileExistsError as error:
            raise UnsafeStoragePathError("reference staging path must be a directory") from error
        try:
            metadata = references.lstat()
            resolved = references.resolve(strict=True)
            resolved.relative_to(staging)
        except OSError as error:
            if error.errno not in _UNSAFE_PATH_ERRNOS:
                raise
            raise UnsafeStoragePathError(
                "reference staging directory is redirected or missing"
            ) from error
        except ValueError as error:
            raise UnsafeStoragePathError(
                "reference staging directory is redirected or missing"
            ) from error
        if not S_ISDIR(metadata.st_mode) or resolved != references:
            raise UnsafeStoragePathError(
                "reference staging directory is redirected or not a directory"
            )
        _tighten_directory(references)
        return references


def _tighten_directory(directory: Path) -> None:
    if os.name != "nt":
        directory.chmod(0o700)
