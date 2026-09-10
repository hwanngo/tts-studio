"""Core-owned orchestration for durable speech Generation Jobs."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import math
import os
import re
import stat
import sys
import tempfile
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import uuid4

from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.events import EventStore
from tts_studio.generation.audio import (
    AudioValidationError,
    ByteBudgetQueue,
    ByteBudgetQueueSignal,
    PcmResult,
    PcmValidator,
)
from tts_studio.generation.domain import (
    AlignmentJob,
    AlignmentResult,
    AlignmentState,
    AlignmentUnit,
    AudioArtifact,
    GenerationJob,
    GenerationState,
    SynthesisOptions,
)
from tts_studio.generation.registry import (
    GenerationRecordNotFoundError,
    GenerationRegistry,
    InvalidGenerationTransitionError,
    InvalidRegistryDataError,
)
from tts_studio.generation.scheduler import GenerationScheduler
from tts_studio.generation.wav import WavArtifactWriter
from tts_studio.models.registry import ModelInstallation, RegistryRecordNotFoundError
from tts_studio.providers.registry import ProviderNotFoundError, ProviderRegistry
from tts_studio.providers.service import ProviderService
from tts_studio.references.domain import (
    CleanupFailedError,
    InvalidReferenceTransitionError,
    ReferenceHandle,
    ReferenceRecoveryRequiredError,
)
from tts_studio.references.registry import ReferenceRecordNotFoundError
from tts_studio.references.service import ReferenceService
from tts_studio.storage.identity import (
    IdentityBoundUnlinkError,
    IdentityBoundUnlinkMismatchError,
    IdentityBoundUnlinkUnavailableError,
    unlink_open_file,
)
from tts_studio.storage.layout import ManagedDirectoryName, StorageLayout, UnsafeStoragePathError
from tts_studio.voices.service import SavedVoiceService
from tts_studio.workers.generation import (
    AlignmentCapability,
    WorkerOperationError,
    WorkerReplicaPool,
    build_synthesis_request,
)


class GenerationModelNotFoundError(LookupError):
    """Raised when a requested Model Installation is not active."""


class GenerationJobNotFoundError(LookupError):
    """Raised when a requested Generation Job is not durable."""


class GenerationVoiceNotFoundError(ValueError):
    """Raised when the Worker does not report the requested runtime Voice."""


class GenerationArtifactNotFoundError(LookupError):
    """Raised when a retained Audio Artifact is not available."""


class GenerationArtifactInvalidError(ValueError):
    """Raised when a retained Audio Artifact no longer matches its identity."""


class GenerationArtifactDeletionError(RuntimeError):
    """Raised when an artifact cannot be deleted without weakening identity safety."""


class GenerationRequestError(ValueError):
    """Raised when a generation request is not safe to queue."""


class GenerationReferenceNotFoundError(LookupError):
    """Raised when a referenced recording is unknown, expired, or already consumed."""


class GenerationCapabilityError(RuntimeError):
    """Raised when the current Worker cannot stream synthesis."""


_PREVIEW_MAX_PCM_BYTES = 4 * 1024 * 1024
_EPHEMERAL_REPLAY_MAX_BYTES = 8 * 1024 * 1024
_MAX_ALIGNMENT_TRANSCRIPT = 2_000
_MAX_ALIGNMENT_UNITS = 10_000
_MAX_ALIGNMENT_PROTO_BYTES = 1024 * 1024
_ALIGNMENT_UNITS = frozenset({"word", "phoneme", "character"})
_PCM_QUEUE_CLOSED_ERRORS = tuple(
    error
    for error in (getattr(asyncio, "QueueShutDown", None),)
    if isinstance(error, type)
)


@dataclass(frozen=True)
class _ReferenceHandle:
    reference_id: str
    relative_path: str
    transcript: str | None
    temporary: bool = True


@dataclass
class _ValidatedArtifactDeletion:
    root_fd: int
    root_identity: tuple[int, int]
    directory_fd: int
    directory_identity: tuple[int, int]
    file_fd: int
    name: str
    snapshot: Any

    def close(self) -> None:
        try:
            self.snapshot.close()
        finally:
            try:
                os.close(self.file_fd)
            finally:
                try:
                    os.close(self.directory_fd)
                finally:
                    os.close(self.root_fd)


class ModelInstallationProvider(Protocol):
    def get_model(self, model_id: str) -> ModelInstallation: ...


class _GenerationFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class _GenerationCancelled(RuntimeError):
    pass


class GenerationService:
    """Deep Core module hiding persistence, scheduling, Workers, and files."""

    def __init__(
        self,
        registry: GenerationRegistry,
        model_registry: ModelInstallationProvider,
        worker_pool: WorkerReplicaPool,
        *,
        layout: StorageLayout,
        event_store: EventStore,
        queue_bytes: int = 1024 * 1024,
        reference_service: ReferenceService | None = None,
        saved_voice_service: SavedVoiceService | None = None,
        provider_registry: ProviderRegistry | None = None,
        retention_default_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._model_registry = model_registry
        self._worker_pool = worker_pool
        self._layout = layout
        self._event_store = event_store
        self._queue_bytes = queue_bytes
        self._reference_service = reference_service
        self._saved_voice_service = saved_voice_service
        self._provider_registry = provider_registry
        self._retention_default_provider = retention_default_provider
        self._scheduler = GenerationScheduler(self._run_job)
        self._alignment_scheduler = GenerationScheduler(self._run_alignment)
        self._replica_gate = asyncio.Lock()
        self._streams: dict[str, AsyncIterator[engine_pb2.SynthesisEvent]] = {}
        self._pcm_subscribers: dict[str, set[asyncio.Queue[bytes | None]]] = {}
        self._pcm_terminated: dict[str, set[asyncio.Queue[bytes | None]]] = {}
        self._pcm_replay: dict[str, bytearray] = {}
        self._pcm_replay_disabled: set[str] = set()
        self._pcm_subscription_locks: dict[str, asyncio.Lock] = {}
        self._reference_handles: dict[str, _ReferenceHandle] = {}
        self._closing = False

    def set_retention_default_provider(self, provider: Callable[[], bool]) -> None:
        self._retention_default_provider = provider

    def resolve_retention(self, retain_artifact: bool | None) -> bool:
        if retain_artifact is not None:
            return retain_artifact
        if self._retention_default_provider is not None:
            return self._retention_default_provider()
        return True

    async def create(
        self,
        *,
        model_id: str,
        voice_id: str | None = None,
        reference_id: str | None = None,
        saved_voice_id: str | None = None,
        text: str,
        retain_artifact: bool | None = None,
        correlation_id: str | None = None,
        options: SynthesisOptions | None = None,
    ) -> GenerationJob:
        retain_artifact = self.resolve_retention(retain_artifact)
        _validate_text(text)
        _validate_options(options)
        _validate_source(voice_id, reference_id, saved_voice_id)
        model = self._get_model(model_id)
        provider_id = model_id.removeprefix("provider:") if model_id.startswith("provider:") else None
        if provider_id is not None:
            if self._provider_registry is None:
                raise GenerationModelNotFoundError from None
            try:
                self._provider_registry.get(provider_id)
            except ProviderNotFoundError as error:
                raise GenerationModelNotFoundError from error
        reference_handle: _ReferenceHandle | None = None
        async with self._replica_gate, self._worker_pool.acquire(model) as lease:
            _require_streaming_capability(lease)
            _require_option_capabilities(lease, options)
            if reference_id is not None or saved_voice_id is not None:
                if saved_voice_id is not None:
                    if self._saved_voice_service is None:
                        raise GenerationRequestError("saved Voices are not configured")
                    try:
                        saved_voice = self._saved_voice_service.get(saved_voice_id)
                    except Exception as error:
                        raise GenerationReferenceNotFoundError from error
                    recording_model_id = saved_voice.model_id
                else:
                    if self._reference_service is None:
                        raise GenerationRequestError("reference generation is not configured")
                    try:
                        recording = self._reference_service.get(reference_id or "")
                    except ReferenceRecordNotFoundError as error:
                        raise GenerationReferenceNotFoundError from error
                    recording_model_id = recording.model_id
                if recording_model_id != model.id:
                    raise GenerationRequestError("the reference is pinned to a different model")
                await lease.load_model(model)
                try:
                    if not lease.capabilities.supports("reference_cloning"):
                        raise GenerationCapabilityError(
                            "the Worker does not support reference cloning"
                        )
                finally:
                    await self._unload_model(lease, model)
            else:
                _require_preset_capability(lease)
            if reference_id is not None:
                try:
                    claimed = self._reference_service.claim_for_generation(reference_id)
                except (InvalidReferenceTransitionError, ReferenceRecordNotFoundError) as error:
                    raise GenerationReferenceNotFoundError from error
                except ReferenceRecoveryRequiredError as error:
                    raise GenerationRequestError("reference recovery is required before generation") from error
                reference_handle = _reference_handle(claimed)
            elif saved_voice_id is not None:
                saved_voice = self._saved_voice_service.get(saved_voice_id)  # type: ignore[union-attr]
                reference_handle = _ReferenceHandle(
                    saved_voice.id, saved_voice.relative_path, saved_voice.transcript, False
                )
            else:
                await lease.load_model(model)
                try:
                    _require_preset_capability(lease)
                    voices = await lease.list_voices()
                    if not any(voice.id == voice_id for voice in voices):
                        raise GenerationVoiceNotFoundError("the requested voice is not available")
                finally:
                    await self._unload_model(lease, model)

        try:
            job = self._registry.create_job(
                model_id=model.id,
                engine_id=_engine_id(model),
                voice_id=voice_id,
                reference_id=reference_id,
                saved_voice_id=saved_voice_id,
                provider_id=provider_id,
                text=text,
                retain_artifact=retain_artifact,
                options=options,
                correlation_id=correlation_id or str(uuid4()),
            )
        except BaseException:
            if reference_handle is not None:
                self._release_reference(reference_handle)
            raise
        if reference_handle is not None:
            self._reference_handles[job.id] = reference_handle
        self._publish(job, "generation.queued", "Generation was queued.")
        try:
            self._scheduler.submit(job.id)
        except BaseException:
            self._reference_handles.pop(job.id, None)
            cleanup_failed = not self._release_reference(reference_handle) if reference_handle is not None else False
            if cleanup_failed:
                self._fail_job(job, "cleanup_failed", "Generation cleanup failed; restart Core to retry cleanup.", False)
            else:
                cleared = self._registry.clear_reference_id(job.id) if reference_id is not None else job
                self._fail_job(cleared, "scheduler_failed", "Generation could not be scheduled.", False)
            raise
        return job

    def get(self, job_id: str) -> GenerationJob:
        try:
            return self._registry.get_job(job_id)
        except GenerationRecordNotFoundError as error:
            raise GenerationJobNotFoundError from error

    def list(self) -> tuple[GenerationJob, ...]:
        return self._registry.list_jobs()

    async def wait(self, job_id: str) -> GenerationJob:
        await self._scheduler.wait(job_id)
        return self.get(job_id)

    async def request_alignment(self, job_id: str, correlation_id: str) -> AlignmentJob:
        job = self.get(job_id)
        existing = self.get_alignment(job_id)
        artifact = self._alignment_artifact(job)
        self._verify_alignment_artifact(artifact)
        _validate_alignment_transcript(job.text)
        if not isinstance(correlation_id, str) or not correlation_id.strip() or "\0" in correlation_id:
            raise GenerationRequestError("correlation_id must be a non-empty safe string")
        model = self._get_model(job.model_id)
        async with self._replica_gate, self._worker_pool.acquire(model) as lease:
            self._verify_alignment_artifact(artifact)
            existing = self.get_alignment(job.id)
            if existing is not None and not _alignment_is_retryable(existing):
                return existing
            await lease.load_model(model)
            try:
                _require_alignment_capability(lease.capabilities)
            finally:
                await self._unload_model(lease, model)
            created = self._registry.retry_alignment(job.id) if existing is not None else self._registry.create_alignment(job.id)
            try:
                self._alignment_scheduler.submit(job.id)
            except BaseException:
                self._registry.fail_alignment(job.id, _alignment_error("scheduler_failed", True))
                raise
        return created

    def get_alignment(self, job_id: str) -> AlignmentJob | None:
        self.get(job_id)
        try:
            return self._registry.get_alignment(job_id)
        except GenerationRecordNotFoundError:
            return None

    async def cancel(self, job_id: str) -> GenerationJob:
        try:
            requested = self._registry.request_cancellation(job_id)
        except GenerationRecordNotFoundError as error:
            raise GenerationJobNotFoundError from error
        stream = self._streams.get(job_id)
        if stream is not None:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        return requested

    async def subscribe_pcm(self, job_id: str) -> AsyncIterator[bytes]:
        """Yield validated live PCM for one active job without persisting it."""
        lock = self._pcm_subscription_locks.setdefault(job_id, asyncio.Lock())
        replay: bytes | None = None
        async with lock:
            job = self.get(job_id)
            if job.state in {
                GenerationState.COMPLETED,
                GenerationState.CANCELLED,
                GenerationState.FAILED,
            }:
                replay_buffer = self._pcm_replay.pop(job_id, None)
                self._pcm_replay_disabled.discard(job_id)
                replay = bytes(replay_buffer) if replay_buffer is not None else None
                self._pcm_subscription_locks.pop(job_id, None)
            else:
                queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
                self._pcm_subscribers.setdefault(job_id, set()).add(queue)
        if replay is not None:
            yield replay
            return
        if job.state in {
            GenerationState.COMPLETED,
            GenerationState.CANCELLED,
            GenerationState.FAILED,
        }:
            return
        try:
            while True:
                terminated = queue in self._pcm_terminated.get(job_id, set())
                if terminated and queue.empty():
                    return
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            self._remove_pcm_subscriber(job_id, queue)
            terminated = self._pcm_terminated.get(job_id)
            if terminated is not None:
                terminated.discard(queue)
                if not terminated:
                    self._pcm_terminated.pop(job_id, None)

    def list_history(self) -> tuple[AudioArtifact, ...]:
        return tuple(
            artifact
            for artifact in self._registry.list_history()
            if self._artifact_is_public(artifact)
        )

    def delete_artifact(self, artifact_id: str) -> bool:
        artifact = next((item for item in self.list_history() if item.id == artifact_id), None)
        if artifact is None:
            return False
        try:
            path = self._artifact_path(artifact)
            candidate = self._open_validated_artifact_for_deletion(artifact, path)
        except GenerationArtifactNotFoundError:
            return False
        except UnsafeStoragePathError as error:
            raise GenerationArtifactNotFoundError from error
        except OSError as error:
            if _artifact_error_is_missing_or_unsafe(error):
                return False
            raise GenerationArtifactDeletionError(
                "the artifact could not be validated in managed storage"
            ) from error
        try:
            try:
                payload = candidate.snapshot.read()
            except OSError as error:
                raise GenerationArtifactDeletionError(
                    "the validated artifact snapshot could not be read"
                ) from error
            self._require_current_audio_directory(candidate)
            self._unlink_validated_artifact(candidate)
            try:
                self._require_current_audio_directory(candidate)
                return self._registry.delete_artifact(artifact_id)
            except Exception as database_error:
                try:
                    self._restore_artifact(
                        path,
                        payload,
                        artifact=artifact,
                        directory_fd=candidate.directory_fd,
                    )
                except Exception as restore_error:  # noqa: BLE001 - report both rollback failures
                    raise GenerationArtifactDeletionError(
                        "artifact metadata was retained but its managed file could not be restored"
                    ) from ExceptionGroup(
                        "artifact deletion and rollback both failed",
                        [database_error, restore_error],
                    )
                raise
        finally:
            candidate.close()

    def read_artifact(self, artifact_id: str) -> Path:
        """Validate an artifact and return its managed path for local administration."""
        try:
            artifact = self._get_public_artifact(artifact_id)
            path = self._artifact_path(artifact)
            handle = self._open_validated_artifact(artifact, path)
        except (GenerationArtifactNotFoundError, UnsafeStoragePathError) as error:
            raise GenerationArtifactNotFoundError from error
        handle.close()
        return path

    def open_artifact(self, artifact_id: str):
        """Open an immutable snapshot of a validated artifact for safe client streaming."""
        try:
            artifact = self._get_public_artifact(artifact_id)
            return self._open_validated_artifact(artifact, self._artifact_path(artifact))
        except (GenerationArtifactNotFoundError, UnsafeStoragePathError) as error:
            raise GenerationArtifactNotFoundError from error

    def _get_public_artifact(self, artifact_id: str) -> AudioArtifact:
        artifact = next((item for item in self.list_history() if item.id == artifact_id), None)
        if artifact is None:
            raise GenerationArtifactNotFoundError
        return artifact

    @staticmethod
    def _hash_handle(handle: Any) -> str:
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        handle.seek(0)
        return digest.hexdigest()

    def _open_validated_artifact(self, artifact: AudioArtifact, path: Path):
        if (
            not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"))
            or os.open not in getattr(os, "supports_dir_fd", ())
            or os.stat not in getattr(os, "supports_dir_fd", ())
        ):
            raise GenerationArtifactNotFoundError
        directory = self._layout.checked_directory("audio")
        name = path.name
        directory_fd: int | None = None
        file_fd: int | None = None
        try:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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
                or path_metadata.st_dev != file_metadata.st_dev
                or path_metadata.st_ino != file_metadata.st_ino
                or file_metadata.st_size != artifact.byte_size
            ):
                raise GenerationArtifactNotFoundError
            source = os.fdopen(file_fd, "rb", closefd=False)
            try:
                snapshot = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - returned snapshot owns its lifecycle
                    max_size=8 * 1024 * 1024, mode="w+b"
                )
                digest = hashlib.sha256()
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    snapshot.write(chunk)
                final_metadata = os.fstat(file_fd)
                if (
                    final_metadata.st_dev != file_metadata.st_dev
                    or final_metadata.st_ino != file_metadata.st_ino
                    or final_metadata.st_size != artifact.byte_size
                    or digest.hexdigest() != artifact.sha256
                ):
                    raise GenerationArtifactNotFoundError
                snapshot.seek(0)
                return snapshot
            except Exception:
                if "snapshot" in locals():
                    snapshot.close()
                raise
            finally:
                source.close()
        except GenerationArtifactNotFoundError:
            raise
        except OSError as error:
            if _artifact_error_is_missing_or_unsafe(error):
                raise GenerationArtifactNotFoundError from error
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def _open_validated_artifact_for_deletion(
        self,
        artifact: AudioArtifact,
        path: Path,
    ) -> _ValidatedArtifactDeletion:
        if (
            not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"))
            or os.open not in getattr(os, "supports_dir_fd", ())
            or os.stat not in getattr(os, "supports_dir_fd", ())
        ):
            raise GenerationArtifactDeletionError(
                "identity-bound artifact deletion is unavailable on this platform"
            )
        root_fd: int | None = None
        directory_fd: int | None = None
        file_fd: int | None = None
        try:
            self._layout.checked_directory("audio")
            root_fd = os.open(
                self._layout.root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            directory_fd = os.open(
                "audio",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            path_metadata = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            file_metadata = os.fstat(file_fd)
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or not stat.S_ISREG(file_metadata.st_mode)
                or path_metadata.st_dev != file_metadata.st_dev
                or path_metadata.st_ino != file_metadata.st_ino
                or file_metadata.st_size != artifact.byte_size
            ):
                raise GenerationArtifactNotFoundError
            source = os.fdopen(file_fd, "rb", closefd=False)
            snapshot = None
            try:
                snapshot = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - candidate owns lifecycle
                    max_size=8 * 1024 * 1024, mode="w+b"
                )
                digest = hashlib.sha256()
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    snapshot.write(chunk)
                final_metadata = os.fstat(file_fd)
                if (
                    final_metadata.st_dev != file_metadata.st_dev
                    or final_metadata.st_ino != file_metadata.st_ino
                    or final_metadata.st_size != artifact.byte_size
                    or digest.hexdigest() != artifact.sha256
                ):
                    raise GenerationArtifactNotFoundError
                snapshot.seek(0)
            except Exception:
                if snapshot is not None:
                    snapshot.close()
                raise
            finally:
                source.close()
            root_metadata = os.fstat(root_fd)
            directory_metadata = os.fstat(directory_fd)
            candidate = _ValidatedArtifactDeletion(
                root_fd=root_fd,
                root_identity=(root_metadata.st_dev, root_metadata.st_ino),
                directory_fd=directory_fd,
                directory_identity=(directory_metadata.st_dev, directory_metadata.st_ino),
                file_fd=file_fd,
                name=path.name,
                snapshot=snapshot,
            )
            root_fd = None
            directory_fd = None
            file_fd = None
            return candidate
        except GenerationArtifactNotFoundError:
            raise
        except OSError as error:
            if _artifact_error_is_missing_or_unsafe(error):
                raise GenerationArtifactNotFoundError from error
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)
            if root_fd is not None:
                os.close(root_fd)

    def _require_current_audio_directory(
        self, candidate: _ValidatedArtifactDeletion
    ) -> None:
        try:
            root_metadata = os.stat(self._layout.root, follow_symlinks=False)
            audio_metadata = os.stat(self._layout.audio, follow_symlinks=False)
        except OSError as error:
            raise GenerationArtifactDeletionError(
                "the managed audio directory changed before artifact deletion completed"
            ) from error
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or (root_metadata.st_dev, root_metadata.st_ino) != candidate.root_identity
            or not stat.S_ISDIR(audio_metadata.st_mode)
            or (audio_metadata.st_dev, audio_metadata.st_ino) != candidate.directory_identity
        ):
            raise GenerationArtifactDeletionError(
                "the managed audio directory changed before artifact deletion completed"
            )

    @staticmethod
    def _unlink_validated_artifact(candidate: _ValidatedArtifactDeletion) -> None:
        try:
            unlink_open_file(candidate.directory_fd, candidate.name, candidate.file_fd)
        except IdentityBoundUnlinkMismatchError as error:
            raise GenerationArtifactNotFoundError from error
        except IdentityBoundUnlinkUnavailableError as error:
            raise GenerationArtifactDeletionError(
                "identity-bound artifact deletion is unavailable on this platform"
            ) from error
        except IdentityBoundUnlinkError as error:
            raise GenerationArtifactDeletionError(
                "the validated artifact could not be removed from managed storage"
            ) from error

    def _restore_artifact(
        self,
        path: Path,
        payload: bytes,
        *,
        artifact: AudioArtifact | None = None,
        directory_fd: int | None = None,
    ) -> None:
        if (
            not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"))
            or os.open not in getattr(os, "supports_dir_fd", ())
            or os.link not in getattr(os, "supports_dir_fd", ())
        ):
            raise GenerationArtifactDeletionError("safe artifact restoration is unavailable")
        owns_directory_fd = directory_fd is None
        if directory_fd is None:
            directory_fd = os.open(
                self._layout.checked_directory("audio"),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        staging_name: str | None = None
        staging_fd: int | None = None
        try:
            staging_name = f".{path.name}.restore-{uuid4().hex}.tmp"
            staging_fd = os.open(
                staging_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(staging_fd, view[offset:])
                if written <= 0:
                    raise OSError("artifact restoration made no write progress")
                offset += written
            os.fsync(staging_fd)
            metadata = os.fstat(staging_fd)
            expected_size = artifact.byte_size if artifact is not None else len(payload)
            expected_sha256 = artifact.sha256 if artifact is not None else hashlib.sha256(payload).hexdigest()
            if metadata.st_size != expected_size:
                raise OSError("restored artifact size does not match metadata")
            os.lseek(staging_fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while chunk := os.read(staging_fd, 1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise OSError("restored artifact checksum does not match metadata")
            os.link(
                staging_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        finally:
            try:
                if staging_fd is not None:
                    os.close(staging_fd)
            finally:
                try:
                    if staging_name is not None:
                        try:
                            os.unlink(staging_name, dir_fd=directory_fd)
                        except FileNotFoundError:
                            pass
                finally:
                    if owns_directory_fd:
                        os.close(directory_fd)

    async def list_voices(self, model_id: str) -> tuple[engine_pb2.PresetVoice, ...]:
        model = self._get_model(model_id)
        async with self._replica_gate, self._worker_pool.acquire(model) as lease:
            _require_preset_capability(lease)
            await lease.load_model(model)
            try:
                _require_preset_capability(lease)
                return await lease.list_voices()
            finally:
                await self._unload_model(lease, model)

    async def preview(self, *, model_id: str, voice_id: str, text: str) -> bytes:
        """Synthesize a short voice preview without creating durable state."""
        _validate_text(text)
        _validate_source(voice_id, None)
        model = self._get_model(model_id)
        provider_id = model_id.removeprefix("provider:") if model_id.startswith("provider:") else None
        if provider_id is not None:
            if self._provider_registry is None:
                raise GenerationModelNotFoundError from None
            try:
                self._provider_registry.get(provider_id)
            except ProviderNotFoundError as error:
                raise GenerationModelNotFoundError from error

        async with self._replica_gate, self._worker_pool.acquire(model) as lease:
            _require_streaming_capability(lease)
            _require_preset_capability(lease)
            stream: AsyncIterator[engine_pb2.SynthesisEvent] | None = None
            loaded = False
            try:
                await lease.load_model(model)
                loaded = True
                _require_preset_capability(lease)
                voices = await lease.list_voices()
                if not any(voice.id == voice_id for voice in voices):
                    raise GenerationVoiceNotFoundError("the requested voice is not available")
                stream = lease.synthesize(
                    build_synthesis_request(
                        model.id,
                        text,
                        voice_id=voice_id,
                        provider_config=_provider_config(self._provider_registry, provider_id)
                        if provider_id is not None else None,
                    )
                )
                pcm, result = await _consume_preview_stream(stream)
                output = io.BytesIO()
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(result.format.channels)
                    wav.setsampwidth(result.format.sample_width)
                    wav.setframerate(result.format.sample_rate)
                    wav.writeframes(pcm)
                return output.getvalue()
            finally:
                original_error = sys.exc_info()[1]
                close_error: BaseException | None = None
                try:
                    if stream is not None:
                        close = getattr(stream, "aclose", None)
                        if close is not None:
                            try:
                                await close()
                            except BaseException as error:  # noqa: BLE001 - preserve first cleanup failure
                                close_error = error
                                if original_error is not None:
                                    close_error = None
                finally:
                    if loaded:
                        try:
                            await self._unload_model(lease, model)
                        except BaseException:
                            if original_error is None and close_error is None:
                                raise
                if close_error is not None:
                    raise close_error

    def has_active_job_for_model(self, model_id: str) -> bool:
        for job in self._registry.list_jobs():
            if job.model_id != model_id:
                continue
            if job.state not in {GenerationState.COMPLETED, GenerationState.CANCELLED, GenerationState.FAILED}:
                return True
            alignment = self.get_alignment(job.id)
            if alignment is not None and alignment.state in {AlignmentState.QUEUED, AlignmentState.RUNNING}:
                return True
        return False

    async def recover(self, *, resume_alignments: bool = True) -> None:
        self._remove_staging_files()
        for job in self._registry.list_jobs():
            if job.reference_id is not None and job.state not in {
                GenerationState.COMPLETED,
                GenerationState.CANCELLED,
                GenerationState.FAILED,
            }:
                try:
                    if self._reference_service is None:
                        recovered = self._registry.mark_recovery_failure(
                            job.id,
                            {
                                "code": "reference_recovery_required",
                                "message": "Reference handoff was lost; generation requires recovery.",
                                "retryable": True,
                            },
                        )
                        self._publish(
                            recovered,
                            "generation.failed",
                            "Reference handoff was lost; generation requires recovery.",
                            error=recovered.error,
                        )
                        continue
                    self._reference_service.release_for_recovery(job.reference_id)
                    recovered = self._registry.mark_recovery_failure(
                        job.id,
                        {
                            "code": "reference_recovery_required",
                            "message": "Reference handoff was lost; generation requires recovery.",
                            "retryable": True,
                        },
                    )
                    self._publish(
                        recovered,
                        "generation.failed",
                        "Reference handoff was lost; generation requires recovery.",
                        error=recovered.error,
                    )
                    self._registry.clear_reference_id(job.id)
                    continue
                except CleanupFailedError:
                    recovered = self._registry.mark_recovery_failure(
                        job.id,
                        {
                            "code": "cleanup_failed",
                            "message": "Generation cleanup failed; restart Core to retry cleanup.",
                            "retryable": True,
                        },
                    )
                    self._publish(
                        recovered,
                        "generation.failed",
                        "Generation cleanup failed; restart Core to retry cleanup.",
                        error=recovered.error,
                    )
                    continue
            if job.state is GenerationState.FAILED and job.error and job.error.get("code") == "cleanup_failed":
                cleanup_still_failed = False
                try:
                    self._remove_job_artifact(job)
                except Exception:  # noqa: BLE001 - keep unsafe cleanup visible for retry
                    cleanup_still_failed = True
                if cleanup_still_failed:
                    continue
            if job.state in {
                GenerationState.LOADING,
                GenerationState.GENERATING,
                GenerationState.FINALIZING,
            }:
                try:
                    self._remove_job_artifact(job)
                except Exception:  # noqa: BLE001 - make recovery cleanup retryable
                    recovered = self._registry.mark_recovery_failure(
                        job.id,
                        {
                            "code": "cleanup_failed",
                            "message": "Generation cleanup failed; restart Core to retry cleanup.",
                            "retryable": True,
                        },
                    )
                    self._publish(
                        recovered,
                        "generation.failed",
                        "Generation cleanup failed; restart Core to retry cleanup.",
                        error=recovered.error,
                    )
                    continue
                recovered = self._registry.mark_recovery_failure(
                    job.id,
                    {
                        "code": "recovery_required",
                        "message": "Generation was interrupted and requires retry.",
                        "retryable": True,
                    },
                )
                self._publish(
                    recovered,
                    "generation.failed",
                    "An interrupted generation requires retry.",
                    error=recovered.error,
                )
        for job in self._registry.list_jobs():
            if job.state is GenerationState.QUEUED:
                self._scheduler.submit(job.id)
        if resume_alignments:
            self.resume_alignments()

    def resume_alignments(self) -> None:
        for job in self._registry.list_jobs():
            try:
                alignment = self.get_alignment(job.id)
            except (InvalidRegistryDataError, TypeError, ValueError, UnicodeError):
                try:
                    self._registry.recover_alignment(job.id)
                except GenerationRecordNotFoundError:
                    pass
                continue
            if alignment is None:
                continue
            if alignment.state is AlignmentState.RUNNING:
                self._registry.fail_alignment(job.id, _alignment_error("alignment_recovery_required", True))
            elif alignment.state is AlignmentState.QUEUED:
                self._alignment_scheduler.submit(job.id)

    async def close(self) -> None:
        self._closing = True
        await self._scheduler.close()
        await self._alignment_scheduler.close()
        for job in self._registry.list_jobs():
            try:
                alignment = self.get_alignment(job.id)
            except (InvalidRegistryDataError, TypeError, ValueError, UnicodeError):
                self._registry.recover_alignment(job.id)
                continue
            if alignment is not None and alignment.state in {AlignmentState.QUEUED, AlignmentState.RUNNING}:
                self._registry.fail_alignment(job.id, _alignment_error("alignment_cancelled", True))
        for job_id, handle in tuple(self._reference_handles.items()):
            self._reference_handles.pop(job_id, None)
            try:
                job = self.get(job_id)
            except GenerationJobNotFoundError:
                continue
            if job.state in {GenerationState.COMPLETED, GenerationState.CANCELLED, GenerationState.FAILED}:
                continue
            cleanup_succeeded = self._release_reference(handle)
            code = "reference_recovery_required" if cleanup_succeeded else "cleanup_failed"
            message = (
                "Reference handoff was lost; generation requires recovery."
                if cleanup_succeeded
                else "Generation cleanup failed; restart Core to retry cleanup."
            )
            self._fail_job(job, code, message, True)
            if cleanup_succeeded:
                try:
                    self._registry.clear_reference_id(job_id)
                except GenerationRecordNotFoundError:
                    pass
        self._pcm_replay.clear()
        self._pcm_replay_disabled.clear()

    async def _run_alignment(self, job_id: str) -> None:
        async with self._replica_gate:
            loaded_lease: Any | None = None
            loaded_model: ModelInstallation | None = None
            artifact: AudioArtifact | None = None
            completed = False
            failure: tuple[str, bool] | None = None
            try:
                job = self.get(job_id)
                artifact = self._alignment_artifact(job)
                self._verify_alignment_artifact(artifact)
                model = self._get_model(job.model_id)
                self._registry.start_alignment(job.id)
                async with self._worker_pool.acquire(model) as lease:
                    await lease.load_model(model)
                    loaded_lease, loaded_model = lease, model
                    capability = _require_alignment_capability(lease.capabilities)
                    snapshot = self._create_alignment_snapshot(artifact)
                    try:
                        response = await lease.align(engine_pb2.AlignRequest(model_id=model.id, audio_path=snapshot[1], transcript=job.text))
                    finally:
                        snapshot[0].unlink(missing_ok=True)
                    result = _alignment_result_from_worker(response, job=job, artifact=artifact, capability=capability)
                    self._verify_alignment_artifact(artifact)
                    self._registry.complete_alignment(job.id, result)
                    completed = True
                    await self._unload_model(lease, model)
                    loaded_lease = loaded_model = None
            except asyncio.CancelledError:
                failure = ("alignment_cancelled", True)
            except GenerationCapabilityError:
                failure = ("alignment_unavailable", False)
            except GenerationArtifactNotFoundError:
                failure = ("artifact_not_found", False)
            except GenerationArtifactInvalidError:
                failure = ("artifact_invalid", False)
            except WorkerOperationError as error:
                failure = (error.code or "worker_failed", error.retryable)
            except (InvalidRegistryDataError, _MalformedAlignment):
                failure = ("malformed_alignment", False)
            except TimeoutError:
                failure = ("alignment_deadline_exceeded", True)
            except Exception as error:  # noqa: BLE001
                failure = _alignment_exception(error)
            finally:
                if loaded_lease is not None and loaded_model is not None:
                    try:
                        await self._unload_model(loaded_lease, loaded_model)
                    except Exception:  # noqa: BLE001 - cleanup failure is normalized below
                        if not completed:
                            failure = ("worker_failed", True)
                if artifact is not None:
                    try:
                        self._verify_alignment_artifact(artifact)
                    except GenerationArtifactNotFoundError:
                        if not completed:
                            failure = ("artifact_not_found", False)
                    except GenerationArtifactInvalidError:
                        if not completed:
                            failure = ("artifact_invalid", False)
                if not completed and failure is not None:
                    try:
                        current = self.get_alignment(job_id)
                    except GenerationJobNotFoundError:
                        current = None
                    if current is not None and current.state in {AlignmentState.QUEUED, AlignmentState.RUNNING}:
                        self._registry.fail_alignment(job_id, _alignment_error(*failure))

    async def _run_job(self, job_id: str) -> None:
        async with self._replica_gate:
            writer: WavArtifactWriter | None = None
            stream: AsyncIterator[engine_pb2.SynthesisEvent] | None = None
            loaded_lease: Any | None = None
            loaded_model: ModelInstallation | None = None
            published_artifact_id: str | None = None
            completion_committed = False
            cancellation_pending = False
            failure: tuple[str, str, bool] | None = None
            try:
                job = self.get(job_id)
                if job.cancellation_requested:
                    cancellation_pending = True
                    return
                model = self._get_model(job.model_id)
                reference_handle = self._reference_handles.get(job.id)
                if job.saved_voice_id is not None and reference_handle is None:
                    if self._saved_voice_service is None:
                        raise _GenerationFailure(
                            "saved_voice_recovery_required",
                            "Saved Voice service is not configured.",
                            retryable=True,
                        )
                    try:
                        saved_voice = self._saved_voice_service.get(job.saved_voice_id)
                    except Exception as error:
                        raise _GenerationFailure(
                            "saved_voice_not_found",
                            "The saved Voice no longer exists.",
                            retryable=False,
                        ) from error
                    if saved_voice.model_id != model.id:
                        raise _GenerationFailure(
                            "saved_voice_model_mismatch",
                            "The saved Voice is pinned to a different model.",
                            retryable=False,
                        )
                    reference_handle = _ReferenceHandle(
                        saved_voice.id, saved_voice.relative_path, saved_voice.transcript, False
                    )
                if job.reference_id is not None and reference_handle is None:
                    raise _GenerationFailure(
                        "reference_recovery_required",
                        "Reference handoff was lost; generation requires recovery.",
                        retryable=True,
                    )
                async with self._worker_pool.acquire(model) as lease:
                    if not lease.capabilities.supports("streaming_synthesis"):
                        raise _GenerationFailure(
                            "capability_unsupported",
                            "The Worker does not support streaming synthesis.",
                            retryable=True,
                        )
                    loading = self._transition(job, GenerationState.LOADING, "Loading the model.")
                    await lease.load_model(model)
                    loaded_lease = lease
                    loaded_model = model
                    generating = self._transition(loading, GenerationState.GENERATING, "Generating audio.")
                    writer = WavArtifactWriter(self._layout, _artifact_id(job.id))
                    if job.reference_id is None and job.saved_voice_id is None and not lease.capabilities.supports("preset_voices"):
                        raise _GenerationFailure(
                            "capability_unsupported",
                            "The Worker does not support preset voices.",
                            retryable=True,
                        )
                    options = job.options
                    _require_option_capabilities(lease, options)
                    stream = lease.synthesize(
                        build_synthesis_request(
                            model.id,
                            job.text,
                            voice_id=job.voice_id,
                            reference_path=reference_handle.relative_path if reference_handle else None,
                            transcript=reference_handle.transcript if reference_handle else None,
                            provider_config=_provider_config(self._provider_registry, job.provider_id)
                            if job.provider_id is not None else None,
                            speed=options.speed if options else None,
                            pitch=options.pitch if options else None,
                            volume=options.volume if options else None,
                        )
                    )
                    self._streams[job.id] = stream
                    result = await self._consume_stream(job, stream, writer)
                    finalizing = self._transition(
                        generating,
                        GenerationState.FINALIZING,
                        "Finalizing the WAV artifact.",
                        bytes_written=result.byte_count,
                        frame_count=result.frame_count,
                        sample_rate=result.format.sample_rate,
                        channel_count=result.format.channels,
                    )
                    artifact_id = _artifact_id(job.id)
                    if job.retain_artifact:
                        path = writer.finalize(byte_count=result.byte_count, frame_count=result.frame_count)
                        published_artifact_id = artifact_id
                        self._registry.create_artifact(
                            job_id=job.id,
                            artifact_id=artifact_id,
                            path=f"audio/{path.name}",
                            byte_size=path.stat().st_size,
                            sha256=_sha256(path),
                            sample_rate=result.format.sample_rate,
                            channel_count=result.format.channels,
                            frame_count=result.frame_count,
                            duration_ms=result.duration_ms,
                        )
                    else:
                        writer.abort()
                    if reference_handle is not None:
                        self._reference_handles.pop(job.id, None)
                        if not self._release_reference(reference_handle):
                            raise _GenerationFailure(
                                "cleanup_failed",
                                "Generation cleanup failed; restart Core to retry cleanup.",
                                retryable=True,
                            )
                        self._registry.clear_reference_id(job.id)
                    completed = self._registry.transition_job(
                        finalizing.id,
                        GenerationState.COMPLETED,
                        bytes_written=result.byte_count,
                        frame_count=result.frame_count,
                    )
                    completion_committed = True
                    self._publish(completed, "generation.completed", "Generation completed.")
                    await self._unload_model(lease, model)
                    loaded_lease = None
                    loaded_model = None
            except asyncio.CancelledError:
                current = self.get(job_id)
                if not self._closing and current.cancellation_requested and current.state in {
                    GenerationState.QUEUED,
                    GenerationState.LOADING,
                    GenerationState.GENERATING,
                }:
                    cancellation_pending = True
                else:
                    raise
            except _GenerationCancelled:
                cancellation_pending = True
            except InvalidGenerationTransitionError:
                current = self.get(job_id)
                if not self._closing and current.cancellation_requested and current.state in {
                    GenerationState.QUEUED,
                    GenerationState.LOADING,
                    GenerationState.GENERATING,
                }:
                    cancellation_pending = True
                else:
                    failure = ("generation_failed", "Generation state could not advance.", False)
            except _GenerationFailure as error:
                failure = (error.code, error.message, False)
            except GenerationCapabilityError as error:
                failure = ("capability_unsupported", str(error), False)
            except WorkerOperationError as error:
                failure = (
                    error.code or "worker_failed",
                    "The engine Worker failed during generation.",
                    False,
                )
            except Exception:  # noqa: BLE001 - durable job boundary normalizes Worker/filesystem faults
                current = self.get(job_id)
                if not self._closing and current.cancellation_requested and current.state in {
                    GenerationState.QUEUED,
                    GenerationState.LOADING,
                    GenerationState.GENERATING,
                }:
                    cancellation_pending = True
                elif current.state not in {GenerationState.COMPLETED, GenerationState.CANCELLED, GenerationState.FAILED}:
                    failure = ("generation_failed", "Generation failed.", False)
            finally:
                self._streams.pop(job_id, None)
                cleanup_failed = False
                if stream is not None:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except Exception:  # noqa: BLE001 - expose cleanup failure durably
                            cleanup_failed = True
                if loaded_lease is not None and loaded_model is not None:
                    try:
                        await self._unload_model(loaded_lease, loaded_model)
                    except Exception:  # noqa: BLE001 - expose lifecycle cleanup failure durably
                        cleanup_failed = True
                if writer is not None:
                    try:
                        if not completion_committed:
                            writer.discard_published()
                        writer.abort()
                        if not completion_committed and published_artifact_id is not None:
                            self._registry.delete_artifact(published_artifact_id)
                    except Exception:  # noqa: BLE001 - never report cancellation as clean
                        cleanup_failed = True
                reference_handle = (
                    self._reference_handles.get(job_id)
                    if self._closing
                    else self._reference_handles.pop(job_id, None)
                )
                if reference_handle is not None and not self._closing:
                    try:
                        if not self._release_reference(reference_handle):
                            cleanup_failed = True
                        else:
                            self._registry.clear_reference_id(job_id)
                    except Exception:  # noqa: BLE001 - expose cleanup failure durably
                        cleanup_failed = True
                if cleanup_failed:
                    failure = ("cleanup_failed", "Generation cleanup failed; restart Core to retry cleanup.", False)
                if failure is not None:
                    self._fail_job(self.get(job_id), *failure)
                elif cancellation_pending:
                    self._cancel_job(self.get(job_id))
                final_job = self.get(job_id)
                if final_job.state is not GenerationState.COMPLETED or final_job.retain_artifact:
                    self._pcm_replay.pop(job_id, None)
                    self._pcm_replay_disabled.discard(job_id)
                subscription_lock = self._pcm_subscription_locks.setdefault(
                    job_id, asyncio.Lock()
                )
                async with subscription_lock:
                    subscribers = tuple(self._pcm_subscribers.pop(job_id, set()))
                    self._pcm_subscription_locks.pop(job_id, None)
                    for subscriber in subscribers:
                        self._terminate_pcm_subscriber(job_id, subscriber)

    async def _consume_stream(
        self,
        job: GenerationJob,
        stream: AsyncIterator[engine_pb2.SynthesisEvent],
        writer: WavArtifactWriter,
    ) -> _StreamResult:
        validator = PcmValidator()
        queue = ByteBudgetQueue(self._queue_bytes)
        result_event: engine_pb2.SynthesisResult | None = None
        duration_ms: int | None = None

        async def produce() -> None:
            nonlocal result_event, duration_ms
            async for event in stream:
                current = self.get(job.id)
                if current.cancellation_requested:
                    raise _GenerationCancelled
                payload = event.WhichOneof("payload")
                if result_event is not None:
                    raise _GenerationFailure("malformed_pcm", "The Worker sent an event after its terminal result.")
                if validator.audio_format is None and payload not in {"header", "error"}:
                    raise _GenerationFailure("malformed_pcm", "The Worker must send the audio header first.")
                if payload == "header":
                    try:
                        validator.accept_header(event.header)
                    except AudioValidationError as error:
                        raise _GenerationFailure("malformed_pcm", str(error)) from error
                elif payload == "chunk":
                    try:
                        validator.accept_chunk(event.chunk)
                    except AudioValidationError as error:
                        raise _GenerationFailure("malformed_pcm", str(error)) from error
                    await queue.put(event.chunk.pcm)
                    payload = bytes(event.chunk.pcm)
                    if (
                        not job.retain_artifact
                        and not self._pcm_subscribers.get(job.id)
                        and job.id not in self._pcm_replay_disabled
                    ):
                        replay = self._pcm_replay.setdefault(job.id, bytearray())
                        if len(replay) + len(payload) <= _EPHEMERAL_REPLAY_MAX_BYTES:
                            replay.extend(payload)
                        else:
                            self._pcm_replay.pop(job.id, None)
                            self._pcm_replay_disabled.add(job.id)
                    for subscriber in tuple(self._pcm_subscribers.get(job.id, ())):
                        try:
                            subscriber.put_nowait(payload)
                        except (asyncio.QueueFull, RuntimeError, *_PCM_QUEUE_CLOSED_ERRORS):
                            self._terminate_pcm_subscriber(job.id, subscriber)
                elif payload == "progress":
                    self._publish(
                        current,
                        "generation.progress",
                        "Generation is in progress.",
                        extra={"bytes_written": validator.byte_count},
                    )
                elif payload == "result":
                    result_event = event.result
                    duration_ms = event.result.duration_ms or None
                elif payload == "error":
                    raise _GenerationFailure(
                        event.error.code or "worker_failed",
                        "The engine Worker failed during synthesis.",
                        retryable=False,
                    )
                else:
                    raise _GenerationFailure("malformed_pcm", "The Worker sent an unknown synthesis event.")
            await queue.put(ByteBudgetQueueSignal.END)

        async def consume() -> None:
            while True:
                payload = await queue.get()
                if payload is ByteBudgetQueueSignal.END:
                    return
                writer.write(payload)

        producer = asyncio.create_task(produce(), name=f"generation-producer-{job.id}")
        consumer = asyncio.create_task(consume(), name=f"generation-consumer-{job.id}")
        try:
            await asyncio.gather(producer, consumer)
        except BaseException:
            producer.cancel()
            consumer.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            raise
        if result_event is None:
            raise _GenerationFailure("malformed_pcm", "The Worker ended without a synthesis result.")
        try:
            pcm_result = validator.finish(result_event)
        except AudioValidationError as error:
            raise _GenerationFailure("malformed_pcm", str(error)) from error
        return _StreamResult(pcm_result, duration_ms)

    def _remove_pcm_subscriber(self, job_id: str, subscriber: asyncio.Queue[bytes | None]) -> None:
        subscribers = self._pcm_subscribers.get(job_id)
        if subscribers is None:
            return
        subscribers.discard(subscriber)
        if not subscribers:
            self._pcm_subscribers.pop(job_id, None)

    def _terminate_pcm_subscriber(
        self, job_id: str, subscriber: asyncio.Queue[bytes | None]
    ) -> None:
        self._remove_pcm_subscriber(job_id, subscriber)
        self._pcm_terminated.setdefault(job_id, set()).add(subscriber)
        try:
            subscriber.put_nowait(None)
        except asyncio.QueueFull:
            return
        except (RuntimeError, *_PCM_QUEUE_CLOSED_ERRORS):
            self._pcm_terminated[job_id].discard(subscriber)

    def _transition(self, job: GenerationJob, state: GenerationState, message: str, **kwargs: Any) -> GenerationJob:
        updated = self._registry.transition_job(job.id, state, **kwargs)
        event_type = {
            GenerationState.LOADING: "generation.loading",
            GenerationState.FINALIZING: "generation.finalizing",
        }.get(state, "generation.progress")
        self._publish(updated, event_type, message)
        return updated

    def _cancel_job(self, job: GenerationJob) -> GenerationJob:
        if job.state in {GenerationState.COMPLETED, GenerationState.CANCELLED, GenerationState.FAILED}:
            return job
        cancelled = self._registry.transition_job(
            job.id,
            GenerationState.CANCELLED,
            error={
                "code": "generation_cancelled",
                "message": "Generation was cancelled.",
                "retryable": False,
            },
        )
        self._publish(cancelled, "generation.cancelled", "Generation was cancelled.", error=cancelled.error)
        return cancelled

    def _fail_job(self, job: GenerationJob, code: str, message: str, retryable: bool) -> None:
        if job.state in {GenerationState.COMPLETED, GenerationState.CANCELLED, GenerationState.FAILED}:
            return
        failed = self._registry.transition_job(
            job.id,
            GenerationState.FAILED,
            error={"code": code, "message": message, "retryable": retryable},
        )
        self._publish(failed, "generation.failed", "Generation failed.", error=failed.error)

    def _publish(
        self,
        job: GenerationJob,
        event_type: str,
        message: str,
        *,
        error: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "job_id": job.id,
            "model_id": job.model_id,
            "engine_id": job.engine_id,
            "voice_id": job.voice_id,
            "state": job.state.value,
            "bytes_written": job.bytes_written,
            "frame_count": job.frame_count,
            "message": message,
        }
        if error is not None:
            payload["error"] = error
        if extra is not None:
            payload.update(extra)
        self._event_store.append(
            event_type,
            payload,
            stream_kind="generation",
            stream_id=job.id,
        )

    def _get_model(self, model_id: str) -> ModelInstallation:
        if model_id.startswith("provider:"):
            provider_id = model_id.removeprefix("provider:")
            if self._provider_registry is None:
                raise GenerationModelNotFoundError
            try:
                self._provider_registry.get(provider_id)
            except ProviderNotFoundError as error:
                raise GenerationModelNotFoundError from error
            return ModelInstallation(
                id=model_id, repository_id=model_id, requested_revision=None,
                resolved_commit="remote", engine_installation_id="openai_compatible@remote",
                compatibility_evidence={"engine_id": "openai_compatible"}, runtime_variant="remote",
                manifest={}, checksum_summary={}, byte_size=0, cache_path="models/remote",
                desired_load_state="loaded", observed_load_state="loaded", replica_summary={},
                last_error=None, created_at="", updated_at="",
            )
        try:
            return self._model_registry.get_model(model_id)
        except RegistryRecordNotFoundError as error:
            raise GenerationModelNotFoundError from error

    def _release_reference(self, handle: _ReferenceHandle | None) -> bool:
        if handle is None or not handle.temporary:
            return True
        if self._reference_service is None:
            return False
        try:
            self._reference_service.release_terminal(
                ReferenceHandle(
                    recording=self._reference_service.get(handle.reference_id),
                    transcript=handle.transcript,
                )
            )
        except CleanupFailedError:
            return False
        except ReferenceRecordNotFoundError:
            return True
        return True

    def _alignment_artifact(self, job: GenerationJob) -> AudioArtifact:
        if job.state is not GenerationState.COMPLETED:
            raise GenerationRequestError("alignment requires a completed Generation Job")
        if not job.retain_artifact or job.artifact_id is None:
            raise GenerationRequestError("alignment requires a retained Audio Artifact")
        artifact = next((item for item in self._registry.list_history() if item.id == job.artifact_id and item.job_id == job.id), None)
        if artifact is None:
            raise GenerationRequestError("alignment requires a retained Audio Artifact")
        return artifact

    def _verify_alignment_artifact(self, artifact: AudioArtifact) -> Path:
        path = self._artifact_path(artifact)
        try:
            path.lstat()
        except FileNotFoundError as error:
            raise GenerationArtifactNotFoundError from error
        try:
            snapshot = self._open_validated_artifact(artifact, path)
        except GenerationArtifactNotFoundError as error:
            raise GenerationArtifactInvalidError from error
        try:
            snapshot.read(1)
        finally:
            snapshot.close()
        return self._artifact_path(artifact)

    def _create_alignment_snapshot(self, artifact: AudioArtifact) -> tuple[Path, str]:
        source = self._open_validated_artifact(artifact, self._artifact_path(artifact))
        staging = self._layout.checked_directory("staging")
        name = f".alignment-{uuid4().hex}.wav"
        path = staging / name
        try:
            payload = source.read()
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("alignment snapshot write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
            finally:
                os.close(descriptor)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            source.close()
        return path, f"staging/{name}"

    def _artifact_path(self, artifact: AudioArtifact) -> Path:
        relative = PurePosixPath(artifact.path)
        if len(relative.parts) != 2 or relative.parts[0] != "audio":
            raise UnsafeStoragePathError("artifact path is not a managed audio path")
        return self._layout.managed_child("audio", relative.parts[1])

    def _artifact_is_public(self, artifact: AudioArtifact) -> bool:
        try:
            job = self._registry.get_job(artifact.job_id)
        except GenerationRecordNotFoundError:
            return False
        return job.state is GenerationState.COMPLETED

    async def _unload_model(self, lease: Any, model: ModelInstallation) -> None:
        try:
            await lease.unload_model(model)
        except WorkerOperationError as error:
            if error.code != "model_not_loaded":
                raise

    def _remove_staging_files(self) -> None:
        directories: tuple[tuple[ManagedDirectoryName, tuple[str, ...]], ...] = (
            ("staging", (".pcm",)),
            ("audio", (".wav",)),
        )
        for directory_name, suffixes in directories:
            directory = self._layout.checked_directory(directory_name)
            for path in directory.iterdir():
                generation_temporary = (
                    path.name.startswith(".generation-")
                    and path.suffix in suffixes
                )
                restore_temporary = (
                    directory_name == "audio"
                    and path.name.startswith(".generation-")
                    and ".wav.restore-" in path.name
                    and path.suffix == ".tmp"
                )
                alignment_snapshot = (
                    directory_name == "staging"
                    and path.name.startswith(".alignment-")
                    and path.suffix == ".wav"
                )
                if (
                    (generation_temporary or restore_temporary or alignment_snapshot)
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    path.unlink()

    def _remove_job_artifact(self, job: GenerationJob) -> None:
        if job.artifact_id is not None:
            artifact = next(
                (item for item in self._registry.list_history() if item.id == job.artifact_id),
                None,
            )
            if artifact is not None:
                self._remove_regular_file(self._artifact_path(artifact))
                self._registry.delete_artifact(artifact.id)
                return
        fallback = self._layout.managed_child("audio", f"{_artifact_id(job.id)}.wav")
        try:
            fallback.lstat()
        except FileNotFoundError:
            return
        raise UnsafeStoragePathError("cannot remove recovered WAV without publication identity")

    @staticmethod
    def _remove_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if path.is_symlink() or not path.is_file():
            return
        del metadata
        path.unlink()


class _StreamResult:
    def __init__(self, pcm: PcmResult, duration_ms: int | None) -> None:
        self.format = pcm.format
        self.byte_count = pcm.byte_count
        self.frame_count = pcm.frame_count
        self.duration_ms = duration_ms


async def _consume_preview_stream(
    stream: AsyncIterator[engine_pb2.SynthesisEvent],
) -> tuple[bytes, PcmResult]:
    validator = PcmValidator(max_bytes=_PREVIEW_MAX_PCM_BYTES)
    pcm = bytearray()
    result_event: engine_pb2.SynthesisResult | None = None
    async for event in stream:
        payload = event.WhichOneof("payload")
        if result_event is not None:
            raise GenerationRequestError("the Worker sent an event after its terminal result")
        if validator.audio_format is None and payload not in {"header", "error"}:
            raise GenerationRequestError("the Worker must send the audio header first")
        if payload == "header":
            try:
                validator.accept_header(event.header)
            except AudioValidationError as error:
                raise GenerationRequestError(str(error)) from error
        elif payload == "chunk":
            try:
                validator.accept_chunk(event.chunk)
            except AudioValidationError as error:
                raise GenerationRequestError(str(error)) from error
            pcm.extend(event.chunk.pcm)
        elif payload == "progress":
            continue
        elif payload == "result":
            result_event = event.result
        elif payload == "error":
            raise WorkerOperationError(event.error)
        else:
            raise GenerationRequestError("the Worker sent an unknown synthesis event")
    if result_event is None:
        raise GenerationRequestError("the Worker ended without a synthesis result")
    try:
        result = validator.finish(result_event)
    except AudioValidationError as error:
        raise GenerationRequestError(str(error)) from error
    return bytes(pcm), result


class _MalformedAlignment(ValueError):
    pass


def _alignment_is_retryable(alignment: AlignmentJob) -> bool:
    return alignment.state is AlignmentState.FAILED and alignment.error is not None and alignment.error.get("retryable") is True


def _require_alignment_capability(capabilities: Any) -> AlignmentCapability:
    capability = getattr(capabilities, "alignment", None)
    supports = getattr(capabilities, "supports", None)
    if (
        capability is None
        or not callable(supports)
        or not supports("alignment")
        or not capability.aligner.strip()
        or "\0" in capability.aligner
        or not capability.units
        or any(unit not in _ALIGNMENT_UNITS for unit in capability.units)
    ):
        raise GenerationCapabilityError("the Worker does not support alignment")
    return cast(AlignmentCapability, capability)


def _validate_alignment_transcript(transcript: str) -> None:
    if not isinstance(transcript, str) or not transcript.strip() or "\0" in transcript or len(transcript) > _MAX_ALIGNMENT_TRANSCRIPT:
        raise GenerationRequestError("alignment transcript exceeds supported bounds")
    try:
        transcript.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GenerationRequestError("alignment transcript is not valid UTF-8") from error


def _alignment_result_from_worker(response: engine_pb2.AlignResponse, *, job: GenerationJob, artifact: AudioArtifact, capability: AlignmentCapability) -> AlignmentResult:
    if not isinstance(response, engine_pb2.AlignResponse) or not response.HasField("result"):
        raise _MalformedAlignment("Worker response has no result")
    worker_result = response.result
    if len(worker_result.SerializeToString()) > _MAX_ALIGNMENT_PROTO_BYTES or worker_result.schema_version != 1:
        raise _MalformedAlignment("Worker result is unsupported or exceeds bounds")
    if worker_result.transcript != job.text or worker_result.sample_rate_hz != artifact.sample_rate or worker_result.total_frames != artifact.frame_count:
        raise _MalformedAlignment("Worker metadata does not match durable artifact")
    if worker_result.unit not in _ALIGNMENT_UNITS or worker_result.unit not in capability.units or worker_result.aligner != capability.aligner:
        raise _MalformedAlignment("Worker alignment identity is not advertised")
    if len(worker_result.units) > _MAX_ALIGNMENT_UNITS:
        raise _MalformedAlignment("Worker unit count exceeds bounds")
    if job.text and not worker_result.units:
        raise _MalformedAlignment("Worker returned no alignment units for a non-empty transcript")
    transcript_bytes = job.text.encode("utf-8")
    previous_source_end = previous_frame_end = 0
    units: list[AlignmentUnit] = []
    for item in worker_result.units:
        source_start, source_end = int(item.source_start), int(item.source_end)
        start_frames, end_frames = int(item.start_frames), int(item.end_frames)
        confidence = float(item.confidence)
        if not 0 <= source_start < source_end <= len(transcript_bytes) or source_start < previous_source_end:
            raise _MalformedAlignment("Worker source range is invalid")
        try:
            source_text = transcript_bytes[source_start:source_end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise _MalformedAlignment("Worker source range is not UTF-8 aligned") from error
        if not item.text or item.text != source_text or "\0" in item.text:
            raise _MalformedAlignment("Worker unit text does not match transcript")
        if not 0 <= start_frames < end_frames <= artifact.frame_count or start_frames < previous_frame_end:
            raise _MalformedAlignment("Worker frame range is invalid")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise _MalformedAlignment("Worker confidence is invalid")
        units.append(AlignmentUnit(item.text, source_start, source_end, start_frames, end_frames, confidence, bool(item.estimated)))
        previous_source_end, previous_frame_end = source_end, end_frames
    return AlignmentResult(1, job.id, artifact.id, job.text, artifact.sample_rate, artifact.frame_count, worker_result.unit, worker_result.aligner, tuple(units))


def _alignment_exception(error: Exception) -> tuple[str, bool]:
    code_method = getattr(error, "code", None)
    if callable(code_method):
        try:
            name = getattr(code_method(), "name", "")
        except Exception:  # noqa: BLE001 - normalize opaque RPC failures
            name = ""
        if name == "DEADLINE_EXCEEDED":
            return "alignment_deadline_exceeded", True
        if name == "CANCELLED":
            return "alignment_cancelled", True
    return "worker_failed", True


def _alignment_error(code: str, retryable: bool) -> dict[str, Any]:
    safe_code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "worker_failed"
    return {"code": safe_code, "message": "Alignment failed.", "retryable": retryable}


def _validate_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise GenerationRequestError("text must be non-empty and must not contain NUL characters")


def _validate_options(options: SynthesisOptions | None) -> None:
    if options is None:
        return
    bounds = (("speed", options.speed, 0.25, 4.0), ("pitch", options.pitch, -1.0, 1.0), ("volume", options.volume, 0.0, 2.0))
    for name, value, minimum, maximum in bounds:
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise GenerationRequestError(f"{name} must be finite")
        if not minimum <= value <= maximum:
            raise GenerationRequestError(f"{name} must be between {minimum} and {maximum}")


def _require_option_capabilities(lease: Any, options: SynthesisOptions | None) -> None:
    if options is None:
        return
    for name, value in (("speed", options.speed), ("pitch", options.pitch), ("volume", options.volume)):
        if value is not None and not lease.capabilities.supports(name):
            raise GenerationCapabilityError(f"the Worker does not support the {name} option")


def _validate_source(
    voice_id: str | None, reference_id: str | None, saved_voice_id: str | None = None
) -> None:
    if any(value is not None and not value for value in (voice_id, reference_id, saved_voice_id)):
        raise GenerationRequestError("generation request requires non-empty source IDs")
    if sum(value is not None for value in (voice_id, reference_id, saved_voice_id)) != 1:
        raise GenerationRequestError("generation request requires exactly one voice source")


def _reference_handle(handle: ReferenceHandle) -> _ReferenceHandle:
    return _ReferenceHandle(
        reference_id=handle.recording.id,
        relative_path=handle.recording.relative_path,
        transcript=handle.transcript,
    )


def _require_streaming_capability(lease: Any) -> None:
    if not lease.capabilities.supports("streaming_synthesis"):
        raise GenerationCapabilityError("the Worker does not support streaming synthesis")


def _require_preset_capability(lease: Any) -> None:
    if not lease.capabilities.supports("preset_voices"):
        raise GenerationCapabilityError("the Worker does not support preset voices")


def _engine_id(model: ModelInstallation) -> str:
    value = model.compatibility_evidence.get("engine_id")
    if isinstance(value, str) and value:
        return value
    return model.engine_installation_id.split("@", maxsplit=1)[0]


def _provider_config(registry: ProviderRegistry | None, provider_id: str) -> tuple[str, str, str]:
    if registry is None:
        raise _GenerationFailure("provider_configuration_missing", "The provider configuration is unavailable.", retryable=True)
    try:
        profile = registry.get(provider_id)
    except ProviderNotFoundError as error:
        raise _GenerationFailure("provider_configuration_missing", "The provider configuration is unavailable.") from error
    try:
        key = ProviderService.resolve_api_key(profile)
    except Exception as error:
        raise _GenerationFailure("provider_configuration_missing", "The provider credential is not configured.", retryable=True) from error
    return profile.base_url, profile.model, key


def _artifact_id(job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", job_id)
    return f"generation-{safe}"


def _artifact_error_is_missing_or_unsafe(error: OSError) -> bool:
    return error.errno in {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        getattr(errno, "EMLINK", -1),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
