"""Core-owned upload, transcript handoff, and safe cleanup for references."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.references.domain import (
    CleanupFailedError,
    InvalidReferenceTransitionError,
    ReferenceHandle,
    ReferenceInUseError,
    ReferenceMetadata,
    ReferenceRecording,
    ReferenceRecoveryRequiredError,
    ReferenceState,
    UploadTooLargeError,
)
from tts_studio.references.registry import ReferenceRecordNotFoundError, ReferenceRegistry
from tts_studio.storage.db import Database
from tts_studio.storage.layout import (
    StorageLayout,
    UnsafeStoragePathError,
    require_identity_bound_reference_storage,
)

MAX_REFERENCE_BYTES = 20 * 1024 * 1024
REFERENCE_TTL = timedelta(hours=1)


class ReferenceService:
    """Own reference bytes and keep transcript handoff outside durable storage."""

    def __init__(self, database: Database, layout: StorageLayout) -> None:
        self._database = database
        self._layout = layout
        self._registry = ReferenceRegistry(database)
        self._transcripts: dict[str, str | None] = {}
        self._saved_voice_identities: dict[str, tuple[int, int, int]] = {}

    def create_upload(
        self,
        *,
        model_id: str,
        payload: bytes | bytearray | BinaryIO | Iterable[bytes],
        transcript: str | None = None,
        now: str | None = None,
    ) -> ReferenceRecording:
        reference_id = str(uuid4())
        directory = self._layout.reference_staging
        digest = hashlib.sha256()
        byte_size = 0
        directory_fd = self._layout.open_reference_staging()
        try:
            descriptor = os.open(
                reference_id,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as output:
                for chunk in _chunks(payload):
                    if not isinstance(chunk, bytes):
                        raise TypeError("reference upload chunks must be bytes")
                    byte_size += len(chunk)
                    if byte_size > MAX_REFERENCE_BYTES:
                        raise UploadTooLargeError("reference upload exceeds 20 MiB")
                    digest.update(chunk)
                    output.write(chunk)
        except BaseException:
            _unlink_path(directory, reference_id)
            raise

        timestamp = now or _utc_now()
        expires_at = (datetime.fromisoformat(timestamp) + REFERENCE_TTL).isoformat()
        relative_path = f"staging/references/{reference_id}"
        try:
            recording = self._registry.create_uploaded(
                reference_id=reference_id,
                model_id=model_id,
                relative_path=relative_path,
                byte_size=byte_size,
                sha256=digest.hexdigest(),
                transcript_present=transcript is not None,
                expires_at=expires_at,
                now=timestamp,
            )
        except BaseException:
            _unlink_path(directory, reference_id)
            raise
        self._transcripts[reference_id] = transcript
        return recording

    def get(self, reference_id: str) -> ReferenceRecording:
        return self._registry.get(reference_id)

    def saved_voice_source(self, reference_id: str) -> tuple[ReferenceRecording, Path, str | None]:
        """Return a validated recording for one Core-owned saved Voice copy."""
        recording = self._registry.get(reference_id)
        if recording.state is not ReferenceState.VALIDATED:
            raise InvalidReferenceTransitionError(
                f"reference cannot become a saved Voice from {recording.state.value}"
            )
        relative = Path(*recording.relative_path.split("/"))
        path = self._layout.root / relative
        try:
            path.relative_to(self._layout.root)
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except (OSError, ValueError) as error:
            raise CleanupFailedError("reference path is missing or unsafe") from error
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or resolved != path:
            raise CleanupFailedError("reference path is not a regular file")
        self._saved_voice_identities[reference_id] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_ctime_ns,
        )
        return recording, path, self._transcripts.get(reference_id)

    def open_saved_voice_source(self, reference_id: str, path: Path) -> int:
        """Open a previously validated saved-Voice source without following replacement paths."""
        expected = self._saved_voice_identities.get(reference_id)
        if expected is None:
            raise CleanupFailedError("reference source was not validated")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns) != expected or not stat.S_ISREG(
            metadata.st_mode
        ):
            os.close(descriptor)
            raise CleanupFailedError("reference path identity changed")
        return descriptor

    def mark_validated(
        self,
        reference_id: str,
        metadata: ReferenceMetadata | engine_pb2.ReferenceMetadata,
        *,
        now: str | None = None,
    ) -> ReferenceRecording:
        normalized = _metadata(metadata)
        return self._registry.mark_validated(reference_id, normalized, now=now)

    def claim_for_generation(self, reference_id: str, *, now: str | None = None) -> ReferenceHandle:
        recording = self._registry.claim_for_generation(reference_id, now=now)
        transcript = self._transcripts.get(reference_id)
        if recording.transcript_present and transcript is None:
            handle = ReferenceHandle(recording=recording, transcript=None)
            try:
                self.release_terminal(handle)
            except CleanupFailedError:
                pass
            raise ReferenceRecoveryRequiredError(
                "reference transcript handoff was lost; recovery is required"
            )
        return ReferenceHandle(recording=recording, transcript=transcript)

    def release_terminal(self, handle: ReferenceHandle) -> None:
        self._cleanup(handle.recording)

    def release_for_recovery(self, reference_id: str) -> None:
        """Remove a job-owned reference when its in-memory handoff is gone."""
        self._cleanup(self._registry.get(reference_id))

    def delete(self, reference_id: str) -> None:
        recording = self._registry.get(reference_id)
        if recording.state is ReferenceState.CONSUMED:
            raise ReferenceInUseError(f"reference {reference_id!r} is owned by a Generation Job")
        self._cleanup(recording)

    def expire(self, *, now: str | None = None) -> tuple[ReferenceRecording, ...]:
        expired = self._registry.expire(now=now)
        for recording in expired:
            try:
                self._cleanup(recording)
            except CleanupFailedError:
                continue
        return expired

    def recover(self, *, now: str | None = None) -> int:
        recovered = 0
        self._registry.expire(now=now)
        for recording in self._registry.list_all():
            if recording.state in {
                ReferenceState.CONSUMED,
                ReferenceState.EXPIRED,
                ReferenceState.DELETED,
                ReferenceState.CLEANUP_FAILED,
            }:
                try:
                    self._cleanup(recording)
                    recovered += 1
                except CleanupFailedError:
                    continue
        try:
            directory = self._layout.reference_staging
            entries = tuple(directory.iterdir())
        except OSError, UnsafeStoragePathError:
            return recovered
        for entry in entries:
            if entry.name not in {recording.id for recording in self._registry.list_all()}:
                try:
                    _unlink_path(directory, entry.name)
                    recovered += 1
                except CleanupFailedError:
                    continue
        return recovered

    def _cleanup(self, recording: ReferenceRecording) -> None:
        try:
            directory = self._layout.reference_staging
            _unlink_path(directory, recording.id)
            self._registry.delete_metadata(recording.id)
        except ReferenceRecordNotFoundError:
            self._transcripts.pop(recording.id, None)
            self._saved_voice_identities.pop(recording.id, None)
        except CleanupFailedError, OSError, UnsafeStoragePathError:
            self._registry.mark_cleanup_failed(recording.id)
            raise CleanupFailedError(f"could not safely clean reference {recording.id!r}")
        else:
            self._transcripts.pop(recording.id, None)
            self._saved_voice_identities.pop(recording.id, None)


def _chunks(payload: bytes | bytearray | BinaryIO | Iterable[bytes]) -> Iterable[bytes]:
    if isinstance(payload, (bytes, bytearray)):
        yield bytes(payload)
        return
    if hasattr(payload, "read"):
        reader = payload
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                return
            yield chunk
        return
    yield from payload


def _metadata(value: ReferenceMetadata | engine_pb2.ReferenceMetadata) -> ReferenceMetadata:
    if isinstance(value, ReferenceMetadata):
        return value
    metadata = value  # protocol-generated metadata has the same stable fields
    return ReferenceMetadata(
        container=str(metadata.container),
        sample_rate_hz=int(metadata.sample_rate_hz),
        channels=int(metadata.channels),
        duration_ms=int(metadata.duration_ms),
    )


def _unlink_path(directory: Path, name: str) -> None:
    require_identity_bound_reference_storage()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise CleanupFailedError("reference filename is unsafe")
    checked = directory / name
    try:
        metadata = checked.lstat()
    except FileNotFoundError:
        return
    if checked.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise CleanupFailedError("reference path is not an unredirected regular file")
    if checked.resolve(strict=True) != checked:
        raise CleanupFailedError("reference path is redirected")
    directory_metadata = directory.lstat()
    if directory.is_symlink() or not stat.S_ISDIR(directory_metadata.st_mode):
        raise CleanupFailedError("reference staging directory is unsafe")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(directory, flags)
    try:
        opened_directory = os.fstat(parent_fd)
        if (
            opened_directory.st_ino != directory_metadata.st_ino
            or opened_directory.st_dev != directory_metadata.st_dev
        ):
            raise CleanupFailedError("reference staging directory identity changed")
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if observed.st_ino != metadata.st_ino or observed.st_dev != metadata.st_dev:
            raise CleanupFailedError("reference path identity changed")
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    finally:
        os.close(parent_fd)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
