from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from tts_studio.references.service import ReferenceService
from tts_studio.storage.identity import IdentityBoundUnlinkError, unlink_open_file
from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError
from tts_studio.voices.domain import SavedVoice
from tts_studio.voices.registry import SavedVoiceRegistry


class SavedVoiceDeletionError(RuntimeError):
    """A Saved Voice could not be deleted without weakening storage safety."""


@dataclass
class _ValidatedSavedVoiceDeletion:
    layout: StorageLayout
    root_fd: int
    root_identity: tuple[int, int]
    voices_fd: int
    voices_identity: tuple[int, int]
    directory_fd: int
    directory_identity: tuple[int, int]
    file_fd: int
    file_identity: tuple[int, int]
    voice_id: str
    name: str
    payload: bytes

    def close(self) -> None:
        try:
            os.close(self.file_fd)
        finally:
            try:
                os.close(self.directory_fd)
            finally:
                try:
                    os.close(self.voices_fd)
                finally:
                    os.close(self.root_fd)


class SavedVoiceService:
    def __init__(
        self, registry: SavedVoiceRegistry, references: ReferenceService, layout: StorageLayout
    ) -> None:
        self._registry = registry
        self._references = references
        self._layout = layout

    def list_for_model(self, model_id: str) -> tuple[SavedVoice, ...]:
        return self._registry.list_for_model(model_id)

    def get(self, voice_id: str) -> SavedVoice:
        return self._registry.get(voice_id)

    def create_from_reference(self, *, model_id: str, label: str, reference_id: str) -> SavedVoice:
        if not label.strip() or len(label) > 120:
            raise ValueError("label must be between 1 and 120 characters")
        recording, source, transcript = self._references.saved_voice_source(reference_id)
        if recording.model_id != model_id:
            raise ValueError("the reference is pinned to a different model")
        voice_id = str(uuid4())
        directory = self._layout.managed_child("voices", voice_id)
        relative_path = f"voices/{voice_id}/reference{source.suffix.lower() or '.bin'}"
        filename = Path(relative_path).name
        created = False
        voices_fd: int | None = None
        directory_fd: int | None = None
        destination_fd: int | None = None
        try:
            voices_fd, directory_fd = _create_voice_directory(self._layout, voice_id)
            source_fd: int | None = None
            try:
                source_fd = self._references.open_saved_voice_source(reference_id, source)
                destination_fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                assert source_fd is not None
                with (
                    os.fdopen(source_fd, "rb") as source_file,
                    os.fdopen(os.dup(destination_fd), "wb") as output,
                ):
                    shutil.copyfileobj(source_file, output)
                if not _directory_matches(directory, directory_fd):
                    raise UnsafeStoragePathError("Saved Voice directory identity changed")
            except BaseException:
                if source_fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(source_fd)
                raise
            voice = self._registry.create(
                voice_id=voice_id,
                model_id=model_id,
                label=label.strip(),
                relative_path=relative_path,
                transcript=transcript,
            )
            created = True
            if not _published_file_matches(
                voices_fd, voice_id, directory_fd, filename, destination_fd
            ):
                raise UnsafeStoragePathError("Saved Voice publication identity changed")
            self._references.delete(reference_id)
            return voice
        except BaseException:
            if created:
                try:
                    self._registry.delete(voice_id)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Saved Voice cleanup failed; the row and copied file were retained"
                    ) from rollback_error
            if voices_fd is not None and directory_fd is not None and destination_fd is not None:
                with contextlib.suppress(OSError, UnsafeStoragePathError):
                    _remove_created_voice(
                        voices_fd, voice_id, directory_fd, filename, destination_fd
                    )
            else:
                _safe_remove(directory)
            raise
        finally:
            for descriptor in (destination_fd, directory_fd, voices_fd):
                if descriptor is not None:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)

    def delete(self, voice_id: str) -> None:
        voice = self._registry.get(voice_id)
        candidate = _open_saved_voice_for_deletion(self._layout, voice)
        try:
            _require_current_saved_voice_directory(candidate)
            try:
                unlink_open_file(candidate.directory_fd, candidate.name, candidate.file_fd)
            except IdentityBoundUnlinkError as error:
                raise SavedVoiceDeletionError(
                    "the validated Saved Voice could not be removed from managed storage"
                ) from error
            try:
                self._registry.delete(voice_id)
            except Exception as database_error:
                try:
                    _restore_saved_voice_file(candidate)
                except Exception as restore_error:  # noqa: BLE001 - report both rollback failures
                    raise SavedVoiceDeletionError(
                        "Saved Voice metadata was retained but its managed file could not be restored"
                    ) from ExceptionGroup(
                        "Saved Voice deletion and rollback both failed",
                        [database_error, restore_error],
                    )
                raise
            try:
                _require_current_saved_voice_directory(candidate)
                unlink_open_file(
                    candidate.voices_fd,
                    candidate.voice_id,
                    candidate.directory_fd,
                    remove_directory=True,
                )
            except IdentityBoundUnlinkError as error:
                raise SavedVoiceDeletionError(
                    "the empty Saved Voice directory could not be removed safely"
                ) from error
        finally:
            candidate.close()


def _open_checked_directory(path: Path) -> int:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeStoragePathError("managed directory is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise UnsafeStoragePathError("managed directory identity changed")
    return descriptor


def _directory_matches(path: Path, descriptor: int) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == (
        opened.st_dev,
        opened.st_ino,
    )


def _create_voice_directory(layout: StorageLayout, voice_id: str) -> tuple[int, int]:
    voices = layout.checked_directory("voices")
    voices_fd = _open_checked_directory(voices)
    try:
        os.mkdir(voice_id, mode=0o700, dir_fd=voices_fd)
        expected = os.stat(voice_id, dir_fd=voices_fd, follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(voice_id, flags, dir_fd=voices_fd)
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            os.close(directory_fd)
            raise UnsafeStoragePathError("Saved Voice directory identity changed")
        return voices_fd, directory_fd
    except BaseException:
        os.close(voices_fd)
        raise


def _published_file_matches(
    voices_fd: int, voice_id: str, directory_fd: int, filename: str, destination_fd: int
) -> bool:
    try:
        directory = os.stat(voice_id, dir_fd=voices_fd, follow_symlinks=False)
        destination = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    opened_directory = os.fstat(directory_fd)
    opened_destination = os.fstat(destination_fd)
    return (directory.st_dev, directory.st_ino) == (
        opened_directory.st_dev,
        opened_directory.st_ino,
    ) and (destination.st_dev, destination.st_ino) == (
        opened_destination.st_dev,
        opened_destination.st_ino,
    )


def _remove_created_voice(
    voices_fd: int, voice_id: str, directory_fd: int, filename: str, destination_fd: int
) -> None:
    if not stat.S_ISREG(os.fstat(destination_fd).st_mode):
        raise UnsafeStoragePathError("Saved Voice file is not regular")
    try:
        unlink_open_file(directory_fd, filename, destination_fd)
        unlink_open_file(voices_fd, voice_id, directory_fd, remove_directory=True)
    except IdentityBoundUnlinkError as error:
        raise UnsafeStoragePathError("Saved Voice cleanup identity changed") from error


def _open_saved_voice_for_deletion(
    layout: StorageLayout, voice: SavedVoice
) -> _ValidatedSavedVoiceDeletion:
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")):
        raise SavedVoiceDeletionError("safe Saved Voice deletion is unavailable")
    relative = PurePosixPath(voice.relative_path)
    if (
        relative.is_absolute()
        or len(relative.parts) != 3
        or relative.parts[0] != "voices"
        or relative.parts[1] != voice.id
        or relative.parts[2] in {"", ".", ".."}
    ):
        raise UnsafeStoragePathError("saved Voice path is unsafe")
    voice_id = relative.parts[1]
    name = relative.parts[2]
    root_fd: int | None = None
    voices_fd: int | None = None
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        layout.checked_directory("voices")
        root_fd = os.open(layout.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        voices_fd = os.open("voices", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        directory_fd = os.open(
            voice_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=voices_fd
        )
        if tuple(os.listdir(directory_fd)) != (name,):
            raise UnsafeStoragePathError("saved Voice directory has unexpected contents")
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        path_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        file_metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or not stat.S_ISREG(file_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (file_metadata.st_dev, file_metadata.st_ino)
        ):
            raise UnsafeStoragePathError("saved Voice path identity changed")
        payload = _read_descriptor(file_fd)
        final_metadata = os.fstat(file_fd)
        if (final_metadata.st_dev, final_metadata.st_ino) != (
            file_metadata.st_dev,
            file_metadata.st_ino,
        ) or final_metadata.st_size != len(payload):
            raise UnsafeStoragePathError("saved Voice path identity changed")
        root_metadata = os.fstat(root_fd)
        voices_metadata = os.fstat(voices_fd)
        directory_metadata = os.fstat(directory_fd)
        candidate = _ValidatedSavedVoiceDeletion(
            layout=layout,
            root_fd=root_fd,
            root_identity=(root_metadata.st_dev, root_metadata.st_ino),
            voices_fd=voices_fd,
            voices_identity=(voices_metadata.st_dev, voices_metadata.st_ino),
            directory_fd=directory_fd,
            directory_identity=(directory_metadata.st_dev, directory_metadata.st_ino),
            file_fd=file_fd,
            file_identity=(file_metadata.st_dev, file_metadata.st_ino),
            voice_id=voice_id,
            name=name,
            payload=payload,
        )
        root_fd = None
        voices_fd = None
        directory_fd = None
        file_fd = None
        return candidate
    except FileNotFoundError as error:
        raise UnsafeStoragePathError("saved Voice path is missing") from error
    finally:
        for descriptor in (file_fd, directory_fd, voices_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _require_current_saved_voice_directory(candidate: _ValidatedSavedVoiceDeletion) -> None:
    try:
        root = os.stat(candidate.layout.root, follow_symlinks=False)
        voices = os.stat(candidate.layout.voices, follow_symlinks=False)
        directory = os.stat(candidate.voice_id, dir_fd=candidate.voices_fd, follow_symlinks=False)
    except OSError as error:
        raise SavedVoiceDeletionError(
            "the managed Saved Voice directory changed during deletion"
        ) from error
    if (
        not stat.S_ISDIR(root.st_mode)
        or (root.st_dev, root.st_ino) != candidate.root_identity
        or not stat.S_ISDIR(voices.st_mode)
        or (voices.st_dev, voices.st_ino) != candidate.voices_identity
        or not stat.S_ISDIR(directory.st_mode)
        or (directory.st_dev, directory.st_ino) != candidate.directory_identity
    ):
        raise SavedVoiceDeletionError("the managed Saved Voice directory changed during deletion")


def _restore_saved_voice_file(candidate: _ValidatedSavedVoiceDeletion) -> None:
    _require_current_saved_voice_directory(candidate)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            candidate.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=candidate.directory_fd,
        )
        view = memoryview(candidate.payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("Saved Voice restoration made no write progress")
            offset += written
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if metadata.st_size != len(candidate.payload):
            raise OSError("restored Saved Voice size does not match")
        restored = _read_descriptor(descriptor)
        if not hashlib.sha256(restored).digest() == hashlib.sha256(candidate.payload).digest():
            raise OSError("restored Saved Voice checksum does not match")
        published = os.stat(candidate.name, dir_fd=candidate.directory_fd, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise UnsafeStoragePathError("restored Saved Voice identity changed")
        _require_current_saved_voice_directory(candidate)
    except BaseException:
        if descriptor is not None:
            with contextlib.suppress(IdentityBoundUnlinkError):
                unlink_open_file(candidate.directory_fd, candidate.name, descriptor)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_remove(directory: Path) -> None:
    try:
        if directory.is_dir() and not directory.is_symlink():
            for child in directory.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
            directory.rmdir()
    except OSError:
        pass
