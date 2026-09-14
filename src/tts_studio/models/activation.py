"""Filesystem verification and atomic activation below the managed data root."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.storage.layout import (
    ManagedDirectoryName,
    StorageLayout,
    UnsafeStoragePathError,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_UUID_DIRECTORY = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_RETIREMENT = re.compile(r"retired--([A-Za-z0-9][A-Za-z0-9_.-]{0,127})--[0-9a-f]{32}\Z")
_PROMOTION_DIRECTORY = re.compile(r"promotion--[0-9a-f]{32}\Z")
_ACTIVE_DOWNLOAD_STATES = {
    DownloadState.QUEUED,
    DownloadState.VALIDATING,
    DownloadState.DOWNLOADING,
    DownloadState.VERIFYING,
    DownloadState.ACTIVATING,
}


class ModelVerificationError(RuntimeError):
    """A staged model failed a Core-owned integrity or confinement check."""

    def __init__(self, code: str = "checksum_mismatch") -> None:
        super().__init__("downloaded model is not a verified managed staging tree")
        self.code = code


class InsufficientStorageError(RuntimeError):
    """Core storage capacity cannot safely admit a verified model."""

    def __init__(self) -> None:
        super().__init__("managed model storage capacity is insufficient")


@dataclass(frozen=True)
class VerifiedModel:
    manifest: dict[str, object]
    checksum_summary: dict[str, object]
    byte_size: int


@dataclass(frozen=True)
class ActivatedModelDirectory:
    path: Path
    cache_path: str


@dataclass(frozen=True)
class RetiredModelDirectory:
    original: Path
    retirement: Path


class ModelActivation:
    """Own model staging, verification, atomic renames, and crash cleanup."""

    def __init__(self, layout: StorageLayout, *, storage_limit_bytes: int | None = None) -> None:
        if storage_limit_bytes is not None and (
            not isinstance(storage_limit_bytes, int)
            or isinstance(storage_limit_bytes, bool)
            or storage_limit_bytes < 0
        ):
            raise ValueError("model storage limit must be a non-negative integer")
        self._layout = layout
        self._storage_limit_bytes = storage_limit_bytes

    def allocate_staging(self, job_id: str) -> Path:
        destination = self._layout.managed_child("staging", job_id)
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as error:
            raise UnsafeStoragePathError("staging destination already exists") from error
        return self._layout.checked_child("staging", job_id)

    def verify(
        self,
        staging: Path,
        manifest: engine_pb2.ModelManifest,
        *,
        repository_id: str,
        resolved_commit: str,
        variant: str,
        required_files: tuple[str, ...],
        expected_bytes: int | None = None,
    ) -> VerifiedModel:
        staging = self._checked_staging_path(staging)
        if (
            manifest.repository_id != repository_id
            or manifest.resolved_commit != resolved_commit
            or manifest.variant != variant
        ):
            raise ModelVerificationError("download_failed")

        reported: dict[str, engine_pb2.ManifestFile] = {}
        for item in manifest.files:
            relative = _safe_relative_file(item.relative_path)
            if relative in reported or _SHA256.fullmatch(item.sha256) is None:
                raise ModelVerificationError()
            reported[relative] = item
        required = tuple(_safe_relative_file(path) for path in required_files)
        if not reported or len(required) != len(set(required)) or set(required) != set(reported):
            raise ModelVerificationError()

        actual_files = self._walk_regular_files(staging)
        if set(actual_files) != set(reported):
            raise ModelVerificationError()

        verified_bytes = 0
        serialized_files: list[dict[str, object]] = []
        for relative in sorted(reported):
            item = reported[relative]
            artifact = actual_files[relative]
            size = artifact.stat().st_size
            checksum = _hash_file(artifact)
            if size != item.byte_size or checksum != item.sha256:
                raise ModelVerificationError()
            verified_bytes += size
            serialized_files.append({"path": relative, "byte_size": size, "sha256": checksum})

        if manifest.byte_size != verified_bytes:
            raise ModelVerificationError()
        if expected_bytes is not None and expected_bytes != verified_bytes:
            raise ModelVerificationError()
        return VerifiedModel(
            manifest={
                "repository_id": repository_id,
                "resolved_commit": resolved_commit,
                "variant": variant,
                "files": serialized_files,
                "byte_size": verified_bytes,
            },
            checksum_summary={"algorithm": "sha256", "verified_files": len(reported)},
            byte_size=verified_bytes,
        )

    def activate(
        self,
        staging: Path,
        model_id: str,
        verified: VerifiedModel,
    ) -> ActivatedModelDirectory:
        """Materialize a verified staging snapshot and publish it atomically."""
        staging = self._checked_staging_path(staging)
        self._require_safe_promotion_primitives()
        self._layout.managed_child("models", model_id)
        promotion_name = f"promotion--{uuid4().hex}"
        promotion_created = False
        models_fd: int | None = None
        source_parent_fd: int | None = None
        source_fd: int | None = None
        promotion_fd: int | None = None
        try:
            models_fd = self._open_managed_directory_fd("models")
            if _entry_exists(models_fd, model_id):
                raise UnsafeStoragePathError("model activation destination already exists")
            os.mkdir(promotion_name, mode=0o700, dir_fd=models_fd)
            promotion_created = True
            promotion_fd = _open_directory_fd(promotion_name, dir_fd=models_fd)
            source_parent_fd = self._open_managed_directory_fd("staging")
            source_fd = _open_directory_fd(staging.name, dir_fd=source_parent_fd)
            expected = _verified_manifest(verified)
            copied = self._copy_verified_snapshot(source_fd, promotion_fd, expected)
            if set(copied) != set(expected):
                raise ModelVerificationError()
            os.rename(
                promotion_name,
                model_id,
                src_dir_fd=models_fd,
                dst_dir_fd=models_fd,
            )
            promotion_created = False
        except BaseException:
            if promotion_created:
                self._remove_direct_child("models", promotion_name)
            raise
        finally:
            for descriptor in (promotion_fd, source_fd, source_parent_fd, models_fd):
                if descriptor is not None:
                    os.close(descriptor)
        try:
            checked = self._layout.checked_child("models", model_id)
        except BaseException:
            self._remove_direct_child("models", model_id)
            raise
        return ActivatedModelDirectory(path=checked, cache_path=f"models/{model_id}")

    def ensure_storage_available(
        self,
        required_bytes: int,
        *,
        installed_bytes: int,
        replacing_bytes: int = 0,
    ) -> None:
        """Fail closed when filesystem capacity or the managed-cache limit is insufficient."""
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (required_bytes, installed_bytes, replacing_bytes)
        ):
            raise ValueError("storage byte counts must be non-negative integers")
        try:
            free_bytes = shutil.disk_usage(self._layout.checked_directory("models")).free
        except OSError as error:
            raise InsufficientStorageError from error
        projected_bytes = installed_bytes - replacing_bytes + required_bytes
        if free_bytes < required_bytes or (
            self._storage_limit_bytes is not None and projected_bytes > self._storage_limit_bytes
        ):
            raise InsufficientStorageError

    def rollback_activation(self, activated: ActivatedModelDirectory) -> None:
        self._remove_direct_child("models", activated.path.name)

    def retire(self, cache_path: str) -> RetiredModelDirectory:
        original = self.model_path(cache_path)
        retirement_name = f"retired--{original.name}--{uuid4().hex}"
        retirement = self._layout.managed_child("staging", retirement_name)
        original.rename(retirement)
        return RetiredModelDirectory(original=original, retirement=retirement)

    def restore_retired(self, retired: RetiredModelDirectory) -> None:
        if retired.original.exists() or retired.original.is_symlink():
            raise UnsafeStoragePathError("cannot restore retired model over an existing path")
        retired.retirement.rename(retired.original)

    def discard_retired(self, retired: RetiredModelDirectory) -> None:
        self._remove_direct_child("staging", retired.retirement.name)

    def discard_model(self, cache_path: str) -> None:
        path = self.model_path(cache_path)
        self._remove_direct_child("models", path.name)

    def cleanup_staging(self, job_id: str) -> None:
        self._remove_direct_child("staging", job_id)

    def model_path(self, cache_path: str) -> Path:
        path = PurePosixPath(cache_path)
        if len(path.parts) != 2 or path.parts[0] != "models":
            raise UnsafeStoragePathError("model cache path is outside the managed model root")
        return self._layout.checked_child("models", path.parts[1])

    def recover(self, registry: ModelRegistry) -> None:
        """Fail abandoned work and reconcile rename artifacts; safe to repeat."""
        self._cleanup_vieneu_download_scratch()
        jobs = registry.list_download_jobs()
        for job in jobs:
            if job.state in _ACTIVE_DOWNLOAD_STATES:
                self.cleanup_staging(job.id)
                registry.mark_recovery_failure(
                    job.id,
                    {
                        "code": "recovery_required",
                        "message": "An interrupted model download was cleaned up.",
                        "retryable": True,
                    },
                )

        models = {model.id: model for model in registry.list_models()}
        staging = self._layout.checked_directory("staging")
        for entry in tuple(staging.iterdir()):
            match = _RETIREMENT.fullmatch(entry.name)
            if match is not None and match.group(1) in models:
                model = models[match.group(1)]
                expected = self._layout.managed_child("models", model.id)
                identity = self._recovery_directory_identity(entry)
                cache_path = PurePosixPath(model.cache_path)
                if (
                    identity is not None
                    and cache_path.parts == ("models", model.id)
                    and not expected.exists()
                    and not expected.is_symlink()
                    and self._recovery_directory_identity(entry) == identity
                ):
                    entry.rename(expected)
                    try:
                        promoted = self._layout.checked_child("models", model.id)
                        if _directory_identity(promoted.lstat()) != identity:
                            raise UnsafeStoragePathError(
                                "recovered model directory identity changed"
                            )
                    except BaseException:
                        self._remove_direct_child("models", model.id)
                        raise
                    continue
            self._remove_direct_child("staging", entry.name)

        referenced = {
            path.parts[1]
            for model in models.values()
            if len((path := PurePosixPath(model.cache_path)).parts) == 2
            and path.parts[0] == "models"
        }
        models_root = self._layout.checked_directory("models")
        for entry in tuple(models_root.iterdir()):
            if (
                _UUID_DIRECTORY.fullmatch(entry.name) or _PROMOTION_DIRECTORY.fullmatch(entry.name)
            ) and entry.name not in referenced:
                self._remove_direct_child("models", entry.name)

    def _cleanup_vieneu_download_scratch(self) -> None:
        """Remove abandoned adapter download workspaces below the data root."""
        root = self._layout.root
        try:
            root_identity = _directory_identity(root.lstat())
        except OSError:
            return
        for entry in tuple(root.iterdir()):
            if not _same_entry_identity(root, root_identity):
                return
            if entry.name != ".vieneu-downloads" and not entry.name.startswith(
                ".vieneu-downloads-"
            ):
                continue
            try:
                metadata = entry.lstat()
            except OSError:
                continue
            identity = _directory_identity(metadata)
            if not _same_entry_identity(entry, identity):
                continue
            if not _same_entry_identity(root, root_identity):
                return
            try:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    if _same_entry_identity(entry, identity):
                        entry.unlink()
                elif _same_entry_identity(entry, identity):
                    shutil.rmtree(entry)
            except FileNotFoundError:
                continue

    def _require_safe_promotion_primitives(self) -> None:
        if (
            os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.mkdir not in os.supports_dir_fd
            or os.rename not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.scandir not in os.supports_fd
        ):
            raise ModelVerificationError("download_failed")

    def _open_managed_directory_fd(self, name: ManagedDirectoryName) -> int:
        root_fd = _open_directory_fd(self._layout.root)
        try:
            return _open_directory_fd(name, dir_fd=root_fd)
        finally:
            os.close(root_fd)

    def _copy_verified_snapshot(
        self,
        source_fd: int,
        promotion_fd: int,
        expected: dict[str, tuple[int, str]] | None,
    ) -> dict[str, tuple[int, str]]:
        copied: dict[str, tuple[int, str]] = {}
        self._copy_directory(source_fd, promotion_fd, "", expected, copied)
        if expected is not None:
            expected_bytes = sum(size for size, _ in expected.values())
            if expected_bytes != sum(size for size, _ in copied.values()):
                raise ModelVerificationError()
        return copied

    def _copy_directory(
        self,
        source_fd: int,
        promotion_fd: int,
        prefix: str,
        expected: dict[str, tuple[int, str]] | None,
        copied: dict[str, tuple[int, str]],
    ) -> None:
        try:
            with os.scandir(source_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise ModelVerificationError() from error
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
                if _is_unsafe_entry(metadata):
                    raise ModelVerificationError()
                relative = _safe_relative_file(f"{prefix}/{entry.name}" if prefix else entry.name)
                if stat.S_ISDIR(metadata.st_mode):
                    os.mkdir(entry.name, mode=0o700, dir_fd=promotion_fd)
                    child_source_fd = _open_directory_fd(entry.name, dir_fd=source_fd)
                    child_promotion_fd = _open_directory_fd(entry.name, dir_fd=promotion_fd)
                    try:
                        self._copy_directory(
                            child_source_fd,
                            child_promotion_fd,
                            relative,
                            expected,
                            copied,
                        )
                    finally:
                        os.close(child_promotion_fd)
                        os.close(child_source_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    if expected is not None and relative not in expected:
                        raise ModelVerificationError()
                    copied[relative] = self._copy_file(
                        source_fd,
                        promotion_fd,
                        entry.name,
                        relative,
                        expected[relative] if expected is not None else None,
                    )
                else:
                    raise ModelVerificationError()
            except ModelVerificationError:
                raise
            except (OSError, ValueError) as error:
                raise ModelVerificationError() from error

    def _copy_file(
        self,
        source_parent_fd: int,
        promotion_parent_fd: int,
        name: str,
        relative: str,
        expected: tuple[int, str] | None,
    ) -> tuple[int, str]:
        source_fd: int | None = None
        destination_fd: int | None = None
        try:
            source_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=source_parent_fd,
            )
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ModelVerificationError()
            destination_fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=promotion_parent_fd,
            )
            digest = hashlib.sha256()
            copied_bytes = 0
            for chunk in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
                digest.update(chunk)
                copied_bytes += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise ModelVerificationError()
                    view = view[written:]
            after = os.fstat(source_fd)
            if (before.st_dev, before.st_ino, before.st_mode) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
            ) or before.st_size != after.st_size:
                raise ModelVerificationError()
            checksum = digest.hexdigest()
            destination_checksum = _hash_descriptor(destination_fd)
            if destination_checksum != checksum:
                raise ModelVerificationError()
            result = (copied_bytes, checksum)
            if expected is not None and result != expected:
                raise ModelVerificationError()
            return result
        except ModelVerificationError:
            raise
        except (OSError, ValueError) as error:
            raise ModelVerificationError() from error
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)

    def _checked_staging_path(self, staging: Path) -> Path:
        if staging.parent != self._layout.checked_directory("staging"):
            raise ModelVerificationError()
        try:
            return self._layout.checked_child("staging", staging.name)
        except UnsafeStoragePathError as error:
            raise ModelVerificationError() from error

    def _recovery_directory_identity(self, path: Path) -> tuple[int, int, int] | None:
        """Return identity only for a real, unredirected managed staging directory."""
        try:
            staging = self._layout.checked_directory("staging")
            if path.parent != staging:
                return None
            metadata = path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or bool(attributes & reparse)
                or not stat.S_ISDIR(metadata.st_mode)
                or path.resolve(strict=True) != path
            ):
                return None
            return _directory_identity(metadata)
        except OSError, ValueError:
            return None

    def _walk_regular_files(self, root: Path) -> dict[str, Path]:
        files: dict[str, Path] = {}

        def raise_walk_error(error: OSError) -> None:
            raise ModelVerificationError() from error

        for current, directories, names in os.walk(
            root, followlinks=False, onerror=raise_walk_error
        ):
            current_path = Path(current)
            for name in [*directories, *names]:
                candidate = current_path / name
                metadata = candidate.lstat()
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if candidate.is_symlink() or bool(attributes & reparse):
                    raise ModelVerificationError()
            for name in names:
                candidate = current_path / name
                if not stat.S_ISREG(candidate.lstat().st_mode):
                    raise ModelVerificationError()
                relative = candidate.relative_to(root).as_posix()
                files[_safe_relative_file(relative)] = candidate
        return files

    def _remove_direct_child(self, parent_name: ManagedDirectoryName, identifier: str) -> None:
        parent = self._layout.checked_directory(parent_name)
        path = self._layout.managed_child(parent_name, identifier)
        if not path.exists() and not path.is_symlink():
            return
        if path.parent != parent:
            raise UnsafeStoragePathError("refusing to remove a path outside managed storage")
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode):
            path.unlink()
            return
        if bool(attributes & reparse):
            if stat.S_ISDIR(metadata.st_mode):
                path.rmdir()
            else:
                path.unlink()
            return
        if not stat.S_ISDIR(metadata.st_mode):
            path.unlink()
            return
        shutil.rmtree(path)


def _safe_relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ModelVerificationError()
    return path.as_posix()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_directory_fd(path: Path | str, *, dir_fd: int | None = None) -> int:
    try:
        return os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)
    except (OSError, TypeError, ValueError) as error:
        raise ModelVerificationError() from error


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise UnsafeStoragePathError("model activation destination is unsafe") from error
    return True


def _verified_manifest(
    verified: VerifiedModel,
) -> dict[str, tuple[int, str]]:
    raw_files = verified.manifest.get("files")
    if not isinstance(raw_files, list):
        raise ModelVerificationError()
    expected: dict[str, tuple[int, str]] = {}
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise ModelVerificationError()
        relative = raw_file.get("path")
        byte_size = raw_file.get("byte_size")
        checksum = raw_file.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
            or not isinstance(checksum, str)
            or _SHA256.fullmatch(checksum) is None
        ):
            raise ModelVerificationError()
        relative = _safe_relative_file(relative)
        if relative in expected:
            raise ModelVerificationError()
        expected[relative] = (byte_size, checksum)
    if not expected or sum(size for size, _ in expected.values()) != verified.byte_size:
        raise ModelVerificationError()
    return expected


def _is_unsafe_entry(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_ctime_ns)


def _same_entry_identity(path: Path, identity: tuple[int, int, int]) -> bool:
    try:
        return _directory_identity(path.lstat()) == identity
    except OSError:
        return False
