"""SQLite repository for the singleton Core settings row."""

from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Row
from typing import Any

from tts_studio.settings.domain import CoreSettings
from tts_studio.storage.db import Database

_UNSET = object()


class InvalidSettingsDataError(ValueError):
    """Raised when settings cannot be safely persisted."""


class CoreSettingsRepository:
    """The sole persistence boundary for user-level Core settings."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self) -> CoreSettings:
        with self._database.read() as connection:
            row = connection.execute("SELECT * FROM core_settings WHERE singleton = 1").fetchone()
        if row is None:
            return self.reset_or_initialize()
        return _settings(row)

    def update(
        self,
        *,
        retain_audio_by_default: bool | object = _UNSET,
        artifact_max_age_days: int | None | object = _UNSET,
        artifact_max_storage_bytes: int | None | object = _UNSET,
        api_token_env: str | None | object = _UNSET,
    ) -> CoreSettings:
        supplied = {
            "retain_audio_by_default": retain_audio_by_default,
            "artifact_max_age_days": artifact_max_age_days,
            "artifact_max_storage_bytes": artifact_max_storage_bytes,
            "api_token_env": api_token_env,
        }
        if not any(value is not _UNSET for value in supplied.values()):
            return self.get()
        with self._database.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM core_settings WHERE singleton = 1"
            ).fetchone()
            current = (
                _settings(current_row)
                if current_row is not None
                else CoreSettings(True, None, None, None)
            )
            values = {
                field: getattr(current, field) if value is _UNSET else value
                for field, value in supplied.items()
            }
            candidate = _validated(values)
            connection.execute(
                """INSERT INTO core_settings (
                    singleton, retain_audio_by_default, artifact_max_age_days,
                    artifact_max_storage_bytes, api_token_env, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    retain_audio_by_default = excluded.retain_audio_by_default,
                    artifact_max_age_days = excluded.artifact_max_age_days,
                    artifact_max_storage_bytes = excluded.artifact_max_storage_bytes,
                    api_token_env = excluded.api_token_env,
                    updated_at = excluded.updated_at""",
                (
                    int(candidate.retain_audio_by_default),
                    candidate.artifact_max_age_days,
                    candidate.artifact_max_storage_bytes,
                    candidate.api_token_env,
                    _utc_now(),
                ),
            )
            row = connection.execute("SELECT * FROM core_settings WHERE singleton = 1").fetchone()
            if row is None:
                raise RuntimeError("settings write did not produce its expected record")
        return _settings(row)

    def reset_or_initialize(self) -> CoreSettings:
        """Restore migration defaults, or create the singleton if absent."""
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_settings (
                    singleton, retain_audio_by_default, artifact_max_age_days,
                    artifact_max_storage_bytes, api_token_env, updated_at
                ) VALUES (1, 1, NULL, NULL, NULL, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    retain_audio_by_default = 1,
                    artifact_max_age_days = NULL,
                    artifact_max_storage_bytes = NULL,
                    api_token_env = NULL,
                    updated_at = excluded.updated_at""",
                (_utc_now(),),
            )
            row = connection.execute("SELECT * FROM core_settings WHERE singleton = 1").fetchone()
            if row is None:
                raise RuntimeError("settings initialization did not produce its expected record")
        return _settings(row)


def _settings(row: Row) -> CoreSettings:
    try:
        return CoreSettings(
            retain_audio_by_default=bool(row["retain_audio_by_default"]),
            artifact_max_age_days=row["artifact_max_age_days"],
            artifact_max_storage_bytes=row["artifact_max_storage_bytes"],
            api_token_env=row["api_token_env"],
        )
    except ValueError as error:
        raise InvalidSettingsDataError("stored core settings are invalid") from error


def _validated(values: dict[str, Any]) -> CoreSettings:
    try:
        return CoreSettings(**values)
    except (TypeError, ValueError) as error:
        raise InvalidSettingsDataError(str(error)) from error


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
