"""Generated public API types. Do not edit by hand."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AdapterCompatibilityResponse(BaseModel):
    available: bool
    available_variants: list[ModelVariantResponse]
    compatible: bool
    engine_id: str
    engine_version: str
    error_code: str | None
    error_retryable: bool | None
    estimated_bytes: int | None
    evidence: list[CompatibilityEvidenceResponse]
    required_files: list[str]
    resolved_commit: str | None


class AlignmentErrorResponse(BaseModel):
    code: str
    retryable: bool


class AlignmentResponse(BaseModel):
    artifact_id: str
    created_at: str
    error: AlignmentErrorResponse | None
    job_id: str
    result: AlignmentResultResponse | None
    state: str
    updated_at: str


class AlignmentResultResponse(BaseModel):
    aligner: str
    artifact_id: str
    job_id: str
    sample_rate_hz: int
    schema_version: int
    total_frames: int
    transcript: str
    unit: str
    units: list[AlignmentUnitResponse]


class AlignmentUnitResponse(BaseModel):
    confidence: float
    end_ms: float
    estimated: bool
    source_end: int
    source_start: int
    start_ms: float
    text: str


class AudioArtifactResponse(BaseModel):
    audio_url: str
    byte_size: int
    channel_count: int
    created_at: str
    duration_ms: int | None
    frame_count: int
    id: str
    job_id: str
    retained_at: str
    sample_rate: int
    sha256: str


class ClearRetentionRequest(BaseModel):
    confirm: bool | None = None


class ClearRetentionResponse(BaseModel):
    deleted: int
    failed: int
    issues: list[dict[str, Any]]
    skipped: int


class CompatibilityEvidenceResponse(BaseModel):
    code: str
    message: str


class ConfirmationRequest(BaseModel):
    confirm: bool | None = None


class DownloadJobResponse(BaseModel):
    bytes_downloaded: int
    cancellation_requested: bool
    correlation_id: str
    created_at: str
    engine_installation_id: str
    error: dict[str, Any] | None
    id: str
    phase: str
    repository_id: str
    requested_revision: str | None
    staging_path: str
    state: str
    target_model_id: str | None
    total_bytes: int | None
    updated_at: str


class DownloadRequest(BaseModel):
    repository_id: str
    requested_revision: str | None = None
    variant: str | None = None


class ErrorBody(BaseModel):
    code: str
    correlation_id: str
    details: dict[str, Any] | None = None
    message: str
    retryable: bool
    source: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class GenerationJobResponse(BaseModel):
    artifact_id: str | None
    artifact_url: str | None
    bytes_written: int
    cancellation_requested: bool
    channel_count: int | None
    correlation_id: str
    created_at: str
    engine_id: str
    error: dict[str, Any] | None
    frame_count: int
    id: str
    model_id: str
    reference_id: str | None
    retain_artifact: bool
    sample_rate: int | None
    saved_voice_id: str | None
    state: str
    text: str
    updated_at: str
    voice_id: str | None


class GenerationRequest(BaseModel):
    model_id: str
    pitch: float | None = None
    reference_id: str | None = None
    retain_artifact: bool | None = None
    saved_voice_id: str | None = None
    speed: float | None = None
    text: str
    voice_id: str | None = None
    volume: float | None = None


class HTTPValidationError(BaseModel):
    detail: list[ValidationError] | None = None


class ModelInstallationResponse(BaseModel):
    byte_size: int
    cache_path: str
    compatibility_evidence: dict[str, Any]
    created_at: str
    desired_load_state: str
    desired_replicas: int
    engine_installation_id: str
    id: str
    last_error: dict[str, Any] | None
    observed_load_state: str
    replica_summary: dict[str, Any]
    repository_id: str
    requested_revision: str | None
    resolved_commit: str
    runtime_variant: str
    updated_at: str


class ModelValidationRequest(BaseModel):
    repository_id: str
    requested_revision: str | None = None


class ModelValidationResponse(BaseModel):
    compatible: bool
    repository_id: str
    requested_revision: str | None
    results: list[AdapterCompatibilityResponse]
    selected_engine_id: str | None


class ModelVariantResponse(BaseModel):
    id: str
    label: str


class ProviderRequest(BaseModel):
    api_key_env: str
    base_url: str
    kind: str | None = None
    label: str
    model: str


class ProviderResponse(BaseModel):
    api_key_env: str
    base_url: str
    created_at: str
    id: str
    kind: str
    label: str
    model: str
    updated_at: str


class ReferenceEvidence(BaseModel):
    code: str
    message: str


class ReferenceResponse(BaseModel):
    byte_size: int
    channels: int
    container: str
    duration_ms: int
    evidence: list[ReferenceEvidence]
    expires_at: str
    id: str
    model_id: str
    sample_rate_hz: int
    sha256: str
    state: str
    transcript_present: bool


class ReplicaConfigurationRequest(BaseModel):
    desired_replicas: int


class RetentionSummaryResponse(BaseModel):
    max_age_days: int | None
    max_storage_bytes: int | None
    retained_bytes: int
    retained_count: int


class RuntimeResponse(BaseModel):
    active_generations: dict[str, Any] | None
    data_dir: str
    database_accessible: bool
    generation_status: str
    host: str
    port: int
    startup_diagnostics: dict[str, Any]
    storage_accessible: bool
    version: str
    workers: list[WorkerRuntimeResponse]


class SavedVoiceRequest(BaseModel):
    label: str
    model_id: str
    reference_id: str


class SavedVoiceResponse(BaseModel):
    created_at: str
    id: str
    label: str
    model_id: str
    transcript_present: bool


class ServiceOperationResponse(BaseModel):
    changed: bool
    message: str
    operation: str


class ServiceResponse(BaseModel):
    healthy: bool | None
    installed: bool | None
    message: str
    running: bool
    status: str


class SettingsPatchRequest(BaseModel):
    artifact_max_age_days: int | None = None
    artifact_max_storage_bytes: int | None = None
    retain_audio_by_default: bool | None = None


class SettingsResponse(BaseModel):
    api_token_env: str | None
    artifact_max_age_days: int | None
    artifact_max_storage_bytes: int | None
    host: str
    port: int
    restart_required: bool
    retain_audio_by_default: bool
    retention: RetentionSummaryResponse


class SpeechJsonResponse(BaseModel):
    aligner: str
    artifact_id: str
    audio: str
    audio_format: Literal["wav"]
    byte_size: int
    encoding: Literal["base64"]
    job_id: str
    media_type: Literal["audio/wav"]
    sample_rate_hz: int
    schema_version: int
    total_frames: int
    transcript: str
    unit: str
    units: list[AlignmentUnitResponse]


class SpeechRequest(BaseModel):
    input: str
    model: str
    response_format: Literal["wav", "json"] | None = None
    voice: str


class SystemStatus(BaseModel):
    data_dir: str
    status: Literal["healthy", "unavailable"]
    version: str
    workers: list[WorkerSummary]


class ValidationError(BaseModel):
    ctx: dict[str, Any] | None = None
    input: Any | None = None
    loc: list[str | int]
    msg: str
    type: str


class VoicePreviewRequest(BaseModel):
    model_id: str
    text: str
    voice_id: str


class VoiceResponse(BaseModel):
    capabilities: list[str]
    id: str
    label: str


class WorkerRuntimeResponse(BaseModel):
    capabilities: list[str] | None
    engine_id: str
    engine_version: str | None
    max_concurrency: int | None
    message: str
    status: str


class WorkerSummary(BaseModel):
    engine_id: str
    message: str
    status: Literal["ready", "unhealthy"]


for _model in (
    AdapterCompatibilityResponse,
    AlignmentErrorResponse,
    AlignmentResponse,
    AlignmentResultResponse,
    AlignmentUnitResponse,
    AudioArtifactResponse,
    ClearRetentionRequest,
    ClearRetentionResponse,
    CompatibilityEvidenceResponse,
    ConfirmationRequest,
    DownloadJobResponse,
    DownloadRequest,
    ErrorBody,
    ErrorEnvelope,
    GenerationJobResponse,
    GenerationRequest,
    HTTPValidationError,
    ModelInstallationResponse,
    ModelValidationRequest,
    ModelValidationResponse,
    ModelVariantResponse,
    ProviderRequest,
    ProviderResponse,
    ReferenceEvidence,
    ReferenceResponse,
    ReplicaConfigurationRequest,
    RetentionSummaryResponse,
    RuntimeResponse,
    SavedVoiceRequest,
    SavedVoiceResponse,
    ServiceOperationResponse,
    ServiceResponse,
    SettingsPatchRequest,
    SettingsResponse,
    SpeechJsonResponse,
    SpeechRequest,
    SystemStatus,
    ValidationError,
    VoicePreviewRequest,
    VoiceResponse,
    WorkerRuntimeResponse,
    WorkerSummary,
):
    _model.model_rebuild()

del _model
