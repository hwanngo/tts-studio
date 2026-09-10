"""Core policy for user settings and retained generation artifacts."""

from __future__ import annotations

from dataclasses import dataclass

from tts_studio.generation.service import GenerationService
from tts_studio.settings.domain import CoreSettings
from tts_studio.settings.repository import CoreSettingsRepository, InvalidSettingsDataError

_UNSET = object()


class SettingsValidationError(ValueError):
    """Raised when a settings patch is invalid."""


class RetentionCleanupError(RuntimeError):
    """Raised for unrecoverable retention cleanup failures."""


class UnsupportedSettingsOperationError(RuntimeError):
    """Raised when a settings operation lacks a Core-owned capability."""


@dataclass(frozen=True)
class SettingsPatch:
    """Partial settings update; omitted fields retain their current values."""

    retain_audio_by_default: bool | object = _UNSET
    artifact_max_age_days: int | None | object = _UNSET
    artifact_max_storage_bytes: int | None | object = _UNSET
    api_token_env: str | None | object = _UNSET


@dataclass(frozen=True)
class RetentionSummary:
    retained_count: int
    retained_bytes: int
    max_age_days: int | None
    max_storage_bytes: int | None


@dataclass(frozen=True)
class RetentionIssue:
    artifact_id: str
    message: str


@dataclass(frozen=True)
class RetentionClearResult:
    deleted: int
    skipped: int
    failed: int
    issues: tuple[RetentionIssue, ...] = ()


class SettingsService:
    """Apply settings policy while keeping SQLite and managed-file work in Core."""

    def __init__(
        self, repository: CoreSettingsRepository, generation_service: GenerationService
    ) -> None:
        if not isinstance(generation_service, GenerationService):
            raise UnsupportedSettingsOperationError("retention operations are not available")
        self._repository = repository
        self._artifact_owner = generation_service

    def get_settings(self) -> CoreSettings:
        return self._repository.get()

    def update_settings(self, patch: SettingsPatch) -> CoreSettings:
        if not isinstance(patch, SettingsPatch):
            raise SettingsValidationError("settings patch has an unsupported type")
        values = {
            field: value
            for field, value in (
                ("retain_audio_by_default", patch.retain_audio_by_default),
                ("artifact_max_age_days", patch.artifact_max_age_days),
                ("artifact_max_storage_bytes", patch.artifact_max_storage_bytes),
                ("api_token_env", patch.api_token_env),
            )
            if value is not _UNSET
        }
        try:
            return self._repository.update(**values)
        except (InvalidSettingsDataError, TypeError, ValueError) as error:
            raise SettingsValidationError(str(error)) from error

    def retention_summary(self) -> RetentionSummary:
        settings = self.get_settings()
        artifacts = self._artifact_owner.list_history()
        return RetentionSummary(
            retained_count=len(artifacts),
            retained_bytes=sum(artifact.byte_size for artifact in artifacts),
            max_age_days=settings.artifact_max_age_days,
            max_storage_bytes=settings.artifact_max_storage_bytes,
        )

    def clear_retention(self) -> RetentionClearResult:
        selected = tuple(self._artifact_owner.list_history())
        deleted = skipped = failed = 0
        issues: list[RetentionIssue] = []
        for artifact in selected:
            try:
                removed = self._artifact_owner.delete_artifact(artifact.id)
            except Exception:  # noqa: BLE001 - normalize filesystem details at Core boundary
                failed += 1
                issues.append(
                    RetentionIssue(artifact.id, "retained artifact could not be safely deleted")
                )
                continue
            if removed:
                deleted += 1
            else:
                skipped += 1
                issues.append(
                    RetentionIssue(artifact.id, "retained artifact was not safely resolvable")
                )
        return RetentionClearResult(deleted, skipped, failed, tuple(issues))

    def apply_generation_defaults(self, retain_artifact: bool | None = None) -> bool:
        """Return the configured default unless a generation explicitly chose a value."""
        if retain_artifact is not None:
            if not isinstance(retain_artifact, bool):
                raise SettingsValidationError("retain_artifact must be a boolean")
            return retain_artifact
        return self.get_settings().retain_audio_by_default
