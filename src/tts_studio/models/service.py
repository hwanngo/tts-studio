"""Core application behavior for model compatibility and installation views."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

import grpc
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.events import EventStore, JsonObject, build_download_progress_payload
from tts_studio.generation.domain import GenerationState
from tts_studio.generation.registry import GenerationRegistry
from tts_studio.models.activation import (
    ActivatedModelDirectory,
    InsufficientStorageError,
    ModelActivation,
    ModelVerificationError,
)
from tts_studio.models.registry import (
    DownloadJob,
    DownloadState,
    ModelInstallation,
    ModelRegistry,
    RegistryRecordNotFoundError,
)
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.generation import WorkerOperationError

_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}\Z")
_FILE_URI = re.compile(r"\bfile:/{1,3}", re.IGNORECASE)
_SOCKET_URI = re.compile(r"\b(?:socket|unix):/{1,3}", re.IGNORECASE)
_NETWORK_URL = re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE)
_USERINFO = re.compile(r"\b[^/\s:@]+:[^/\s@]+@[^/\s]+")
_BEARER_SECRET = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_HF_SECRET = re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|credential|password|secret|token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_SECRET_WORD = re.compile(r"\b(?:api[_-]?key|credential|password|secret|token)\b", re.IGNORECASE)
_TRACEBACK_TEXT = re.compile(r"\btraceback\b", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_WINDOWS_UNC_PATH = re.compile(r"\\\\[^\\/\s]+[\\/][^\\/\s]+")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9/])/(?!/)[^\s]+")
_RESOLVED_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "command",
        "credential",
        "environment",
        "password",
        "secret",
        "stack",
        "token",
        "traceback",
        "working_directory",
    }
)
_EVENT_UNSET = object()
_ACTIVE_DOWNLOAD_STATES = frozenset(
    {
        DownloadState.QUEUED,
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    }
)


class InvalidRepositoryIdError(ValueError):
    """Raised without echoing a rejected repository ID."""

    def __init__(self) -> None:
        super().__init__("repository ID is invalid")


class AdapterUnavailableError(RuntimeError):
    """Raised when no configured adapter can answer validation."""

    def __init__(self) -> None:
        super().__init__("no engine adapter is available")


class ModelInstallationNotFoundError(LookupError):
    """Raised without echoing a rejected Model Installation identifier."""

    def __init__(self) -> None:
        super().__init__("model installation was not found")


class DownloadJobNotFoundError(LookupError):
    """Raised without echoing a rejected Download Job identifier."""

    def __init__(self) -> None:
        super().__init__("download job was not found")


class ModelIncompatibleError(RuntimeError):
    """Raised when no installed adapter can run a requested repository."""

    def __init__(self) -> None:
        super().__init__("model is not compatible with an installed adapter")


class ModelValidationError(RuntimeError):
    """Raised when validation returns a structured non-compatible result."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__("model validation failed")
        self.code = code
        self.retryable = retryable


class ModelVariantUnavailableError(ValueError):
    """Raised when a requested runtime variant was not validated by the adapter."""

    def __init__(self) -> None:
        super().__init__("model variant is unavailable")


class ModelInUseError(RuntimeError):
    """Raised when an active generation prevents replacement or removal."""

    def __init__(self) -> None:
        super().__init__("model installation is in use")


class WorkerDownloadError(RuntimeError):
    def __init__(self, code: str = "download_failed", retryable: bool = False) -> None:
        super().__init__("engine adapter could not download the model")
        self.code = code if _SAFE_CODE.fullmatch(code) else "download_failed"
        self.retryable = retryable


class ValidationClient(Protocol):
    """The narrow Worker client seam consumed by model validation."""

    async def validate_model(
        self,
        engine_id: str,
        request: engine_pb2.ValidateModelRequest,
        *,
        timeout: float = 10.0,
    ) -> engine_pb2.ValidateModelResponse: ...

    async def describe(self, engine_id: str) -> engine_pb2.DescribeResponse: ...

    def download_model(
        self,
        engine_id: str,
        request: engine_pb2.DownloadModelRequest,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]: ...

    async def unload_model(self, engine_id: str, model_id: str, cache_path: Path) -> None: ...


@dataclass(frozen=True)
class ModelVariant:
    id: str
    label: str


@dataclass(frozen=True)
class CompatibilityEvidence:
    code: str
    message: str


@dataclass(frozen=True)
class AdapterCompatibility:
    engine_id: str
    engine_version: str
    available: bool
    compatible: bool
    resolved_commit: str | None
    required_files: tuple[str, ...]
    available_variants: tuple[ModelVariant, ...]
    estimated_bytes: int | None
    evidence: tuple[CompatibilityEvidence, ...]
    error_code: str | None
    error_retryable: bool | None


@dataclass(frozen=True)
class ValidationResult:
    repository_id: str
    requested_revision: str | None
    compatible: bool
    selected_engine_id: str | None
    results: tuple[AdapterCompatibility, ...]


@dataclass(frozen=True)
class ModelInstallationView:
    id: str
    repository_id: str
    requested_revision: str | None
    resolved_commit: str
    engine_installation_id: str
    compatibility_evidence: dict[str, Any]
    runtime_variant: str
    byte_size: int
    cache_path: str
    desired_load_state: str
    observed_load_state: str
    replica_summary: dict[str, Any]
    desired_replicas: int
    last_error: dict[str, Any] | None
    created_at: str
    updated_at: str


class ModelService:
    """Coordinate registry reads and isolated adapter validation."""

    def __init__(
        self,
        registry: ModelRegistry,
        validation_client: ValidationClient,
        adapters: Sequence[AdapterDescriptor],
        *,
        layout: StorageLayout | None = None,
        storage_limit_bytes: int | None = None,
        event_store: EventStore | None = None,
        generation_registry: GenerationRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._validation_client = validation_client
        self._adapters = tuple(sorted(adapters, key=lambda item: (item.priority, item.engine_id)))
        self._adapter_by_engine = {adapter.engine_id: adapter for adapter in self._adapters}
        self._activation = (
            ModelActivation(layout, storage_limit_bytes=storage_limit_bytes)
            if layout is not None
            else None
        )
        self._download_tasks: dict[str, asyncio.Task[None]] = {}
        self._activation_lock = asyncio.Lock()
        self._event_store = event_store
        self._generation_registry = generation_registry

    async def validate(
        self,
        repository_id: str,
        requested_revision: str | None = None,
    ) -> ValidationResult:
        """Return all adapter evidence and deterministically select one compatible adapter."""
        canonical_id = canonicalize_repository_id(repository_id)
        request = engine_pb2.ValidateModelRequest(repository_id=canonical_id)
        if requested_revision is not None:
            request.requested_revision = requested_revision

        results: list[AdapterCompatibility] = []
        responded = False
        for descriptor in self._adapters:
            try:
                response = await self._validation_client.validate_model(
                    descriptor.engine_id,
                    request,
                )
            except (grpc.aio.AioRpcError, RuntimeError, TimeoutError):
                results.append(_unavailable_result(descriptor.engine_id))
                continue
            responded = True
            results.append(
                _map_worker_response(
                    descriptor.engine_id,
                    response,
                    repository_id=canonical_id,
                    requested_revision=requested_revision,
                )
            )

        if not responded:
            self._publish_validation_failure(canonical_id)
            raise AdapterUnavailableError

        selected = next((item for item in results if item.available and item.compatible), None)
        result = ValidationResult(
            repository_id=canonical_id,
            requested_revision=requested_revision,
            compatible=selected is not None,
            selected_engine_id=selected.engine_id if selected is not None else None,
            results=tuple(results),
        )
        self._publish_validation(result)
        return result

    def list_models(self) -> tuple[ModelInstallationView, ...]:
        """Return safe public projections of all active Model Installations."""
        return tuple(_model_view(model) for model in self._registry.list_models())

    def get_model(self, model_id: str) -> ModelInstallationView:
        """Return one active Model Installation without exposing lookup input on failure."""
        model = next((item for item in self._registry.list_models() if item.id == model_id), None)
        if model is None:
            raise ModelInstallationNotFoundError
        return _model_view(model)

    async def set_desired_replicas(self, model_id: str, count: int) -> ModelInstallationView:
        try:
            model = self._registry.get_model(model_id)
        except RegistryRecordNotFoundError as error:
            raise ModelInstallationNotFoundError from error
        ensure = getattr(self._validation_client, "ensure_replicas", None)
        if ensure is not None:
            await ensure(model, count)
        self._registry.set_desired_replicas(model_id, count)
        return _model_view(self._registry.get_model(model_id))

    async def start_download(
        self,
        repository_id: str,
        *,
        requested_revision: str | None = None,
        variant: str | None = None,
        correlation_id: str,
    ) -> DownloadJob:
        """Create durable queued work and execute it independently of the caller."""
        activation = self._required_activation()
        validation = await self.validate(repository_id, requested_revision)
        selected = next(
            (
                result
                for result in validation.results
                if result.engine_id == validation.selected_engine_id
                and result.available
                and result.compatible
            ),
            None,
        )
        if selected is None or selected.resolved_commit is None:
            structured_error = next(
                (item for item in validation.results if item.error_code is not None),
                None,
            )
            if structured_error is not None:
                raise ModelValidationError(
                    structured_error.error_code or "model_incompatible",
                    retryable=bool(structured_error.error_retryable),
                )
            raise ModelIncompatibleError
        selected_variant = variant or (
            selected.available_variants[0].id if selected.available_variants else None
        )
        if selected_variant is None or selected_variant not in {
            item.id for item in selected.available_variants
        }:
            raise ModelVariantUnavailableError

        descriptor = self._adapter_by_engine[selected.engine_id]
        engine_installation_id = f"{selected.engine_id}@{selected.engine_version}"
        capabilities = await self._runtime_capabilities(selected.engine_id)
        self._registry.upsert_engine_installation(
            engine_installation_id=engine_installation_id,
            engine_id=selected.engine_id,
            version=selected.engine_version,
            command=list(descriptor.launch.command),
            working_directory=str(descriptor.launch.cwd),
            environment={"manager": "uv", "locked": True},
            capabilities=capabilities,
            lifecycle_state="ready",
        )
        job_id = str(uuid4())
        job = self._registry.create_download_job(
            job_id=job_id,
            repository_id=validation.repository_id,
            requested_revision=requested_revision,
            engine_installation_id=engine_installation_id,
            staging_path=f"staging/{job_id}",
            correlation_id=correlation_id,
        )
        self._publish_download(job, "download.queued", "Model download was queued.")
        task = asyncio.create_task(
            self._run_download(job, selected, selected_variant, activation),
            name=f"model-download-{job_id}",
        )
        self._download_tasks[job_id] = task
        task.add_done_callback(partial(self._download_done, job_id))
        return job

    async def _runtime_capabilities(self, engine_id: str) -> dict[str, Any]:
        describe = getattr(self._validation_client, "describe", None)
        if describe is None:
            return {"supported": [], "max_concurrency": 0}
        response = await describe(engine_id)
        supported = sorted(
            {
                capability.name
                for capability in response.capabilities
                if capability.supported and _SAFE_CODE.fullmatch(capability.name)
            }
        )
        max_concurrency = response.max_concurrency
        if not 0 < max_concurrency <= 1024:
            max_concurrency = 0
        return {"supported": supported, "max_concurrency": max_concurrency}

    def list_downloads(self) -> tuple[DownloadJob, ...]:
        return self._registry.list_download_jobs()

    def get_download(self, job_id: str) -> DownloadJob:
        try:
            return self._registry.get_download_job(job_id)
        except RegistryRecordNotFoundError as error:
            raise DownloadJobNotFoundError from error

    async def wait_for_download(self, job_id: str) -> DownloadJob:
        task = self._download_tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)
        return self.get_download(job_id)

    async def cancel_download(self, job_id: str) -> DownloadJob:
        try:
            requested = self._registry.request_download_cancellation(job_id)
        except RegistryRecordNotFoundError as error:
            raise DownloadJobNotFoundError from error
        if requested.state in {
            DownloadState.COMPLETED,
            DownloadState.CANCELLED,
            DownloadState.FAILED,
        }:
            return requested
        if requested.state is DownloadState.QUEUED:
            cancelled = self._cancel_job(job_id)
            task = self._download_tasks.get(job_id)
            if task is not None:
                task.cancel()
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
            return cancelled
        task = self._download_tasks.get(job_id)
        if task is not None:
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # The download task owns its terminal transition and catches
                # cancellation after the authenticated stream has closed.
                pass
            return self.get_download(job_id)
        return self.get_download(job_id)

    async def remove_model(self, model_id: str) -> None:
        activation = self._required_activation()
        async with self._activation_lock:
            try:
                model = self._registry.get_model(model_id)
            except RegistryRecordNotFoundError as error:
                raise ModelInstallationNotFoundError from error
            if _model_is_in_use(model) or self._generation_model_is_in_use(model.id):
                raise ModelInUseError
            model_path = activation.model_path(model.cache_path)
            await self._unload_model(_model_engine_id(model), model.id, model_path)
            retired = activation.retire(model.cache_path)
            try:
                if not self._registry.remove_model(model.id):
                    raise ModelInstallationNotFoundError
            except BaseException:
                activation.restore_retired(retired)
                raise
            activation.discard_retired(retired)

    def recover_downloads(self) -> None:
        interrupted = {
            job.id
            for job in self._registry.list_download_jobs()
            if job.state in _ACTIVE_DOWNLOAD_STATES
        }
        self._required_activation().recover(self._registry)
        for job_id in interrupted:
            recovered = self._registry.get_download_job(job_id)
            if recovered.state is DownloadState.FAILED:
                self._publish_download(
                    recovered,
                    "download.failed",
                    "An interrupted model download requires recovery.",
                    error=recovered.error,
                )

    async def close(self) -> None:
        tasks = tuple(self._download_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _generation_model_is_in_use(self, model_id: str) -> bool:
        if self._generation_registry is None:
            return False
        for job in self._generation_registry.list_jobs():
            if job.model_id != model_id:
                continue
            if job.state not in {GenerationState.COMPLETED, GenerationState.CANCELLED, GenerationState.FAILED}:
                return True
            try:
                alignment = self._generation_registry.get_alignment(job.id)
            except LookupError:
                continue
            if alignment.state.value in {"queued", "running"}:
                return True
        return False

    async def _unload_model(self, engine_id: str, model_id: str, cache_path: Path) -> None:
        try:
            await self._validation_client.unload_model(engine_id, model_id, cache_path)
        except WorkerOperationError as error:
            if error.code != "model_not_loaded":
                raise

    async def _run_download(
        self,
        job: DownloadJob,
        selected: AdapterCompatibility,
        variant: str,
        activation: ModelActivation,
    ) -> None:
        staging: Path | None = None
        activated: ActivatedModelDirectory | None = None
        try:
            self._raise_if_cancelled(job.id)
            self._transition_download(
                job.id, DownloadState.VALIDATING, phase="validating"
            )
            if not selected.compatible or selected.resolved_commit is None:
                raise ModelIncompatibleError
            if variant not in {item.id for item in selected.available_variants}:
                raise ModelVariantUnavailableError
            self._raise_if_cancelled(job.id)

            staging = activation.allocate_staging(job.id)
            self._transition_download(
                job.id, DownloadState.DOWNLOADING, phase="downloading"
            )
            manifest: engine_pb2.ModelManifest | None = None
            sequence = 0
            request = engine_pb2.DownloadModelRequest(
                repository_id=job.repository_id,
                resolved_commit=selected.resolved_commit,
                variant=variant,
                staging_destination=job.id,
            )
            async for event in self._validation_client.download_model(
                selected.engine_id, request
            ):
                self._raise_if_cancelled(job.id)
                payload = event.WhichOneof("payload")
                if payload == "error":
                    raise WorkerDownloadError(event.error.code, event.error.retryable)
                if payload == "manifest":
                    if manifest is not None:
                        raise WorkerDownloadError()
                    manifest = engine_pb2.ModelManifest()
                    manifest.CopyFrom(event.manifest)
                    continue
                if payload != "progress" or manifest is not None:
                    raise WorkerDownloadError()
                progress = event.progress
                if progress.sequence <= sequence:
                    raise WorkerDownloadError()
                sequence = progress.sequence
                state = (
                    DownloadState.VERIFYING
                    if progress.phase
                    in {
                        engine_pb2.DOWNLOAD_PHASE_VERIFYING,
                        engine_pb2.DOWNLOAD_PHASE_FINALIZING,
                    }
                    else DownloadState.DOWNLOADING
                )
                if progress.HasField("total_bytes"):
                    self._transition_download(
                        job.id,
                        state,
                        phase=("verifying" if state is DownloadState.VERIFYING else "downloading"),
                        bytes_downloaded=progress.bytes_downloaded,
                        total_bytes=progress.total_bytes,
                    )
                else:
                    self._transition_download(
                        job.id,
                        state,
                        phase=("verifying" if state is DownloadState.VERIFYING else "downloading"),
                        bytes_downloaded=progress.bytes_downloaded,
                    )

            if manifest is None:
                raise WorkerDownloadError()
            current = self._registry.get_download_job(job.id)
            if current.state is DownloadState.DOWNLOADING:
                current = self._transition_download(
                    job.id, DownloadState.VERIFYING, phase="verifying"
                )
            verified = activation.verify(
                staging,
                manifest,
                repository_id=job.repository_id,
                resolved_commit=selected.resolved_commit,
                variant=variant,
                required_files=selected.required_files,
                expected_bytes=current.bytes_downloaded,
            )
            self._raise_if_cancelled(job.id)
            async with self._activation_lock:
                installed_models = self._registry.list_models()
                previous = next(
                    (
                        model
                        for model in installed_models
                        if model.repository_id == job.repository_id
                    ),
                    None,
                )
                activation.ensure_storage_available(
                    verified.byte_size,
                    installed_bytes=sum(model.byte_size for model in installed_models),
                    replacing_bytes=previous.byte_size if previous is not None else 0,
                )
                self._transition_download(
                    job.id, DownloadState.ACTIVATING, phase="activating"
                )
                if previous is not None:
                    if _model_is_in_use(previous) or self._generation_model_is_in_use(previous.id):
                        raise ModelInUseError
                    await self._unload_model(
                        _model_engine_id(previous),
                        previous.id,
                        activation.model_path(previous.cache_path),
                    )

                model_id = str(uuid4())
                activated = activation.activate(staging, model_id, verified)
                try:
                    activation.cleanup_staging(job.id)
                except BaseException:
                    activation.rollback_activation(activated)
                    activated = None
                    raise
                staging = None
                try:
                    installed = self._registry.activate_model(
                        download_job_id=job.id,
                        model_id=model_id,
                        repository_id=job.repository_id,
                        requested_revision=job.requested_revision,
                        resolved_commit=selected.resolved_commit,
                        engine_installation_id=job.engine_installation_id,
                        compatibility_evidence={
                            "engine_id": selected.engine_id,
                            "engine_version": selected.engine_version,
                            "evidence": [item.__dict__ for item in selected.evidence],
                        },
                        runtime_variant=variant,
                        manifest=verified.manifest,
                        checksum_summary=verified.checksum_summary,
                        byte_size=verified.byte_size,
                        cache_path=activated.cache_path,
                        desired_load_state="unloaded",
                        observed_load_state="unloaded",
                        replica_summary={"ready": 0, "active_generations": 0},
                    )
                except BaseException:
                    activation.rollback_activation(activated)
                    activated = None
                    raise
                if previous is not None:
                    activation.discard_model(previous.cache_path)
                completed = self._registry.get_download_job(job.id)
                self._publish_download(
                    completed,
                    "model.activated",
                    "Model download completed and was activated.",
                    extra={"model_id": installed.id},
                )
        except asyncio.CancelledError:
            if activated is not None:
                activation.rollback_activation(activated)
            self._cancel_job(job.id)
        except ModelVerificationError as error:
            self._fail_job(
                job.id,
                code=error.code,
                message="Downloaded model verification failed.",
                retryable=False,
            )
        except InsufficientStorageError:
            self._fail_job(
                job.id,
                code="insufficient_storage",
                message="There is not enough managed storage to activate this model.",
                retryable=True,
            )
        except ModelInUseError:
            self._fail_job(
                job.id,
                code="model_in_use",
                message="The current model revision is in use.",
                retryable=True,
            )
        except WorkerDownloadError as error:
            self._fail_job(
                job.id,
                code=error.code,
                message="The engine adapter could not download the model.",
                retryable=error.retryable,
            )
        except (ModelIncompatibleError, ModelVariantUnavailableError):
            self._fail_job(
                job.id,
                code="model_incompatible",
                message="The validated model selection is no longer available.",
                retryable=False,
            )
        except Exception:  # noqa: BLE001 - durable job boundary normalizes Worker/filesystem faults
            self._fail_job(
                job.id,
                code="download_failed",
                message="Model download failed.",
                retryable=True,
            )
        finally:
            if staging is not None:
                activation.cleanup_staging(job.id)

    def _raise_if_cancelled(self, job_id: str) -> None:
        if self._registry.get_download_job(job_id).cancellation_requested:
            raise asyncio.CancelledError

    def _cancel_job(self, job_id: str) -> DownloadJob:
        current = self._registry.get_download_job(job_id)
        if current.state in {
            DownloadState.COMPLETED,
            DownloadState.CANCELLED,
            DownloadState.FAILED,
        }:
            return current
        cancelled = self._registry.transition_download_job(
            job_id,
            DownloadState.CANCELLED,
            phase="cancelled",
            error={
                "code": "download_cancelled",
                "message": "Model download was cancelled.",
                "retryable": True,
            },
        )
        self._publish_download(
            cancelled,
            "download.cancelled",
            "Model download was cancelled.",
            error=cancelled.error,
        )
        return cancelled

    def _fail_job(self, job_id: str, *, code: str, message: str, retryable: bool) -> None:
        current = self._registry.get_download_job(job_id)
        if current.state in {
            DownloadState.COMPLETED,
            DownloadState.CANCELLED,
            DownloadState.FAILED,
        }:
            return
        failed = self._registry.transition_download_job(
            job_id,
            DownloadState.FAILED,
            phase="failed",
            error={"code": code, "message": message, "retryable": retryable},
        )
        self._publish_download(
            failed,
            "download.failed",
            "Model download failed.",
            error=failed.error,
        )

    def _transition_download(
        self,
        job_id: str,
        state: DownloadState,
        *,
        phase: str | None = None,
        bytes_downloaded: int | None = None,
        total_bytes: int | None | object = _EVENT_UNSET,
    ) -> DownloadJob:
        options: dict[str, Any] = {
            "phase": phase,
            "bytes_downloaded": bytes_downloaded,
        }
        if total_bytes is not _EVENT_UNSET:
            options["total_bytes"] = total_bytes
        job = self._registry.transition_download_job(job_id, state, **options)
        if state in {DownloadState.DOWNLOADING, DownloadState.VERIFYING}:
            event_type = "download.progress"
            message = (
                "Model files are being verified."
                if state is DownloadState.VERIFYING
                else "Model files are downloading."
            )
        elif state is DownloadState.VALIDATING:
            event_type = "download.validating"
            message = "Model compatibility is being revalidated."
        elif state is DownloadState.ACTIVATING:
            event_type = "download.activating"
            message = "The verified model is being activated."
        else:
            return job
        self._publish_download(job, event_type, message)
        return job

    def _publish_validation(self, result: ValidationResult) -> None:
        if self._event_store is None:
            return
        self._event_store.append(
            "model.validation",
            {
                "repository_id": result.repository_id,
                "compatible": result.compatible,
                "selected_engine_id": result.selected_engine_id,
                "message": (
                    "A compatible engine adapter was found."
                    if result.compatible
                    else "No compatible engine adapter was found."
                ),
            },
        )

    def _publish_validation_failure(self, repository_id: str) -> None:
        if self._event_store is None:
            return
        self._event_store.append(
            "model.validation",
            {
                "repository_id": repository_id,
                "compatible": False,
                "selected_engine_id": None,
                "error_code": "adapter_unavailable",
                "message": "No engine adapter was available for validation.",
            },
        )

    def _publish_download(
        self,
        job: DownloadJob,
        event_type: str,
        message: str,
        *,
        error: Mapping[str, Any] | None = None,
        extra: JsonObject | None = None,
    ) -> None:
        if self._event_store is None:
            return
        payload = build_download_progress_payload(
            job_id=job.id,
            phase=job.phase,
            bytes_downloaded=job.bytes_downloaded,
            total_bytes=job.total_bytes,
            message=message,
        )
        if error is not None:
            code = error.get("code")
            retryable = error.get("retryable")
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code) is not None:
                payload["error_code"] = code
            if isinstance(retryable, bool):
                payload["retryable"] = retryable
        if extra is not None:
            payload.update(extra)
        self._event_store.append(event_type, payload, download_id=job.id)

    def _required_activation(self) -> ModelActivation:
        if self._activation is None:
            raise RuntimeError("model filesystem management is unavailable")
        return self._activation

    def _download_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._download_tasks.pop(job_id, None)
        if not task.cancelled():
            task.exception()


def canonicalize_repository_id(repository_id: str) -> str:
    """Validate a Hugging Face-style ID while preserving its case-sensitive identity."""
    if (
        not isinstance(repository_id, str)
        or repository_id != repository_id.strip()
        or not 1 <= len(repository_id) <= 96
        or repository_id.count("/") > 1
        or "--" in repository_id
        or ".." in repository_id
        or repository_id.endswith(".git")
    ):
        raise InvalidRepositoryIdError
    components = repository_id.split("/")
    if any(
        not component
        or _REPOSITORY_COMPONENT.fullmatch(component) is None
        or component[0] in ".-"
        or component[-1] in ".-"
        for component in components
    ):
        raise InvalidRepositoryIdError
    return repository_id


def _map_worker_response(
    descriptor_engine_id: str,
    response: engine_pb2.ValidateModelResponse,
    *,
    repository_id: str,
    requested_revision: str | None,
) -> AdapterCompatibility:
    response_revision = (
        response.requested_revision if response.HasField("requested_revision") else None
    )
    identity_matches = (
        response.repository_id == repository_id
        and (
            response_revision is None
            or response_revision == requested_revision
            or (requested_revision is None and response_revision == "")
        )
        and response.engine_id == descriptor_engine_id
    )
    compatible = bool(response.compatible) and identity_matches
    error_code: str | None = None
    if not compatible:
        candidate = response.error.code
        error_code = candidate if _SAFE_CODE.fullmatch(candidate) else "model_incompatible"
    return AdapterCompatibility(
        engine_id=descriptor_engine_id,
        engine_version=(
            response.engine_version
            if _SAFE_VERSION.fullmatch(response.engine_version)
            else "unknown"
        ),
        available=True,
        compatible=compatible,
        resolved_commit=(
            response.resolved_commit
            if response.HasField("resolved_commit")
            and _RESOLVED_COMMIT.fullmatch(response.resolved_commit)
            else None
        ),
        required_files=tuple(
            file for file in response.required_files if _is_safe_relative_file(file)
        ),
        available_variants=tuple(
            ModelVariant(
                id=variant.id if _SAFE_CODE.fullmatch(variant.id) else "unknown",
                label=_safe_message(variant.label, "Adapter variant"),
            )
            for variant in response.available_variants
        ),
        estimated_bytes=(
            response.estimated_bytes if response.HasField("estimated_bytes") else None
        ),
        evidence=tuple(
            CompatibilityEvidence(
                code=evidence.code if _SAFE_CODE.fullmatch(evidence.code) else "adapter_evidence",
                message=_safe_message(
                    evidence.message,
                    "The adapter supplied compatibility evidence.",
                ),
            )
            for evidence in response.evidence
        ),
        error_code=error_code,
        error_retryable=(response.error.retryable if error_code is not None else None),
    )


def _unavailable_result(engine_id: str) -> AdapterCompatibility:
    return AdapterCompatibility(
        engine_id=engine_id,
        engine_version="unknown",
        available=False,
        compatible=False,
        resolved_commit=None,
        required_files=(),
        available_variants=(),
        estimated_bytes=None,
        evidence=(),
        error_code="adapter_unavailable",
        error_retryable=True,
    )


def _model_view(model: ModelInstallation) -> ModelInstallationView:
    last_error = _sanitize_mapping(model.last_error) if model.last_error is not None else None
    return ModelInstallationView(
        id=model.id,
        repository_id=model.repository_id,
        requested_revision=model.requested_revision,
        resolved_commit=model.resolved_commit,
        engine_installation_id=model.engine_installation_id,
        compatibility_evidence=_sanitize_mapping(model.compatibility_evidence),
        runtime_variant=model.runtime_variant,
        byte_size=model.byte_size,
        cache_path=model.cache_path,
        desired_load_state=model.desired_load_state,
        observed_load_state=model.observed_load_state,
        replica_summary=_sanitize_mapping(model.replica_summary),
        desired_replicas=model.desired_replicas,
        last_error=last_error,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _sanitize_json(item)
        for key, item in value.items()
        if key.casefold() not in _SENSITIVE_KEYS
    }


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_message(value, "[redacted]")
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    return value


def _safe_message(value: str, fallback: str) -> str:
    if (
        not value
        or len(value) > 500
        or _contains_machine_path(value)
        or _contains_sensitive_text(value)
        or any(ord(character) < 32 and character not in "\t\n" for character in value)
    ):
        return fallback
    return value


def _contains_machine_path(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            _FILE_URI,
            _SOCKET_URI,
            _WINDOWS_ABSOLUTE_PATH,
            _WINDOWS_UNC_PATH,
            _POSIX_ABSOLUTE_PATH,
        )
    )


def _contains_sensitive_text(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            _NETWORK_URL,
            _USERINFO,
            _BEARER_SECRET,
            _HF_SECRET,
            _SECRET_ASSIGNMENT,
            _SECRET_WORD,
            _TRACEBACK_TEXT,
        )
    )


def _is_safe_relative_file(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value
        and "\\" not in value
        and not value.startswith("/")
        and "//" not in value
        and not _contains_machine_path(value)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _model_is_in_use(model: ModelInstallation) -> bool:
    active = model.replica_summary.get("active_generations", 0)
    return isinstance(active, int) and not isinstance(active, bool) and active > 0


def _model_engine_id(model: ModelInstallation) -> str:
    engine_id = model.compatibility_evidence.get("engine_id")
    if isinstance(engine_id, str) and engine_id:
        return engine_id
    return model.engine_installation_id.split("@", maxsplit=1)[0]
