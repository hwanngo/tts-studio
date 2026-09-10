"""Identity-bound managed filesystem operations."""

from __future__ import annotations

import ctypes
import errno
import os

_AT_REMOVEDIR = 0x80
_IDENTITY_MISMATCH_ERRNOS = frozenset({errno.EDEADLK, errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


class IdentityBoundUnlinkError(RuntimeError):
    """A checked directory entry could not be removed safely."""


class IdentityBoundUnlinkUnavailableError(IdentityBoundUnlinkError):
    """The host does not expose an identity-bound unlink primitive."""


class IdentityBoundUnlinkMismatchError(IdentityBoundUnlinkError):
    """The directory entry no longer names the retained descriptor."""


def unlink_open_file(
    directory_fd: int,
    name: str,
    file_fd: int,
    *,
    remove_directory: bool = False,
) -> None:
    """Remove ``name`` only when it still identifies ``file_fd`` within ``directory_fd``."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        funlinkat = libc.funlinkat
    except (AttributeError, OSError) as error:
        raise IdentityBoundUnlinkUnavailableError(
            "identity-bound deletion is unavailable on this platform"
        ) from error
    funlinkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    funlinkat.restype = ctypes.c_int
    flags = _AT_REMOVEDIR if remove_directory else 0
    if funlinkat(directory_fd, os.fsencode(name), file_fd, flags) == 0:
        return
    error_number = ctypes.get_errno()
    cause = OSError(error_number, os.strerror(error_number))
    if error_number in _IDENTITY_MISMATCH_ERRNOS:
        raise IdentityBoundUnlinkMismatchError(
            "the managed entry changed before deletion completed"
        ) from cause
    raise IdentityBoundUnlinkError(
        "the validated managed entry could not be removed"
    ) from cause
