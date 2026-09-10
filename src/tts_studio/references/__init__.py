"""Core-owned temporary reference recording storage."""

from tts_studio.references.domain import (
    InvalidReferenceTransitionError,
    ReferenceHandle,
    ReferenceMetadata,
    ReferenceRecording,
    ReferenceState,
)
from tts_studio.references.registry import ReferenceRegistry
from tts_studio.references.service import ReferenceService

__all__ = [
    "InvalidReferenceTransitionError",
    "ReferenceHandle",
    "ReferenceMetadata",
    "ReferenceRecording",
    "ReferenceRegistry",
    "ReferenceService",
    "ReferenceState",
]
