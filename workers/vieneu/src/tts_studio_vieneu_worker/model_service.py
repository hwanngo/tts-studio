"""VieNeu model compatibility and self-contained model acquisition."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import httpx
from huggingface_hub.errors import (
    EntryNotFoundError,
    HfHubHTTPError,
    LocalEntryNotFoundError,
    OfflineModeIsEnabled,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from tts_studio_protocol.engine.v1 import engine_pb2

from .constants import (
    ENGINE_ID,
    ENGINE_VERSION,
    MOSS_CODEC_COMMIT,
    MOSS_CODEC_REPOSITORY,
    VARIANT_DIRECTORIES,
    VIENEU_REPOSITORY,
)
from .huggingface import HuggingFaceRepositoryClient, RepositoryClient

TARGET_REPOSITORY = VIENEU_REPOSITORY
CODEC_REPOSITORY = MOSS_CODEC_REPOSITORY
CODEC_REVISION = MOSS_CODEC_COMMIT

_VARIANTS = {
    "int8": ("INT8", VARIANT_DIRECTORIES["int8"]),
    "fp32": ("FP32", VARIANT_DIRECTORIES["fp32"]),
}
_GRAPH_FILES = (
    "config.json",
    "tokenizer.json",
    "vieneu_acoustic_cached.onnx",
    "vieneu_decode_step.onnx",
    "vieneu_prefill.onnx",
    "vieneu_backbone_shared.data",
    "vieneu_v3_heads.npz",
)
_CLONING_FILES = ("denoiser.onnx", "speaker_encoder.onnx")
_CODEC_FILES = (
    "codec_browser_onnx_meta.json",
    "moss_audio_tokenizer_decode_full.onnx",
    "moss_audio_tokenizer_decode_shared.data",
    "moss_audio_tokenizer_decode_step.onnx",
    "moss_audio_tokenizer_encode.data",
    "moss_audio_tokenizer_encode.onnx",
)
_REQUIRED_MANIFEST_FILES = tuple(
    [f"backbone/{variant}/{name}" for variant in _VARIANTS for name in _GRAPH_FILES]
    + [f"cloning/{name}" for name in _CLONING_FILES]
    + [f"codec/{name}" for name in _CODEC_FILES]
)
_SNAPSHOT_CWD_LOCK = threading.Lock()


class DownloadContext(Protocol):
    def cancelled(self) -> bool: ...


@dataclass
class _StagingDestination:
    path: Path
    fd: int

    def close(self) -> None:
        os.close(self.fd)


class _Cancelled(Exception):
    pass


class VieNeuModelService:
    def __init__(self, repository: RepositoryClient | None, data_root: Path) -> None:
        self._repository = repository or HuggingFaceRepositoryClient()
        self._data_root = _canonical_data_root(data_root)

    def validate(
        self, request: engine_pb2.ValidateModelRequest
    ) -> engine_pb2.ValidateModelResponse:
        if request.repository_id != TARGET_REPOSITORY:
            return self._error_response(
                "model_incompatible", "The repository is not compatible with VieNeu"
            )
        requested = request.requested_revision if request.HasField("requested_revision") else None
        try:
            info = self._repository.model_info(TARGET_REPOSITORY, requested)
            revision = _commit(info)
            if requested is not None and _is_immutable_commit(requested) and revision != requested:
                return self._error_response(
                    "revision_not_found", "The requested model revision is unavailable"
                )
            model_entries = tuple(self._repository.list_files(TARGET_REPOSITORY, revision))
            files = _file_names(model_entries)
            missing = self._missing_model_files(files)
            codec_info = self._repository.model_info(CODEC_REPOSITORY, CODEC_REVISION)
            codec_revision = _commit(codec_info)
            if codec_revision != CODEC_REVISION:
                raise ValueError("codec revision is not pinned")
            codec_entries = tuple(self._repository.list_files(CODEC_REPOSITORY, codec_revision))
            codec_files = _file_names(codec_entries)
            missing.extend(f"codec/{name}" for name in _CODEC_FILES if name not in codec_files)
            estimated_bytes = _estimated_bytes(
                model_entries,
                [f"{prefix}/{name}" for _, prefix in _VARIANTS.values() for name in _GRAPH_FILES]
                + list(_CLONING_FILES),
                (),
            ) + _estimated_bytes(codec_entries, _CODEC_FILES, ())
        except Exception as error:  # noqa: BLE001 - repository SDK errors are intentionally redacted
            code, message, retryable = _repository_error(error)
            return self._error_response(code, message, retryable=retryable)
        if missing:
            response = self._error_response(
                "model_incompatible", "Required VieNeu artifacts are missing"
            )
            response.error.details["missing_file"] = missing[0]
            return response
        response = engine_pb2.ValidateModelResponse(
            repository_id=TARGET_REPOSITORY,
            resolved_commit=revision,
            compatible=True,
            engine_id=ENGINE_ID,
            engine_version=ENGINE_VERSION,
            required_files=list(_REQUIRED_MANIFEST_FILES),
            available_variants=[
                engine_pb2.ModelVariant(id=variant, label=label)
                for variant, (label, _) in _VARIANTS.items()
            ],
            estimated_bytes=estimated_bytes,
            evidence=[
                engine_pb2.CompatibilityEvidence(
                    code="codec_provenance",
                    message=f"{CODEC_REPOSITORY}@{codec_revision}",
                ),
            ],
        )
        if requested is not None:
            response.requested_revision = requested
        return response

    async def download(
        self, request: engine_pb2.DownloadModelRequest, context: DownloadContext
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        sequence = 0
        destination: _StagingDestination | None = None
        destination_identity: tuple[int, int] | None = None
        destination_preexisting = False
        staging_root: Path | None = None
        staging_root_identity: tuple[int, int] | None = None
        work: Path | None = None
        work_identity: tuple[int, int] | None = None
        work_root: Path | None = None
        scratch_root: _StagingDestination | None = None
        work_fd: int | None = None
        work_cleanup_pending = False
        owned_files: list[tuple[Path, tuple[int, int]]] = []
        owned_dirs: list[tuple[Path, tuple[int, int]]] = []
        succeeded = False

        def progress(
            phase: engine_pb2.DownloadPhase.ValueType,
            downloaded: int,
            total: int | None,
            message: str,
        ) -> engine_pb2.DownloadModelEvent:
            nonlocal sequence
            sequence += 1
            return engine_pb2.DownloadModelEvent(
                progress=engine_pb2.DownloadProgress(
                    sequence=sequence,
                    phase=phase,
                    bytes_downloaded=downloaded,
                    message=message,
                    total_bytes=total,
                )
            )

        try:
            if request.repository_id != TARGET_REPOSITORY or request.variant not in _VARIANTS:
                raise _DownloadFailure(
                    "invalid_request", "The requested VieNeu model selection is invalid"
                )
            (
                destination,
                destination_preexisting,
                destination_identity,
                staging_root,
                staging_root_identity,
            ) = self._staging_path(request.staging_destination)
            _check_cancelled(context)
            self._assert_staging_root(staging_root, staging_root_identity)
            info = await self._repository_call(
                self._repository.model_info, TARGET_REPOSITORY, request.resolved_commit
            )
            revision = _commit(info)
            if revision != request.resolved_commit:
                raise _DownloadFailure(
                    "revision_not_found", "The requested model revision is unavailable"
                )
            model_names = _file_names(
                await self._repository_call(
                    self._repository.list_files, TARGET_REPOSITORY, revision
                )
            )
            codec_names = _file_names(
                await self._repository_call(
                    self._repository.list_files, CODEC_REPOSITORY, CODEC_REVISION
                )
            )
            codec_revision = _commit(
                await self._repository_call(
                    self._repository.model_info, CODEC_REPOSITORY, CODEC_REVISION
                )
            )
            if codec_revision != CODEC_REVISION:
                raise _DownloadFailure(
                    "model_incompatible", "The pinned codec revision is unavailable"
                )
            model_patterns = [
                f"{variant_dir}/{name}"
                for _, variant_dir in _VARIANTS.values()
                for name in _GRAPH_FILES
            ] + list(_CLONING_FILES)
            missing = [name for name in model_patterns if name not in model_names]
            missing.extend(name for name in _CODEC_FILES if name not in codec_names)
            if missing:
                raise _DownloadFailure(
                    "model_incompatible", "Required VieNeu artifacts are missing", missing[0]
                )
            yield progress(
                engine_pb2.DOWNLOAD_PHASE_DOWNLOADING, 0, None, "Downloading VieNeu artifacts"
            )
            self._assert_staging_root(staging_root, staging_root_identity)
            scratch_root = self._scratch_root()
            work_root = scratch_root.path
            work_name = _make_directory_at(
                scratch_root.fd,
                f".repository-download-{destination.path.name}-",
            )
            work = work_root / work_name
            work_fd = _open_directory_at(scratch_root.fd, work_name)
            work_identity = _directory_identity_from_fd(work_fd)
            self._assert_staging_root(staging_root, staging_root_identity)
            _assert_workspace(work, work_identity, work_root)
            try:
                await _run_blocking(
                    _snapshot_download_in_directory,
                    self._repository.snapshot_download,
                    work_fd,
                    TARGET_REPOSITORY,
                    revision,
                    ".",
                    model_patterns,
                    on_detached=lambda: _remove_workspace_no_follow(work, work_identity, work_root),
                )
            except asyncio.CancelledError:
                work_cleanup_pending = True
                raise
            except Exception as error:
                raise _repository_download_failure(error) from error
            _check_cancelled(context)
            _assert_workspace(work, work_identity, work_root)
            try:
                await _run_blocking(
                    _snapshot_download_in_directory,
                    self._repository.snapshot_download,
                    work_fd,
                    CODEC_REPOSITORY,
                    CODEC_REVISION,
                    ".",
                    list(_CODEC_FILES),
                    on_detached=lambda: _remove_workspace_no_follow(work, work_identity, work_root),
                )
            except asyncio.CancelledError:
                work_cleanup_pending = True
                raise
            except Exception as error:
                raise _repository_download_failure(error) from error
            _check_cancelled(context)
            _assert_workspace(work, work_identity, work_root)
            copied = 0
            expected: list[tuple[Path, Path]] = []
            for variant, (_, variant_dir) in _VARIANTS.items():
                for name in _GRAPH_FILES:
                    expected.append(
                        (
                            work / variant_dir / name,
                            destination.path / "backbone" / variant / name,
                        )
                    )
            for name in _CLONING_FILES:
                expected.append((work / name, destination.path / "cloning" / name))
            for name in _CODEC_FILES:
                expected.append((work / name, destination.path / "codec" / name))
            for source, target in expected:
                _check_cancelled(context)
                self._assert_staging_root(staging_root, staging_root_identity)
                created_files, created_dirs = _copy_regular_file(source, target, work, destination)
                owned_files.extend(created_files)
                owned_dirs.extend(created_dirs)
                self._assert_staging_root(staging_root, staging_root_identity)
                copied += target.stat().st_size
            _remove_tree_no_follow(work, expected_identity=work_identity)
            yield progress(
                engine_pb2.DOWNLOAD_PHASE_VERIFYING, copied, copied, "Verifying VieNeu artifacts"
            )
            self._assert_staging_root(staging_root, staging_root_identity)
            files = [_manifest_file(destination.path, target) for _, target in expected]
            yield progress(
                engine_pb2.DOWNLOAD_PHASE_FINALIZING, copied, copied, "Finalizing VieNeu manifest"
            )
            yield engine_pb2.DownloadModelEvent(
                manifest=engine_pb2.ModelManifest(
                    repository_id=TARGET_REPOSITORY,
                    resolved_commit=revision,
                    variant=request.variant,
                    files=files,
                    byte_size=copied,
                )
            )
            succeeded = True
        except _Cancelled:
            yield engine_pb2.DownloadModelEvent(
                error=_error("download_cancelled", "The model download was cancelled")
            )
        except _DownloadFailure as error:
            details = {"missing_file": error.missing} if error.missing else {}
            yield engine_pb2.DownloadModelEvent(
                error=_error(error.code, error.message, details, retryable=error.retryable)
            )
        except OSError:
            yield engine_pb2.DownloadModelEvent(
                error=_error(
                    "download_failed", "VieNeu artifacts could not be staged", retryable=True
                )
            )
        except ValueError:
            yield engine_pb2.DownloadModelEvent(
                error=_error("download_failed", "VieNeu artifacts could not be staged")
            )
        except Exception:  # noqa: BLE001 - repository SDK errors are intentionally redacted
            yield engine_pb2.DownloadModelEvent(
                error=_error(
                    "download_failed", "VieNeu artifacts could not be downloaded", retryable=True
                )
            )
        finally:
            if destination is not None:
                _remove_partial_staging(
                    destination.path,
                    destination_identity,
                    succeeded,
                    destination_preexisting,
                    None if work_cleanup_pending else work,
                    owned_files,
                    owned_dirs,
                    staging_root,
                    staging_root_identity,
                    work_identity,
                    work_root,
                )
            if destination is not None:
                destination.close()
            if work_fd is not None:
                os.close(work_fd)
            if scratch_root is not None:
                scratch_root.close()

    def _missing_model_files(self, files: set[str]) -> list[str]:
        missing: list[str] = []
        for _, prefix in _VARIANTS.values():
            missing.extend(
                f"{prefix}/{name}" for name in _GRAPH_FILES if f"{prefix}/{name}" not in files
            )
        missing.extend(name for name in _CLONING_FILES if name not in files)
        return missing

    async def _repository_call[T](self, function: Callable[..., T], *args: object) -> T:
        try:
            return await _run_blocking(function, *args)
        except Exception as error:
            raise _repository_download_failure(error) from error

    def _staging_path(
        self, value: str
    ) -> tuple[_StagingDestination, bool, tuple[int, int], Path, tuple[int, int]]:
        relative = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 1
        ):
            raise _DownloadFailure("invalid_request", "The staging destination is invalid")
        root = self._data_root / "staging"
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise _DownloadFailure("invalid_request", "The staging destination is invalid")
        root_fd = _open_directory_fd(root)
        destination_fd: int | None = None
        try:
            root_identity = _directory_identity_from_fd(root_fd)
            destination = root / relative.name
            preexisting = _entry_is_present(root_fd, relative.name)
            if preexisting:
                if not _entry_is_directory(root_fd, relative.name):
                    raise _DownloadFailure("invalid_request", "The staging destination is invalid")
            else:
                os.mkdir(relative.name, dir_fd=root_fd)
            destination_fd = os.open(relative.name, _directory_open_flags(), dir_fd=root_fd)
            destination_identity = _directory_identity_from_fd(destination_fd)
        except BaseException:
            if destination_fd is not None:
                os.close(destination_fd)
            raise
        finally:
            os.close(root_fd)
        assert destination_fd is not None
        return (
            _StagingDestination(destination, destination_fd),
            preexisting,
            destination_identity,
            root,
            root_identity,
        )

    def _scratch_root(self) -> _StagingDestination:
        root = self._data_root / ".vieneu-downloads"
        root.mkdir(parents=True, exist_ok=True)
        root_fd = _open_directory_fd(root)
        try:
            _directory_identity_from_fd(root_fd)
        except BaseException:
            os.close(root_fd)
            raise
        return _StagingDestination(root, root_fd)

    @staticmethod
    def _assert_staging_root(root: Path | None, identity: tuple[int, int] | None) -> None:
        if root is None or identity is None or not _same_directory_identity(root, identity):
            raise ValueError("staging root was replaced")
        if not _has_no_symlink_components(root.anchor and Path(root.anchor) or root, root):
            raise ValueError("staging root was redirected")

    @staticmethod
    def _error_response(
        code: str, message: str, *, retryable: bool = False
    ) -> engine_pb2.ValidateModelResponse:
        return engine_pb2.ValidateModelResponse(error=_error(code, message, retryable=retryable))


class _DownloadFailure(Exception):
    def __init__(
        self, code: str, message: str, missing: str | None = None, retryable: bool = False
    ) -> None:
        self.code, self.message, self.missing, self.retryable = code, message, missing, retryable


def _commit(info: object) -> str:
    value = getattr(info, "sha", None)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise ValueError("repository did not return an immutable revision")
    return value


def _is_immutable_commit(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40,64}", value) is not None


def _repository_error(error: BaseException) -> tuple[str, str, bool]:
    if isinstance(error, RevisionNotFoundError):
        return "revision_not_found", "The requested model revision is unavailable", False
    if isinstance(error, RepositoryNotFoundError):
        return "model_incompatible", "The VieNeu repository could not be validated", False
    # LocalEntryNotFoundError inherits EntryNotFoundError and OSError.  It is
    # an offline/cache miss, not proof that the repository is incompatible.
    if isinstance(
        error,
        (
            LocalEntryNotFoundError,
            OfflineModeIsEnabled,
            HfHubHTTPError,
            httpx.TransportError,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return "download_failed", "The repository could not be reached", True
    if isinstance(error, EntryNotFoundError):
        return "model_incompatible", "The VieNeu repository could not be validated", False
    if isinstance(error, OSError):
        return "download_failed", "The repository could not be reached", True
    return "model_incompatible", "The VieNeu repository could not be validated", False


def _repository_download_failure(error: BaseException) -> _DownloadFailure:
    code, message, retryable = _repository_error(error)
    return _DownloadFailure(code, message, retryable=retryable)


def _file_names(entries: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            name = entry
        else:
            name = str(getattr(entry, "rfilename", getattr(entry, "path", "")))
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
            or path.as_posix() != name
            or getattr(entry, "type", "file") not in ("file", None)
        ):
            raise ValueError("repository returned an unsafe relative path")
        if name in names:
            raise ValueError("repository returned duplicate paths")
        names.add(name)
    return names


def _estimated_bytes(
    entries: Iterable[object], required_names: Iterable[str], root_names: Iterable[str]
) -> int:
    names = set(required_names) | set(root_names)
    return sum(
        int(getattr(entry, "size", 0) or 0) for entry in entries if _entry_name(entry) in names
    )


def _entry_name(entry: object) -> str:
    if isinstance(entry, str):
        return entry
    return str(getattr(entry, "rfilename", getattr(entry, "path", "")))


def _check_cancelled(context: DownloadContext) -> None:
    if context.cancelled():
        raise _Cancelled


def _assert_workspace(work: Path, identity: tuple[int, int], root: Path) -> None:
    if not _same_directory_identity(work, identity) or not _has_no_symlink_components(root, work):
        raise ValueError("download workspace was replaced")


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(name, _directory_open_flags(), dir_fd=parent_fd)


def _make_directory_at(parent_fd: int, prefix: str) -> str:
    for _ in range(100):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name
    raise FileExistsError("could not allocate a private download workspace")


def _snapshot_download_in_directory(
    function: Callable[[str, str, Path, list[str]], None],
    work_fd: int,
    repo_id: str,
    revision: str,
    destination_name: str,
    allow_patterns: list[str],
) -> None:
    with _SNAPSHOT_CWD_LOCK:
        previous_fd = _open_directory_fd(Path("."))
        try:
            os.fchdir(work_fd)
            function(repo_id, revision, Path(destination_name), allow_patterns)
        finally:
            os.fchdir(previous_fd)
            os.close(previous_fd)


def _remove_workspace_no_follow(work: Path, identity: tuple[int, int], root: Path) -> None:
    if (
        _same_directory_identity(work, identity)
        and _has_no_symlink_components(root, work)
        and work.is_dir()
        and not work.is_symlink()
    ):
        _remove_tree_no_follow(work)


async def _run_blocking[T](
    function: Callable[..., T],
    *args: object,
    on_detached: Callable[[], None] | None = None,
) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A repository SDK call cannot be forcefully interrupted from Python.
        # Do not hold the RPC open while its bounded worker thread unwinds; the
        # caller owns cleanup of any files produced after the call returns.
        if on_detached is None:
            task.add_done_callback(_consume_blocking_result)
        else:
            task.add_done_callback(
                lambda completed: _consume_detached_result(completed, on_detached)
            )
        raise


def _consume_blocking_result[T](task: asyncio.Task[T]) -> None:
    try:
        task.result()
    except BaseException:  # noqa: BLE001, S110 - detached SDK thread result is intentionally discarded
        pass


def _consume_detached_result[T](task: asyncio.Task[T], cleanup: Callable[[], None]) -> None:
    _consume_blocking_result(task)
    try:
        cleanup()
    except BaseException:  # noqa: BLE001, S110 - detached cleanup cannot affect the RPC
        pass


def _copy_regular_file(
    source: Path, target: Path, source_root: Path, target_root: _StagingDestination
) -> tuple[list[tuple[Path, tuple[int, int]]], list[tuple[Path, tuple[int, int]]]]:
    _assert_no_symlink_components(source_root, source)
    _assert_no_symlink_components(target_root.path, target)
    stat_result = source.lstat()
    if not source.is_file() or source.is_symlink() or not stat_result:
        raise ValueError("source is not a regular file")
    relative = target.relative_to(target_root.path)
    missing_dirs: list[tuple[Path, tuple[int, int]]] = []
    parent_fd = os.dup(target_root.fd)
    try:
        relative_parent = Path()
        for component in relative.parent.parts:
            relative_parent /= component
            directory = target_root.path / relative_parent
            try:
                os.mkdir(component, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                created = False
            next_fd = os.open(component, _directory_open_flags(), dir_fd=parent_fd)
            if created:
                missing_dirs.append((directory, _directory_identity_from_fd(next_fd)))
            os.close(parent_fd)
            parent_fd = next_fd

        if _entry_is_present(parent_fd, relative.name):
            raise ValueError("target already exists")
        source_fd = os.open(os.fspath(source), os.O_RDONLY | _no_follow_flag())
        target_fd = -1
        target_created = False
        target_identity: tuple[int, int] | None = None
        try:
            source_metadata = os.fstat(source_fd)
            if not stat.S_ISREG(source_metadata.st_mode):
                raise ValueError("source is not a regular file")
            target_fd = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            target_created = True
            target_metadata = os.fstat(target_fd)
            if not stat.S_ISREG(target_metadata.st_mode):
                raise ValueError("target is not a regular file")
            target_identity = (target_metadata.st_dev, target_metadata.st_ino)
            try:
                with (
                    os.fdopen(source_fd, "rb") as source_stream,
                    os.fdopen(target_fd, "wb") as target_stream,
                ):
                    source_fd = -1
                    target_fd = -1
                    shutil.copyfileobj(source_stream, target_stream)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
            finally:
                if target_fd != -1:
                    os.close(target_fd)
        finally:
            if source_fd != -1:
                os.close(source_fd)
    except Exception:
        if target_fd != -1:
            os.close(target_fd)
            target_fd = -1
        if target_created and target_identity is not None:
            try:
                current = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == target_identity:
                    os.unlink(relative.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        for directory, identity in reversed(missing_dirs):
            if _same_directory_identity(directory, identity) and not directory.is_symlink():
                directory.rmdir()
        raise
    finally:
        os.close(parent_fd)
    assert target_identity is not None
    return (
        [(target, target_identity)],
        missing_dirs,
    )


def _no_follow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:
        raise OSError("safe no-follow file opens are unavailable")
    return flag


def _directory_open_flags() -> int:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag or any(
        function not in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    ):
        raise OSError("safe descriptor-relative directory opens are unavailable")
    return os.O_RDONLY | directory_flag | _no_follow_flag()


def _open_directory_fd(path: Path) -> int:
    return os.open(os.fspath(path), _directory_open_flags())


def _directory_identity_from_fd(file_descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(file_descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("owned staging directory is not a directory")
    return metadata.st_dev, metadata.st_ino


def _entry_is_present(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _entry_is_directory(parent_fd: int, name: str) -> bool:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return stat.S_ISDIR(metadata.st_mode)


def _assert_no_symlink_components(root: Path, path: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("path escapes staging root") from error
    current = Path(root.anchor)
    for component in root.parts[1:] + path.relative_to(root).parts:
        current /= component
        if current.is_symlink():
            raise ValueError("staging path contains a symbolic link")


def _manifest_file(root: Path, path: Path) -> engine_pb2.ManifestFile:
    relative = path.relative_to(root).as_posix()
    if "\\" in relative:
        raise ValueError("manifest path contains a backslash")
    data = path.read_bytes()
    return engine_pb2.ManifestFile(
        relative_path=relative, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest()
    )


def _error(
    code: str,
    message: str,
    details: dict[str, str] | None = None,
    retryable: bool = False,
) -> engine_pb2.WorkerError:
    return engine_pb2.WorkerError(
        code=code, message=message, retryable=retryable, details=details or {}
    )


def _canonical_data_root(value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(candidate))
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("data root must not contain symlinked components")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("data root must be a directory")
    return candidate.resolve(strict=False)


def _remove_partial_staging(
    destination: Path,
    destination_identity: tuple[int, int] | None,
    succeeded: bool,
    destination_preexisting: bool,
    work: Path | None,
    owned_files: list[tuple[Path, tuple[int, int]]],
    owned_dirs: list[tuple[Path, tuple[int, int]]],
    staging_root: Path | None,
    staging_root_identity: tuple[int, int] | None,
    work_identity: tuple[int, int] | None,
    work_root: Path | None,
) -> None:
    root_is_unchanged = (
        staging_root is not None
        and staging_root_identity is not None
        and _same_directory_identity(staging_root, staging_root_identity)
        and _has_no_symlink_components(Path(staging_root.anchor), staging_root)
    )
    if (
        work is not None
        and work_identity is not None
        and _same_directory_identity(work, work_identity)
        and work_root is not None
        and _has_no_symlink_components(Path(work_root.anchor), work_root)
        and _has_no_symlink_components(work_root, work)
        and work.is_dir()
        and not work.is_symlink()
    ):
        _remove_tree_no_follow(work, expected_identity=work_identity)
    if succeeded:
        return
    if (
        not destination_preexisting
        and destination_identity is not None
        and _same_directory_identity(destination, destination_identity)
        and root_is_unchanged
        and _has_no_symlink_components(Path(destination.anchor), destination)
        and destination.is_dir()
        and not destination.is_symlink()
    ):
        _remove_tree_no_follow(destination, expected_identity=destination_identity)
        return
    if not destination_preexisting and destination_identity is not None:
        return
    for path, identity in owned_files:
        if _same_regular_identity(path, identity) and _has_no_symlink_components(
            Path(destination.anchor), path
        ):
            path.unlink()
    for path, identity in sorted(owned_dirs, key=lambda item: len(item[0].parts), reverse=True):
        if (
            _same_directory_identity(path, identity)
            and root_is_unchanged
            and _has_no_symlink_components(Path(destination.anchor), path)
        ):
            path.rmdir()


def _regular_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("owned staging file is not regular")
    return metadata.st_dev, metadata.st_ino


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("owned staging directory is not a directory")
    return metadata.st_dev, metadata.st_ino


def _same_regular_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        return _regular_identity(path) == identity
    except OSError:
        return False


def _same_directory_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        return _directory_identity(path) == identity
    except OSError, ValueError:
        return False


def _has_no_symlink_components(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for component in (".", *relative.parts):
        if component != ".":
            current /= component
        try:
            metadata = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            return False
    return True


def _remove_tree_no_follow(path: Path, *, expected_identity: tuple[int, int] | None = None) -> None:
    """Remove one path without following replacements during recursive cleanup."""
    parent_fd = _open_directory_fd(path.parent)
    try:
        _remove_directory_entry_at(parent_fd, path.name, expected_identity)
    finally:
        os.close(parent_fd)


def _remove_directory_entry_at(
    parent_fd: int, name: str, expected_identity: tuple[int, int] | None
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    identity = (metadata.st_dev, metadata.st_ino)
    if expected_identity is not None and identity != expected_identity:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return

    try:
        directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except NotADirectoryError:
        os.unlink(name, dir_fd=parent_fd)
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            os.unlink(name, dir_fd=parent_fd)
            return
        raise
    try:
        if _directory_identity_from_fd(directory_fd) != identity:
            return
        entries: list[tuple[str, tuple[int, int]]] = []
        with os.scandir(directory_fd) as directory_entries:
            for entry in directory_entries:
                try:
                    child_metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                entries.append((entry.name, (child_metadata.st_dev, child_metadata.st_ino)))
        for child_name, child_identity in entries:
            _remove_directory_entry_at(directory_fd, child_name, child_identity)
    finally:
        os.close(directory_fd)

    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity and stat.S_ISDIR(current.st_mode):
        os.rmdir(name, dir_fd=parent_fd)
