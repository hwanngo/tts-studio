import sqlite3
from pathlib import Path

import pytest

from tts_studio.settings.domain import CoreSettings
from tts_studio.settings.repository import CoreSettingsRepository, InvalidSettingsDataError
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _database(tmp_path: Path) -> Database:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return database


def test_fresh_database_has_singleton_defaults_and_typed_columns(tmp_path: Path) -> None:
    database = _database(tmp_path)
    settings = CoreSettingsRepository(database).get()

    assert settings == CoreSettings(True, None, None, None)
    with sqlite3.connect(database.path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(core_settings)")]
        assert columns == [
            "singleton", "retain_audio_by_default", "artifact_max_age_days",
            "artifact_max_storage_bytes", "api_token_env", "updated_at",
        ]
        assert connection.execute("SELECT COUNT(*) FROM core_settings").fetchone()[0] == 1
        declared = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA table_info(core_settings)")
        }
        assert declared == {
            "singleton": ("INTEGER", 0, None),
            "retain_audio_by_default": ("INTEGER", 1, "1"),
            "artifact_max_age_days": ("INTEGER", 0, None),
            "artifact_max_storage_bytes": ("INTEGER", 0, None),
            "api_token_env": ("TEXT", 0, None),
            "updated_at": ("TEXT", 1, None),
        }
        schema_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'core_settings'"
        ).fetchone()[0].upper()
        assert "CHECK (SINGLETON = 1)" in schema_sql
        assert "IN (0, 1)" in schema_sql
        assert "IS NULL OR ARTIFACT_MAX_AGE_DAYS > 0" in schema_sql
        assert "IS NULL OR ARTIFACT_MAX_STORAGE_BYTES > 0" in schema_sql
        assert "JSON" not in schema_sql
        assert "BLOB" not in schema_sql
        assert not any("JSON" in name or "BLOB" in name for name in declared)


def test_update_changes_only_supplied_fields_and_supports_nullable_limits(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = CoreSettingsRepository(database)

    updated = repository.update(
        retain_audio_by_default=False,
        artifact_max_age_days=30,
        artifact_max_storage_bytes=1000,
        api_token_env="TTS_API_TOKEN",
    )
    assert updated == CoreSettings(False, 30, 1000, "TTS_API_TOKEN")
    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT retain_audio_by_default FROM core_settings"
        ).fetchone()[0] == 0

    assert repository.update(retain_audio_by_default=True) == CoreSettings(True, 30, 1000, "TTS_API_TOKEN")
    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT retain_audio_by_default FROM core_settings"
        ).fetchone()[0] == 1

    assert repository.update(artifact_max_age_days=None) == CoreSettings(True, None, 1000, "TTS_API_TOKEN")


def test_reset_or_initialize_is_idempotent_and_preserves_unrelated_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = CoreSettingsRepository(database)
    with database.transaction() as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated VALUES ('preserved')")
        connection.execute("DELETE FROM core_settings")

    assert repository.reset_or_initialize() == CoreSettings(True, None, None, None)
    assert repository.reset_or_initialize() == CoreSettings(True, None, None, None)
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "preserved"
        assert connection.execute("SELECT COUNT(*) FROM core_settings").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retain_audio_by_default", 1),
        ("artifact_max_age_days", 0),
        ("artifact_max_storage_bytes", -1),
        ("api_token_env", ""),
        ("api_token_env", "TOKEN NAME"),
        ("api_token_env", "TOKEN=VALUE"),
        ("api_token_env", "1TOKEN"),
    ],
)
def test_update_rejects_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    repository = CoreSettingsRepository(_database(tmp_path))
    with pytest.raises(InvalidSettingsDataError):
        repository.update(**{field: value})
