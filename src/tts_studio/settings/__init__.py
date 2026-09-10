"""Core settings persistence and domain values."""

from tts_studio.settings.domain import CoreSettings
from tts_studio.settings.repository import CoreSettingsRepository
from tts_studio.settings.service import (
    RetentionCleanupError,
    RetentionClearResult,
    RetentionIssue,
    RetentionSummary,
    SettingsPatch,
    SettingsService,
    SettingsValidationError,
    UnsupportedSettingsOperationError,
)

__all__ = [
    "CoreSettings",
    "CoreSettingsRepository",
    "RetentionCleanupError",
    "RetentionClearResult",
    "RetentionIssue",
    "RetentionSummary",
    "SettingsPatch",
    "SettingsService",
    "SettingsValidationError",
    "UnsupportedSettingsOperationError",
]
