"""Safe cleanup for abandoned VieNeu worker-owned download scratch."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def recover_download_scratch(data_root: Path) -> None:
    """Remove only unredirected entries in the worker's managed scratch root."""
    root = data_root.expanduser().absolute()
    if not _is_real_directory(root):
        return
    scratch = root / ".vieneu-downloads"
    if not _is_real_directory(scratch):
        return
    scratch_identity = _directory_identity(scratch)
    try:
        entries = tuple(scratch.iterdir())
    except OSError:
        return
    for entry in entries:
        if _directory_identity(scratch) != scratch_identity:
            return
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        identity = _entry_identity(metadata)
        if not _same_entry_identity(entry, identity):
            continue
        if stat.S_ISLNK(metadata.st_mode):
            entry.unlink()
            continue
        if stat.S_ISDIR(metadata.st_mode):
            if _is_below(entry, scratch) and _same_entry_identity(entry, identity):
                shutil.rmtree(entry)
        elif (
            stat.S_ISREG(metadata.st_mode)
            and _is_below(entry, scratch)
            and _same_entry_identity(entry, identity)
        ):
            entry.unlink()


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and path.resolve(strict=True) == path
    )


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("managed scratch entry is not a directory")
    return metadata.st_dev, metadata.st_ino


def _entry_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns


def _same_entry_identity(path: Path, identity: tuple[int, int, int]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return _entry_identity(metadata) == identity


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except OSError, ValueError:
        return False
    return True
