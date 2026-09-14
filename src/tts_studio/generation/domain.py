"""Immutable values for Core-owned generation persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

JsonObject = dict[str, Any]


class AlignmentState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class AlignmentUnit:
    text: str
    source_start: int
    source_end: int
    start_frames: int
    end_frames: int
    confidence: float
    estimated: bool

    def milliseconds(self, sample_rate_hz: int) -> tuple[float, float]:
        if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be a positive integer")
        scale = 1000.0 / sample_rate_hz
        return self.start_frames * scale, self.end_frames * scale


@dataclass(frozen=True)
class AlignmentResult:
    schema_version: int
    job_id: str
    artifact_id: str
    transcript: str
    sample_rate_hz: int
    total_frames: int
    unit: str
    aligner: str
    units: tuple[AlignmentUnit, ...]


@dataclass(frozen=True)
class AlignmentJob:
    job_id: str
    artifact_id: str
    state: AlignmentState
    result: AlignmentResult | None
    error: JsonObject | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SynthesisOptions:
    speed: float | None = None
    pitch: float | None = None
    volume: float | None = None


class GenerationState(str, Enum):
    QUEUED = "queued"
    LOADING = "loading"
    GENERATING = "generating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class GenerationJob:
    id: str
    model_id: str
    engine_id: str
    voice_id: str | None
    reference_id: str | None
    saved_voice_id: str | None
    provider_id: str | None
    text: str
    retain_artifact: bool
    options: SynthesisOptions | None
    state: GenerationState
    bytes_written: int
    frame_count: int
    sample_rate: int | None
    channel_count: int | None
    artifact_id: str | None
    correlation_id: str
    cancellation_requested: bool
    error: JsonObject | None
    created_at: str
    updated_at: str

    @property
    def frames(self) -> int:
        return self.frame_count

    @property
    def can_retry(self) -> bool:
        return (
            self.state is GenerationState.FAILED
            and self.error is not None
            and self.error.get("retryable") is True
            and self.error.get("code") not in {"cleanup_failed", "reference_recovery_required"}
            and self.retain_artifact
            and bool(self.text)
            and self.artifact_id is None
            and self.reference_id is None
            and (self.voice_id is not None or self.saved_voice_id is not None)
        )


@dataclass(frozen=True)
class AudioArtifact:
    id: str
    job_id: str
    path: str
    byte_size: int
    sha256: str
    sample_rate: int
    channel_count: int
    frame_count: int
    duration_ms: int | None
    created_at: str
    retained_at: str

    @property
    def artifact_path(self) -> str:
        return self.path

    @property
    def frames(self) -> int:
        return self.frame_count
