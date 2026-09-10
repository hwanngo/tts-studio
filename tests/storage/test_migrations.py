import shutil
import sqlite3
from pathlib import Path

import pytest

from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError


def _layout(tmp_path: Path) -> StorageLayout:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    return layout


def _apply_schema(connection: sqlite3.Connection, target_version: int) -> None:
    migrations = Path(__file__).parents[2] / "src/tts_studio/storage/migrations"
    for migration in sorted(migrations.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(migration.name.split("_", maxsplit=1)[0])
        if version > target_version:
            break
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute(f"PRAGMA user_version = {version}")


def test_fresh_database_applies_model_management_schema(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert version == 12
    assert {
        "engine_installations",
        "model_installations",
        "download_jobs",
        "model_events",
        "model_event_cursor",
        "generation_jobs",
        "audio_artifacts",
        "reference_recordings",
    } <= tables
    assert {
        "idx_download_jobs_repository_id",
        "idx_download_jobs_state",
        "idx_model_installations_engine",
        "idx_generation_jobs_state",
        "idx_generation_jobs_model",
        "idx_audio_artifacts_history",
        "idx_reference_recordings_state_expires",
    } <= indexes


def test_phase_one_database_upgrades_without_losing_existing_data(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with sqlite3.connect(layout.database_path) as connection:
        connection.execute("CREATE TABLE phase_one_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO phase_one_marker VALUES ('preserved')")
        connection.execute("PRAGMA user_version = 1")

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert connection.execute("SELECT value FROM phase_one_marker").fetchone()[0] == "preserved"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'download_jobs'"
            ).fetchone()[0]
            == 1
        )


def test_phase_two_database_upgrades_to_durable_events(tmp_path: Path) -> None:
    """Catch event tables being added only to an already-applied migration."""
    layout = _layout(tmp_path)
    with sqlite3.connect(layout.database_path) as connection:
        connection.execute("CREATE TABLE phase_two_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO phase_two_marker VALUES ('preserved')")
        connection.execute("PRAGMA user_version = 2")

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        marker = connection.execute("SELECT value FROM phase_two_marker").fetchone()[0]
        cursor = connection.execute(
            "SELECT last_event_id FROM model_event_cursor WHERE singleton = 1"
        ).fetchone()[0]

        assert version == 12
    assert {"model_events", "model_event_cursor"} <= tables
    assert marker == "preserved"
    assert cursor == 0


def test_phase_two_development_database_keeps_existing_event_cursor(tmp_path: Path) -> None:
    """Catch migration 003 breaking databases created while events lived in migration 002."""
    layout = _layout(tmp_path)
    with sqlite3.connect(layout.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE model_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                download_id TEXT,
                payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX idx_model_events_download_id_id ON model_events(download_id, id);
            CREATE TABLE model_event_cursor (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                last_event_id INTEGER NOT NULL CHECK (last_event_id >= 0)
            );
            INSERT INTO model_event_cursor(singleton, last_event_id) VALUES (1, 7);
            PRAGMA user_version = 2;
            """
        )

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        cursor = connection.execute(
            "SELECT last_event_id FROM model_event_cursor WHERE singleton = 1"
        ).fetchone()[0]

        assert version == 12
    assert cursor == 7


def test_schema_version_nine_upgrades_settings_without_losing_existing_data(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with sqlite3.connect(layout.database_path) as connection:
        _apply_schema(connection, 9)
        connection.execute(
            """INSERT INTO engine_installations (
                id, engine_id, version, command_json, working_directory,
                environment_json, capabilities_json, lifecycle_state, created_at, updated_at
            ) VALUES ('engine-1', 'provider-engine', '1.0', '["run"]', 'workers', '{}', '{}', 'ready', 'now', 'now')"""
        )
        connection.execute(
            """INSERT INTO saved_voices (
                id, model_id, label, relative_path, transcript, created_at, updated_at
            ) VALUES ('voice-1', 'model-1', 'Saved voice', 'voices/voice-1.wav', 'hello', 'now', 'now')"""
        )
        connection.execute(
            """INSERT INTO provider_profiles (
                id, kind, label, base_url, model, api_key_env, created_at, updated_at
            ) VALUES ('provider-1', 'openai_compatible', 'Provider', 'https://example.test',
                      'tts-1', 'PROVIDER_TOKEN', 'now', 'now')"""
        )
        connection.executescript(
            """INSERT INTO generation_jobs (
                id, model_id, engine_id, voice_id, saved_voice_id, provider_id, text,
                retain_artifact, state, correlation_id, created_at, updated_at
            ) VALUES ('saved-job', 'model-1', 'provider-engine', NULL, 'voice-1', NULL,
                      'saved voice text', 1, 'queued', 'saved-corr', 'now', 'now');
            INSERT INTO generation_jobs (
                id, model_id, engine_id, voice_id, saved_voice_id, provider_id, text,
                retain_artifact, state, correlation_id, created_at, updated_at
            ) VALUES ('provider-job', 'provider:provider-1', 'provider-engine', 'alloy', NULL,
                      'provider-1', 'provider text', 1, 'queued', 'provider-corr', 'now', 'now');"""
        )

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert connection.execute(
            "SELECT engine_id, lifecycle_state FROM engine_installations WHERE id = 'engine-1'"
        ).fetchone() == ("provider-engine", "ready")
        assert connection.execute(
            "SELECT saved_voice_id, provider_id, text FROM generation_jobs WHERE id = 'saved-job'"
        ).fetchone() == ("voice-1", None, "saved voice text")
        assert connection.execute(
            "SELECT voice_id, saved_voice_id, provider_id, text FROM generation_jobs "
            "WHERE id = 'provider-job'"
        ).fetchone() == ("alloy", None, "provider-1", "provider text")
        assert connection.execute(
            "SELECT label, transcript FROM saved_voices WHERE id = 'voice-1'"
        ).fetchone() == ("Saved voice", "hello")
        assert connection.execute(
            "SELECT api_key_env FROM provider_profiles WHERE id = 'provider-1'"
        ).fetchone() == ("PROVIDER_TOKEN",)
        assert (
            connection.execute("SELECT retain_audio_by_default FROM core_settings").fetchone()[0]
            == 1
        )


def test_schema_version_ten_upgrades_queued_jobs_with_nullable_generation_options(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with sqlite3.connect(layout.database_path) as connection:
        _apply_schema(connection, 10)
        connection.execute(
            """INSERT INTO generation_jobs (
                id, model_id, engine_id, voice_id, text, retain_artifact, state,
                correlation_id, created_at, updated_at
            ) VALUES ('queued-job', 'model-1', 'engine-1', 'voice-1', 'preserved text',
                      1, 'queued', 'corr-1', 'now', 'now')"""
        )

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert connection.execute(
            "SELECT id, state, text, speed, pitch, volume FROM generation_jobs "
            "WHERE id = 'queued-job'"
        ).fetchone() == (
            "queued-job",
            "queued",
            "preserved text",
            None,
            None,
            None,
        )


def test_corrupt_newest_migration_rolls_back_without_losing_schema_v10_data(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    source = Path(__file__).parents[2] / "src/tts_studio/storage/migrations"
    migrations = tmp_path / "migrations"
    shutil.copytree(source, migrations)
    newest = migrations / "011_generation_options.sql"
    newest.write_text(
        newest.read_text(encoding="utf-8") + "\nTHIS IS INVALID SQL;\n",
        encoding="utf-8",
    )
    with sqlite3.connect(layout.database_path) as connection:
        _apply_schema(connection, 10)
        connection.execute(
            """INSERT INTO generation_jobs (
                id, model_id, engine_id, voice_id, text, retain_artifact, state,
                correlation_id, created_at, updated_at
            ) VALUES ('queued-job', 'model-1', 'engine-1', 'voice-1', 'preserved text',
                      1, 'queued', 'corr-1', 'now', 'now')"""
        )

    with pytest.raises(sqlite3.Error):
        Database(layout.database_path, migrations_directory=migrations).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute(
            "SELECT id, state, text FROM generation_jobs WHERE id = 'queued-job'"
        ).fetchone() == ("queued-job", "queued", "preserved text")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(generation_jobs)")
        }
        assert {"speed", "pitch", "volume"}.isdisjoint(columns)


def test_missing_intermediate_migration_fails_before_version_advancement(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source = Path(__file__).parents[2] / "src/tts_studio/storage/migrations"
    migrations = tmp_path / "migrations"
    shutil.copytree(source, migrations)
    (migrations / "006_generation_reference_source.sql").unlink()

    with pytest.raises(RuntimeError, match="migration versions must be contiguous"):
        Database(layout.database_path, migrations_directory=migrations).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_migration_012_directly_creates_alignment_schema_from_version_11(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with sqlite3.connect(layout.database_path) as connection:
        _apply_schema(connection, 11)
        connection.execute(
            "INSERT INTO generation_jobs (id, model_id, engine_id, voice_id, text, "
            "retain_artifact, state, correlation_id, created_at, updated_at) "
            "VALUES ('job', 'model', 'engine', 'voice', 'text', 1, 'completed', 'corr', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO audio_artifacts (id, job_id, path, byte_size, sha256, sample_rate, "
            "channel_count, frame_count, created_at, retained_at) VALUES "
            "('artifact', 'job', 'audio/job.wav', 1, 'sha', 48000, 1, 1, 'now', 'now')"
        )

    Database(layout.database_path).migrate()

    with sqlite3.connect(layout.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert [row[1] for row in connection.execute("PRAGMA table_info(generation_alignments)")] == [
            "job_id",
            "artifact_id",
            "state",
            "result_json",
            "error_json",
            "created_at",
            "updated_at",
        ]
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name = "
            "'idx_audio_artifacts_job_id_id'"
        ).fetchone() == (
            "idx_audio_artifacts_job_id_id",
            "CREATE UNIQUE INDEX idx_audio_artifacts_job_id_id ON audio_artifacts(job_id, id)",
        )
        assert connection.execute(
            'SELECT id, seq, "table", "from", "to", on_update, on_delete '
            "FROM pragma_foreign_key_list('generation_alignments') ORDER BY id"
        ).fetchall() == [
            (0, 0, "audio_artifacts", "job_id", "job_id", "NO ACTION", "CASCADE"),
            (0, 1, "audio_artifacts", "artifact_id", "id", "NO ACTION", "CASCADE"),
            (1, 0, "generation_jobs", "job_id", "id", "NO ACTION", "CASCADE"),
        ]
        connection.execute(
            "INSERT INTO generation_alignments (job_id, artifact_id, state, created_at, updated_at) "
            "VALUES ('job', 'artifact', 'queued', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO generation_alignments (job_id, artifact_id, state, created_at, updated_at) "
                "VALUES ('job', 'artifact', 'unknown', 'now', 'now')"
            )
        connection.execute(
            "INSERT INTO generation_jobs (id, model_id, engine_id, voice_id, text, "
            "retain_artifact, state, correlation_id, created_at, updated_at) "
            "VALUES ('job-invalid', 'model', 'engine', 'voice', 'text', 1, 'completed', "
            "'corr-invalid', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO audio_artifacts (id, job_id, path, byte_size, sha256, sample_rate, "
            "channel_count, frame_count, created_at, retained_at) VALUES "
            "('artifact-invalid', 'job-invalid', 'audio/invalid.wav', 1, 'sha', 48000, 1, 1, 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO generation_alignments (job_id, artifact_id, state, result_json, "
                "created_at, updated_at) VALUES ('job-invalid', 'artifact-invalid', 'completed', NULL, 'now', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO generation_alignments (job_id, artifact_id, state, result_json, "
                "created_at, updated_at) VALUES ('job-invalid', 'artifact-invalid', 'queued', '{}', 'now', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO generation_alignments (job_id, artifact_id, state, created_at, updated_at) "
                "VALUES ('missing-job', 'artifact', 'queued', 'now', 'now')"
            )
        connection.execute(
            "INSERT INTO generation_jobs (id, model_id, engine_id, voice_id, text, "
            "retain_artifact, state, correlation_id, created_at, updated_at) VALUES "
            "('job-artifact-cascade', 'model', 'engine', 'voice', 'text', 1, 'completed', "
            "'corr-artifact-cascade', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO audio_artifacts (id, job_id, path, byte_size, sha256, sample_rate, "
            "channel_count, frame_count, created_at, retained_at) VALUES "
            "('artifact-cascade', 'job-artifact-cascade', 'audio/cascade.wav', 1, 'sha', 48000, 1, 1, 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO generation_alignments (job_id, artifact_id, state, created_at, updated_at) "
            "VALUES ('job-artifact-cascade', 'artifact-cascade', 'queued', 'now', 'now')"
        )
        connection.execute("DELETE FROM audio_artifacts WHERE id = 'artifact-cascade'")
        assert connection.execute(
            "SELECT COUNT(*) FROM generation_alignments WHERE job_id = 'job-artifact-cascade'"
        ).fetchone()[0] == 0
        connection.execute("DELETE FROM generation_jobs WHERE id = 'job'")
        assert connection.execute(
            "SELECT COUNT(*) FROM generation_alignments WHERE job_id = 'job'"
        ).fetchone()[0] == 0


def test_database_connection_boundary_enforces_foreign_keys(tmp_path: Path) -> None:
    database = Database(_layout(tmp_path).database_path)
    database.migrate()

    with database.transaction() as connection:
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert enabled == 1


def test_generation_schema_enforces_states_and_managed_audio_paths(tmp_path: Path) -> None:
    database = Database(_layout(tmp_path).database_path)
    database.migrate()

    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO generation_jobs (
                    id, model_id, engine_id, voice_id, text, retain_artifact, state,
                    correlation_id, created_at, updated_at
                ) VALUES ('job', 'model', 'engine', 'voice', 'text', 1, 'unknown', 'corr', 'now', 'now')"""
            )
        connection.execute(
            """INSERT INTO generation_jobs (
                id, model_id, engine_id, voice_id, text, retain_artifact, state,
                correlation_id, created_at, updated_at
            ) VALUES ('job', 'model', 'engine', 'voice', 'text', 1, 'completed', 'corr', 'now', 'now')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO audio_artifacts (
                    id, job_id, path, byte_size, sha256, sample_rate, channel_count,
                    frame_count, created_at, retained_at
                ) VALUES ('artifact', 'job', '/tmp/audio.wav', 1, 'sha', 48000, 1, 1, 'now', 'now')"""
            )


@pytest.mark.parametrize(
    "path",
    [
        r"audio\escape.wav",
        r"audio/sub\escape.wav",
        "audio//escape.wav",
        "audio/sub//escape.wav",
        "audio/../escape.wav",
        "audio/./escape.wav",
        "audio/.",
        "audio/..",
        "audio/",
        "audio/sub/../escape.wav",
        "audio/sub/./escape.wav",
        "audio/sub/..",
        "audio/sub/.",
        "audio/escape.wav/",
        "AUDIO/escape.wav",
    ],
)
def test_generation_schema_rejects_non_normalized_artifact_paths(tmp_path: Path, path: str) -> None:
    database = Database(_layout(tmp_path).database_path)
    database.migrate()

    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO generation_jobs (
                id, model_id, engine_id, voice_id, text, retain_artifact, state,
                correlation_id, created_at, updated_at
            ) VALUES ('job', 'model', 'engine', 'voice', 'text', 1, 'completed', 'corr', 'now', 'now')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO audio_artifacts (
                    id, job_id, path, byte_size, sha256, sample_rate, channel_count,
                    frame_count, created_at, retained_at
                ) VALUES ('artifact', 'job', ?, 1, 'sha', 48000, 1, 1, 'now', 'now')""",
                (path,),
            )


def test_database_rejects_a_symlinked_sqlite_file(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    outside = tmp_path / "outside.sqlite3"
    outside.touch()
    layout.database_path.symlink_to(outside)

    with pytest.raises(UnsafeStoragePathError, match="symbolic link"):
        Database(layout.database_path).migrate()


def test_reference_schema_has_only_safe_metadata_and_exact_states(tmp_path: Path) -> None:
    database = Database(_layout(tmp_path).database_path)
    database.migrate()

    with sqlite3.connect(database.path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(reference_recordings)")]
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reference_recordings'"
        ).fetchone()[0]

    assert columns == [
        "id",
        "model_id",
        "relative_path",
        "byte_size",
        "sha256",
        "container",
        "sample_rate_hz",
        "channels",
        "duration_ms",
        "transcript_present",
        "state",
        "expires_at",
        "created_at",
        "updated_at",
    ]
    assert "cleanup_failed" in sql
    assert "uploaded" in sql and "validated" in sql and "consumed" in sql
    assert "transcript" not in {column for column in columns if column != "transcript_present"}
    assert "bytes" not in " ".join(columns)
