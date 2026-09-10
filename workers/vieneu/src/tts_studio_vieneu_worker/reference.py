"""Validation and descriptor-safe access to Core-owned references."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

import numpy as np
import soundfile as sf
from tts_studio_protocol.engine.v1 import engine_pb2
from tts_studio_worker_sdk.limits import MAX_REFERENCE_COMPRESSED_BYTES

_REFERENCE_PREFIX: Final[tuple[str, str]] = ("staging", "references")
_MAX_DURATION_SECONDS: Final[float] = 8.0
_MAX_TRANSCRIPT_CHARS: Final[int] = 2000
_MAX_DECODED_SAMPLES: Final[int] = 8 * 192000 * 2
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


class ReferenceValidationError(Exception):
    """A safe, structured reference validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class VieNeuReferenceValidator:
    """Decode references through an opened, immutable descriptor."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root).expanduser().absolute()

    def validate(
        self, model_id: str, reference_path: str, transcript: str | None
    ) -> engine_pb2.ReferenceMetadata:
        del model_id
        with self.open_reference(reference_path) as file_descriptor:
            return self.validate_descriptor(file_descriptor, reference_path, transcript)

    @contextmanager
    def open_reference(self, reference_path: str) -> Iterator[int]:
        """Open every component relative to an O_NOFOLLOW directory descriptor."""
        relative = self._validated_relative_path(reference_path)
        descriptor = self._open_confined_file(relative)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def validate_descriptor(
        self, file_descriptor: int, reference_path: str, transcript: str | None
    ) -> engine_pb2.ReferenceMetadata:
        self.validate_transcript(transcript)
        extension = Path(reference_path).suffix.lower()
        if extension not in {".wav", ".flac"}:
            raise ReferenceValidationError(
                "reference_unsupported", "The reference container is unsupported"
            )
        try:
            byte_size = os.fstat(file_descriptor).st_size
            if byte_size > MAX_REFERENCE_COMPRESSED_BYTES:
                raise ReferenceValidationError(
                    "reference_too_large", "The reference audio file is too large"
                )
            with os.fdopen(os.dup(file_descriptor), "rb") as stream, sf.SoundFile(
                stream, mode="r"
            ) as audio:
                    sample_rate = int(audio.samplerate)
                    channels = int(audio.channels)
                    frames = int(audio.frames)
                    if sample_rate <= 0 or channels not in {1, 2} or frames <= 0:
                        raise ReferenceValidationError(
                            "reference_invalid", "The reference audio metadata is invalid"
                        )
                    if frames * channels > _MAX_DECODED_SAMPLES:
                        raise ReferenceValidationError(
                            "reference_too_large", "The decoded reference audio is too large"
                        )
                    duration = frames / sample_rate
                    if duration <= 0 or duration > _MAX_DURATION_SECONDS:
                        raise ReferenceValidationError(
                            "reference_invalid", "The reference duration must be at most 8 seconds"
                        )
                    saw_samples = False
                    while True:
                        samples = audio.read(frames=65536, dtype="float32", always_2d=True)
                        if samples.size == 0:
                            break
                        saw_samples = True
                        if not np.isfinite(samples).all():
                            raise ReferenceValidationError(
                                "reference_invalid", "The reference audio samples are invalid"
                            )
                    if not saw_samples:
                        raise ReferenceValidationError(
                            "reference_invalid", "The reference audio samples are invalid"
                        )
                    container = str(audio.format or extension[1:]).lower()
        except ReferenceValidationError:
            raise
        except (OSError, ValueError, RuntimeError, sf.SoundFileError) as error:
            raise ReferenceValidationError(
                "reference_invalid", "The reference audio could not be decoded"
            ) from error
        return engine_pb2.ReferenceMetadata(
            sample_rate_hz=sample_rate,
            channels=channels,
            duration_ms=round(duration * 1000),
            byte_size=byte_size,
            container=container,
        )

    def confined_path(self, reference_path: str) -> Path:
        """Return a lexical path; callers must use :meth:`open_reference`."""
        return self._data_root.joinpath(*self._validated_relative_path(reference_path).parts)

    def _validated_relative_path(self, reference_path: str) -> PurePosixPath:
        relative = PurePosixPath(reference_path)
        if (
            not reference_path
            or "\\" in reference_path
            or relative.is_absolute()
            or PureWindowsPath(reference_path).drive
            or PureWindowsPath(reference_path).is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 3
            or tuple(relative.parts[:2]) != _REFERENCE_PREFIX
        ):
            raise ReferenceValidationError("reference_invalid", "The reference path is invalid")
        return relative

    def _open_confined_file(self, relative: PurePosixPath) -> int:
        if not _OPEN_SUPPORTS_DIR_FD or not hasattr(os, "O_NOFOLLOW"):
            raise ReferenceValidationError(
                "reference_unsupported", "This platform cannot safely open references"
            )
        root_descriptor = -1
        directory_descriptor = -1
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_descriptor = os.open(self._data_root, directory_flags)
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
                raise ReferenceValidationError("reference_invalid", "The reference file is unavailable")
            return descriptor
        except ReferenceValidationError:
            raise
        except OSError as error:
            raise ReferenceValidationError("reference_invalid", "The reference file is unavailable") from error
        finally:
            if directory_descriptor >= 0 and directory_descriptor != root_descriptor:
                os.close(directory_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    @staticmethod
    def validate_transcript(transcript: str | None) -> None:
        if transcript is None:
            return
        try:
            transcript.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ReferenceValidationError(
                "reference_invalid", "The reference transcript is invalid"
            ) from error
        if not transcript or "\x00" in transcript or len(transcript) > _MAX_TRANSCRIPT_CHARS:
            raise ReferenceValidationError("reference_invalid", "The reference transcript is invalid")
