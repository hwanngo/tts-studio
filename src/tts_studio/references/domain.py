"""Immutable domain values for temporary reference recordings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReferenceState(str, Enum):
    UPLOADED = "uploaded"
    VALIDATED = "validated"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    DELETED = "deleted"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True)
class ReferenceMetadata:
    container: str
    sample_rate_hz: int
    channels: int
    duration_ms: int


@dataclass(frozen=True)
class ReferenceRecording:
    id: str
    model_id: str
    relative_path: str
    byte_size: int
    sha256: str
    container: str
    sample_rate_hz: int
    channels: int
    duration_ms: int
    transcript_present: bool
    state: ReferenceState
    expires_at: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReferenceHandle:
    recording: ReferenceRecording
    transcript: str | None


class InvalidReferenceTransitionError(LookupError, RuntimeError):
    """Raised when a reference lifecycle transition is not allowed."""


class ReferenceRecoveryRequiredError(RuntimeError):
    """Raised when a restart lost the in-memory transcript handoff."""


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds the Core staging limit."""


class CleanupFailedError(RuntimeError):
    """Raised when safe reference cleanup cannot be completed."""


class ReferenceInUseError(RuntimeError):
    """Raised when a Generation Job still owns a reference recording."""
