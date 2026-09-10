"""Deterministic Hugging Face-like model behavior for adapter compliance tests."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import re
import stat
import wave
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tts_studio_protocol.engine.v1 import engine_pb2

_ENGINE_ID = "fake"
_ENGINE_VERSION = "0.2.0"
_DESTINATION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_CONFIG_CONTENT = b'{"architecture":"fake-tts","sample_rate":24000}\n'
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


@dataclass(frozen=True)
class _Fixture:
    resolved_commit: str
    compatible: bool = True
    slow: bool = False
    corrupt_manifest: bool = False


@dataclass(frozen=True)
class _DirectoryWriter:
    """Write new files through an anchored or rename-locked directory."""

    descriptor: int | None = None
    path: Path | None = None

    def write_file(self, relative_path: str, content: bytes) -> None:
        if Path(relative_path).name != relative_path:
            raise ValueError("fixture artifact path must be a single component")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOINHERIT", 0)
        )
        if self.descriptor is not None:
            file_descriptor = os.open(
                relative_path,
                flags,
                0o600,
                dir_fd=self.descriptor,
            )
        elif self.path is not None:
            file_descriptor = os.open(self.path / relative_path, flags, 0o600)
        else:
            raise RuntimeError("directory writer has no anchored destination")
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                file_descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)


_FIXTURES = {
    "fixtures/compatible": _Fixture(resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3"),
    "fixtures/incompatible": _Fixture(
        resolved_commit="eeadf37ad393d30acfe21c22b45bcc8c2f01048e",
        compatible=False,
    ),
    "fixtures/checksum-failure": _Fixture(
        resolved_commit="d650fc66884d6c22e1988fab5be5e5d529da4704",
        corrupt_manifest=True,
    ),
    "fixtures/slow": _Fixture(
        resolved_commit="57f6fdf87f900bf21ae604817ac739186237758f",
        slow=True,
    ),
}
_VARIANTS = (
    engine_pb2.ModelVariant(id="int8", label="INT8"),
    engine_pb2.ModelVariant(id="fp32", label="FP32"),
)
_PRESET_VOICE = engine_pb2.PresetVoice(
    id="fake-neutral",
    label="Fake Neutral",
    capabilities=["preset", "deterministic"],
)


class FakeModelService:
    """Keep fake model lifecycle, voices, validation, and downloads in Worker memory."""

    def __init__(self, staging_root: Path | None) -> None:
        self._staging_root = staging_root.expanduser().absolute() if staging_root else None
        self._root_identity: tuple[int, int] | None = None
        if self._staging_root is not None:
            if self._staging_root.is_symlink():
                raise ValueError("fake adapter staging root must not be a symbolic link")
            self._staging_root.mkdir(parents=True, exist_ok=True)
            self._root_identity = _directory_identity(self._checked_staging_root())
        self._loaded_models: dict[str, str] = {}

    def load_model(self, request: engine_pb2.LoadModelRequest) -> engine_pb2.LoadModelResponse:
        # Core assigns an opaque Model Installation ID when it activates a
        # repository. The Worker has already validated the repository during
        # the download path, so lifecycle calls must accept that installed ID
        # rather than requiring the original repository ID.
        if not request.model_id or request.model_id == "fixtures/incompatible":
            return engine_pb2.LoadModelResponse(
                loaded=False,
                error=self._generation_error(
                    "model_incompatible", "The requested model is not compatible with fake"
                ),
            )
        if request.variant not in {"int8", "fp32"}:
            return engine_pb2.LoadModelResponse(
                loaded=False,
                error=self._generation_error("variant_unsupported", "The requested variant is unsupported"),
            )
        self._loaded_models[request.model_id] = request.variant
        return engine_pb2.LoadModelResponse(loaded=True)

    def unload_model(self, model_id: str) -> engine_pb2.UnloadModelResponse:
        self._loaded_models.pop(model_id, None)
        return engine_pb2.UnloadModelResponse(unloaded=True)

    def list_voices(self, model_id: str) -> engine_pb2.ListVoicesResponse:
        if model_id not in self._loaded_models:
            return engine_pb2.ListVoicesResponse(
                error=self._generation_error("model_not_loaded", "The requested model is not loaded")
            )
        return engine_pb2.ListVoicesResponse(voices=[_PRESET_VOICE])

    def synthesize_error(
        self, request: engine_pb2.SynthesizeRequest, reference_bytes: bytes | None = None
    ) -> engine_pb2.WorkerError | None:
        if request.model_id not in self._loaded_models:
            return self._generation_error("model_not_loaded", "The requested model is not loaded")
        source = request.WhichOneof("voice_source")
        if source == "reference":
            try:
                if reference_bytes is None:
                    reference_bytes = self.reference_bytes(request)
                if request.reference.HasField("transcript") and (
                    not request.reference.transcript
                    or "\x00" in request.reference.transcript
                    or len(request.reference.transcript) > 2000
                ):
                    raise ValueError("invalid transcript")
            except (OSError, ValueError):
                return self._generation_error("reference_invalid", "The reference file is unavailable")
        elif request.voice_id != _PRESET_VOICE.id:
            return self._generation_error("voice_not_found", "The requested voice is unavailable")
        elif source != "voice_id":
            return self._generation_error("invalid_request", "A preset voice or reference is required")
        if not request.text.strip():
            return self._generation_error("invalid_request", "Synthesis text must not be empty")
        return None

    def validate_reference(
        self, request: engine_pb2.ValidateReferenceRequest
    ) -> engine_pb2.ValidateReferenceResponse:
        if request.model_id not in self._loaded_models:
            return engine_pb2.ValidateReferenceResponse(
                error=self._generation_error("model_not_loaded", "The requested model is not loaded")
            )
        try:
            with self._open_reference(request.reference_path) as descriptor:
                with wave.open(os.fdopen(os.dup(descriptor), "rb"), "rb") as stream:
                    channels = stream.getnchannels()
                    sample_rate = stream.getframerate()
                    frames = stream.getnframes()
                    if channels not in {1, 2} or sample_rate <= 0 or frames <= 0:
                        raise ValueError("invalid reference audio")
                    duration = frames / sample_rate
                    if duration > 8.0:
                        raise ValueError("reference is too long")
                byte_size = os.fstat(descriptor).st_size
            if request.HasField("transcript") and (
                not request.transcript or "\x00" in request.transcript or len(request.transcript) > 2000
            ):
                raise ValueError("invalid transcript")
        except (OSError, ValueError, wave.Error):
            return engine_pb2.ValidateReferenceResponse(
                error=self._generation_error("reference_invalid", "The reference is invalid")
            )
        return engine_pb2.ValidateReferenceResponse(
            valid=True,
            metadata=engine_pb2.ReferenceMetadata(
                sample_rate_hz=sample_rate,
                channels=channels,
                duration_ms=round(duration * 1000),
                byte_size=byte_size,
                container="wav",
            ),
        )

    @contextmanager
    def _open_reference(self, reference_path: str) -> Iterator[int]:
        if not reference_path or "\\" in reference_path:
            raise ValueError("invalid reference path")
        relative = Path(reference_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid reference path")
        if len(relative.parts) != 3 or relative.parts[:2] != ("staging", "references"):
            raise ValueError("invalid reference path")
        root = self._staging_root.parent if self._staging_root is not None else None
        if root is None:
            raise ValueError("reference staging is unavailable")
        if not _OPEN_SUPPORTS_DIR_FD or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("reference path is unsupported")
        root_descriptor = -1
        directory_descriptor = -1
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_descriptor = os.open(root, directory_flags)
            directory_descriptor = root_descriptor
            for component in relative.parts[:-1]:
                next_descriptor = os.open(component, directory_flags, dir_fd=directory_descriptor)
                if directory_descriptor != root_descriptor:
                    os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ValueError("reference file is unavailable")
            yield descriptor
        finally:
            if directory_descriptor >= 0 and directory_descriptor != root_descriptor:
                os.close(directory_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    def _reference_path(self, reference_path: str) -> Path:
        """Return only a lexical path for compatibility with existing callers."""
        if not reference_path or "\\" in reference_path:
            raise ValueError("invalid reference path")
        relative = Path(reference_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid reference path")
        if len(relative.parts) != 3 or relative.parts[:2] != ("staging", "references"):
            raise ValueError("invalid reference path")
        root = self._staging_root.parent if self._staging_root is not None else None
        if root is None:
            raise ValueError("reference staging is unavailable")
        return root.joinpath(*relative.parts)

    def variant_for_model(self, model_id: str) -> str:
        return self._loaded_models[model_id]

    @contextmanager
    def _open_audio(self, audio_path: str) -> Iterator[int]:
        if not audio_path or "\\" in audio_path:
            raise ValueError("invalid audio path")
        relative = Path(audio_path)
        managed_audio = len(relative.parts) >= 2 and relative.parts[0] == "audio"
        alignment_snapshot = (
            len(relative.parts) == 2
            and relative.parts[0] == "staging"
            and relative.parts[1].startswith(".alignment-")
            and relative.parts[1].endswith(".wav")
        )
        if relative.is_absolute() or ".." in relative.parts or not (
            managed_audio or alignment_snapshot
        ):
            raise ValueError("invalid audio path")
        root = self._staging_root.parent if self._staging_root is not None else None
        if root is None or not _OPEN_SUPPORTS_DIR_FD or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("audio staging is unavailable")
        root_descriptor = -1
        directory_descriptor = -1
        descriptor = -1
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_descriptor = os.open(root, directory_flags)
            directory_descriptor = root_descriptor
            for component in relative.parts[:-1]:
                next_descriptor = os.open(component, directory_flags, dir_fd=directory_descriptor)
                if directory_descriptor != root_descriptor:
                    os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("audio file is unavailable")
            yield descriptor
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if directory_descriptor >= 0 and directory_descriptor != root_descriptor:
                os.close(directory_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    def reference_bytes(self, request: engine_pb2.SynthesizeRequest) -> bytes | None:
        if request.WhichOneof("voice_source") != "reference":
            return None
        with self._open_reference(request.reference.reference_path) as descriptor:
            return os.read(descriptor, os.fstat(descriptor).st_size)

    @staticmethod
    def _generation_error(code: str, message: str) -> engine_pb2.WorkerError:
        return engine_pb2.WorkerError(
            code=code,
            message=message,
            retryable=False,
            details={"adapter": _ENGINE_ID},
        )

    def validate_model(
        self, request: engine_pb2.ValidateModelRequest
    ) -> engine_pb2.ValidateModelResponse:
        """Return deterministic compatibility evidence without filesystem side effects."""

        fixture = _FIXTURES.get(request.repository_id)
        requested_revision = (
            request.requested_revision if request.HasField("requested_revision") else None
        )
        if fixture is None:
            return self._validation_error(
                repository_id="",
                requested_revision=requested_revision,
                code="model_incompatible",
                message="No compatible fake adapter fixture was found",
            )

        if requested_revision not in (None, "main", fixture.resolved_commit):
            return self._validation_error(
                repository_id=request.repository_id,
                requested_revision=requested_revision,
                code="revision_not_found",
                message="The requested fixture revision was not found",
            )

        if not fixture.compatible:
            response = self._validation_error(
                repository_id=request.repository_id,
                requested_revision=requested_revision,
                code="model_incompatible",
                message="The fixture is intentionally incompatible",
            )
            response.evidence.append(
                engine_pb2.CompatibilityEvidence(
                    code="fake_fixture_incompatible",
                    message="The fake adapter rejected this deterministic fixture",
                )
            )
            return response

        files = self._fixture_files("int8")
        response = engine_pb2.ValidateModelResponse(
            repository_id=request.repository_id,
            resolved_commit=fixture.resolved_commit,
            compatible=True,
            engine_id=_ENGINE_ID,
            engine_version=_ENGINE_VERSION,
            required_files=["config.json", "model.bin"],
            available_variants=_VARIANTS,
            evidence=[
                engine_pb2.CompatibilityEvidence(
                    code="fake_fixture_compatible",
                    message="The fake adapter recognized this deterministic fixture",
                )
            ],
        )
        if requested_revision is not None:
            response.requested_revision = requested_revision
        if not fixture.slow:
            response.estimated_bytes = sum(len(content) for _, content in files)
        return response

    async def download_model(
        self, request: engine_pb2.DownloadModelRequest
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        """Write a fixture into one preallocated, confined staging directory."""

        fixture = _FIXTURES.get(request.repository_id)
        if (
            fixture is None
            or not fixture.compatible
            or request.resolved_commit != fixture.resolved_commit
            or request.variant not in {"int8", "fp32"}
        ):
            yield self._download_error("The validated fixture selection is unavailable")
            return

        files = self._fixture_files(request.variant)
        total_bytes = sum(len(content) for _, content in files)
        bytes_downloaded = 0
        sequence = 1
        try:
            with self._open_destination(request.staging_destination) as destination:
                yield self._progress(
                    sequence,
                    engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
                    bytes_downloaded,
                    total_bytes=None if fixture.slow else total_bytes,
                    message="Starting fixture download",
                )

                for relative_path, content in files:
                    await asyncio.sleep(0.1 if fixture.slow else 0)
                    destination.write_file(relative_path, content)
                    bytes_downloaded += len(content)
                    sequence += 1
                    yield self._progress(
                        sequence,
                        engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
                        bytes_downloaded,
                        total_bytes=None if fixture.slow else total_bytes,
                        message="Downloaded fixture artifact",
                    )

                sequence += 1
                yield self._progress(
                    sequence,
                    engine_pb2.DOWNLOAD_PHASE_VERIFYING,
                    bytes_downloaded,
                    total_bytes=None if fixture.slow else total_bytes,
                    message="Verifying fixture artifacts",
                )
                sequence += 1
                yield self._progress(
                    sequence,
                    engine_pb2.DOWNLOAD_PHASE_FINALIZING,
                    bytes_downloaded,
                    total_bytes=None if fixture.slow else total_bytes,
                    message="Finalizing fixture manifest",
                )
        except (OSError, ValueError):
            yield self._download_error("The staging destination could not be written")
            return

        manifest_files = []
        for relative_path, content in files:
            checksum = hashlib.sha256(content).hexdigest()
            if fixture.corrupt_manifest and relative_path == "model.bin":
                checksum = "0" * 64
            manifest_files.append(
                engine_pb2.ManifestFile(
                    relative_path=relative_path,
                    byte_size=len(content),
                    sha256=checksum,
                )
            )
        yield engine_pb2.DownloadModelEvent(
            manifest=engine_pb2.ModelManifest(
                repository_id=request.repository_id,
                resolved_commit=request.resolved_commit,
                variant=request.variant,
                files=manifest_files,
                byte_size=total_bytes,
            )
        )

    def _checked_staging_root(self) -> os.stat_result:
        if self._staging_root is None:
            raise ValueError("fake adapter staging root is not configured")
        metadata = self._staging_root.lstat()
        resolved = self._staging_root.resolve(strict=True)
        if (
            self._staging_root.is_symlink()
            or _is_reparse_point(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != self._staging_root
        ):
            raise ValueError("fake adapter staging root is unsafe")
        return metadata

    @contextmanager
    def _open_destination(self, identifier: str) -> Iterator[_DirectoryWriter]:
        if self._staging_root is None or self._root_identity is None:
            raise ValueError("fake adapter staging root is not configured")
        if _DESTINATION_PATTERN.fullmatch(identifier) is None:
            raise ValueError("invalid staging destination identifier")
        if os.name == "nt":
            with self._open_windows_destination(identifier) as destination:
                yield destination
            return

        if not _OPEN_SUPPORTS_DIR_FD:
            raise OSError("platform cannot anchor staging directory writes")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_descriptor = os.open(self._staging_root, flags)
        destination_descriptor: int | None = None
        try:
            root_metadata = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or _directory_identity(root_metadata) != self._root_identity
            ):
                raise ValueError("fake adapter staging root identity changed")
            destination_descriptor = os.open(
                identifier,
                flags,
                dir_fd=root_descriptor,
            )
            destination_metadata = os.fstat(destination_descriptor)
            if not stat.S_ISDIR(destination_metadata.st_mode):
                raise ValueError("staging destination is not a directory")
            yield _DirectoryWriter(descriptor=destination_descriptor)
        finally:
            if destination_descriptor is not None:
                os.close(destination_descriptor)
            os.close(root_descriptor)

    @contextmanager
    def _open_windows_destination(self, identifier: str) -> Iterator[_DirectoryWriter]:
        root_before = self._checked_staging_root()
        if _directory_identity(root_before) != self._root_identity:
            raise ValueError("fake adapter staging root identity changed")
        with _locked_windows_directory(self._staging_root):
            root_after = self._checked_staging_root()
            if _directory_identity(root_after) != self._root_identity:
                raise ValueError("fake adapter staging root identity changed")

            destination = self._staging_root / identifier
            destination_before = _checked_windows_directory(destination)
            with _locked_windows_directory(destination):
                destination_after = _checked_windows_directory(destination)
                if _directory_identity(destination_after) != _directory_identity(
                    destination_before
                ):
                    raise ValueError("staging destination identity changed")
                yield _DirectoryWriter(path=destination)

    @staticmethod
    def _fixture_files(variant: str) -> tuple[tuple[str, bytes], ...]:
        return (
            ("config.json", _CONFIG_CONTENT),
            ("model.bin", f"fake-model:{variant}\n".encode()),
        )

    @staticmethod
    def _progress(
        sequence: int,
        phase: engine_pb2.DownloadPhase.ValueType,
        bytes_downloaded: int,
        *,
        total_bytes: int | None,
        message: str,
    ) -> engine_pb2.DownloadModelEvent:
        progress = engine_pb2.DownloadProgress(
            sequence=sequence,
            phase=phase,
            bytes_downloaded=bytes_downloaded,
            message=message,
        )
        if total_bytes is not None:
            progress.total_bytes = total_bytes
        return engine_pb2.DownloadModelEvent(progress=progress)

    @staticmethod
    def _download_error(message: str) -> engine_pb2.DownloadModelEvent:
        return engine_pb2.DownloadModelEvent(
            error=engine_pb2.WorkerError(
                code="download_failed",
                message=message,
                retryable=False,
                details={"adapter": _ENGINE_ID},
            )
        )

    @staticmethod
    def _validation_error(
        *,
        repository_id: str,
        requested_revision: str | None,
        code: str,
        message: str,
    ) -> engine_pb2.ValidateModelResponse:
        response = engine_pb2.ValidateModelResponse(
            repository_id=repository_id,
            compatible=False,
            engine_id=_ENGINE_ID,
            engine_version=_ENGINE_VERSION,
            error=engine_pb2.WorkerError(
                code=code,
                message=message,
                retryable=False,
                details={"adapter": _ENGINE_ID},
            ),
        )
        if requested_revision is not None:
            response.requested_revision = requested_revision
        return response


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _checked_windows_directory(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if path.is_symlink() or _is_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("staging destination is not a safe directory")
    return metadata


@contextmanager
def _locked_windows_directory(path: Path) -> Iterator[None]:
    """Prevent rename/delete while path-based child opens occur on Windows."""

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows directory locking is unavailable")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_file: Any = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x0001 | 0x0002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deny delete/rename
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error_code = _windows_last_error()
        raise OSError(error_code, "could not lock staging directory")
    try:
        yield
    finally:
        close_handle: Any = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        if not close_handle(handle):
            error_code = _windows_last_error()
            raise OSError(error_code, "could not close staging directory lock")


def _windows_last_error() -> int:
    get_last_error = getattr(ctypes, "get_last_error", None)
    return int(get_last_error()) if callable(get_last_error) else 0
