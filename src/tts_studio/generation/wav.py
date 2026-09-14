"""Core-owned WAV staging and atomic publication."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
import wave
from pathlib import Path

from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError


class WavArtifactWriter:
    """Stage PCM privately and atomically publish one managed WAV artifact."""

    def __init__(self, layout: StorageLayout, artifact_id: str) -> None:
        layout.ensure()
        self._layout = layout
        self._destination = layout.managed_child("audio", f"{artifact_id}.wav")
        descriptor, path = tempfile.mkstemp(
            prefix=f".{artifact_id}.", suffix=".pcm", dir=layout.checked_directory("staging")
        )
        self._temporary_path = Path(path)
        self._stream = os.fdopen(descriptor, "w+b", buffering=0)
        self._source_identity = _descriptor_identity(descriptor)
        self._byte_count = 0
        self._pcm_digest = hashlib.sha256()
        self._published = False
        self._final_temporary_path: Path | None = None
        self._published_path: Path | None = None
        self._published_identity: tuple[int, int] | None = None

    @property
    def temporary_path(self) -> Path:
        return self._temporary_path

    def write(self, pcm: bytes | bytearray | memoryview) -> None:
        if self._published or self._stream.closed:
            raise RuntimeError("WAV writer is closed")
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise TypeError("PCM payload must be bytes")
        payload = bytes(pcm)
        if len(payload) % 2:
            raise ValueError("S16LE PCM must be sample-width aligned")
        if self._stream.write(payload) != len(payload):
            raise OSError("incomplete PCM write")
        self._byte_count += len(payload)
        self._pcm_digest.update(payload)

    def finalize(self, *, byte_count: int | None = None, frame_count: int | None = None) -> Path:
        if self._published:
            return self._destination
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            if byte_count is not None and byte_count != self._byte_count:
                raise ValueError("validated PCM byte total does not match staged audio")
            if frame_count is not None and frame_count * 2 != self._byte_count:
                raise ValueError("validated PCM frame total does not match staged audio")
            _require_identity(self._temporary_path, self._source_identity)
            self._stream.seek(0)
            self._layout.checked_directory("audio")
            if self._destination.is_symlink():
                raise UnsafeStoragePathError(
                    "managed artifact destination must not be a symbolic link"
                )

            descriptor, final_path = tempfile.mkstemp(
                prefix=f".{self._destination.stem}.",
                suffix=".wav",
                dir=self._layout.checked_directory("audio"),
            )
            self._final_temporary_path = Path(final_path)
            with os.fdopen(descriptor, "w+b", buffering=0) as final_stream:
                identity = _descriptor_identity(final_stream.fileno())
                copied = hashlib.sha256()
                copied_bytes = 0
                with wave.open(final_stream, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(48_000)
                    while chunk := self._stream.read(1024 * 1024):
                        copied.update(chunk)
                        copied_bytes += len(chunk)
                        output.writeframesraw(chunk)
                    output.writeframes(b"")
                if copied_bytes != self._byte_count or copied.digest() != self._pcm_digest.digest():
                    raise ValueError("staged PCM differs from validated audio")
                final_stream.seek(0)
                expected_header = struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF",
                    36 + self._byte_count,
                    b"WAVE",
                    b"fmt ",
                    16,
                    1,
                    1,
                    48_000,
                    96_000,
                    2,
                    16,
                    b"data",
                    self._byte_count,
                )
                if final_stream.read(44) != expected_header:
                    raise ValueError("WAV header does not match validated PCM totals")
                wav_digest = hashlib.sha256()
                while chunk := final_stream.read(1024 * 1024):
                    wav_digest.update(chunk)
                if (
                    os.fstat(final_stream.fileno()).st_size != 44 + self._byte_count
                    or wav_digest.digest() != self._pcm_digest.digest()
                ):
                    raise ValueError("WAV payload does not match validated PCM")
                os.fsync(final_stream.fileno())
                _require_identity(self._final_temporary_path, identity)
            self._stream.close()
            self._published_identity = identity
            _require_identity(self._final_temporary_path, identity)
            os.replace(self._final_temporary_path, self._destination)
            self._published_path = self._destination
            _fsync_directory(self._layout.checked_directory("audio"))
            self._published = True
            self._final_temporary_path = None
            self._temporary_path.unlink(missing_ok=True)
            return self._destination
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if self._published:
            return
        if not self._stream.closed:
            self._stream.close()
        self._temporary_path.unlink(missing_ok=True)
        if self._final_temporary_path is not None:
            self._final_temporary_path.unlink(missing_ok=True)
            self._final_temporary_path = None
        if self._published_path is not None and self._published_identity is not None:
            _remove_published_file(self._destination, self._published_identity)
        self._published_identity = None
        self._published_path = None

    def discard_published(self) -> None:
        """Remove the exact file this writer published, if it is still present."""
        if not self._published:
            return
        if self._published_path is not None and self._published_identity is not None:
            _remove_published_file(self._published_path, self._published_identity)
        self._published = False
        self._published_identity = None
        self._published_path = None


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _require_identity(path: Path, identity: tuple[int, int]) -> None:
    if _file_identity(path) != identity:
        raise UnsafeStoragePathError("private WAV source identity changed")


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeStoragePathError("published WAV must be a regular file")
    return metadata.st_dev, metadata.st_ino


def _remove_published_file(path: Path, identity: tuple[int, int]) -> None:
    descriptor, quarantine_name = tempfile.mkstemp(
        prefix=".cleanup-", suffix=path.suffix, dir=path.parent
    )
    os.close(descriptor)
    quarantine = Path(quarantine_name)
    moved = False
    try:
        try:
            os.replace(path, quarantine)
            moved = True
        except FileNotFoundError:
            return
        try:
            moved_identity = _file_identity(quarantine)
        except OSError, UnsafeStoragePathError:
            _restore_quarantined_file(quarantine, path)
            raise UnsafeStoragePathError("published WAV changed before cleanup") from None
        if moved_identity == identity:
            quarantine.unlink(missing_ok=True)
            return
        _restore_quarantined_file(quarantine, path)
        raise UnsafeStoragePathError("published WAV identity changed before cleanup")
    finally:
        if not moved:
            quarantine.unlink(missing_ok=True)


def _restore_quarantined_file(quarantine: Path, destination: Path) -> None:
    os.link(quarantine, destination, follow_symlinks=False)
    quarantine.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
