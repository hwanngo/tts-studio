"""Authentication helpers for private worker RPCs."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import grpc

_MAX_TOKEN_FILE_BYTES = 1024
_WINDOWS = os.name == "nt"


def consume_worker_token(token_file: Path) -> str:
    """Read and immediately remove one private per-launch credential file."""
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        descriptor = os.open(token_file, flags)
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        # Windows st_mode bits do not describe the file's DACL: writable regular
        # files are commonly reported as 0o666 even when access is user-scoped.
        private_permissions = _WINDOWS or mode & 0o077 == 0
        owned_by_current_user = not hasattr(os, "getuid") or metadata.st_uid == os.getuid()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not private_permissions
            or not owned_by_current_user
            or not 0 < metadata.st_size <= _MAX_TOKEN_FILE_BYTES
        ):
            raise ValueError("worker token must be a private regular file")

        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = None
            token = stream.read(_MAX_TOKEN_FILE_BYTES + 1)
        if not token or len(token.encode("utf-8")) > _MAX_TOKEN_FILE_BYTES:
            raise ValueError("worker token must be a private regular file")
        return token
    except (OSError, UnicodeError) as error:
        raise ValueError("worker token must be a private regular file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        token_file.unlink(missing_ok=True)


async def require_worker_token[RequestT, ResponseT](
    context: grpc.aio.ServicerContext[RequestT, ResponseT], expected: str
) -> None:
    """Abort an RPC unless it presents the per-launch worker token."""
    for key, value in context.invocation_metadata() or ():
        if (
            key == "x-tts-worker-token"
            and isinstance(value, str)
            and secrets.compare_digest(value, expected)
        ):
            return

    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "worker authentication failed")
