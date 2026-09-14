from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Row

from tts_studio.storage.db import Database
from tts_studio.voices.domain import SavedVoice


class SavedVoiceNotFoundError(LookupError):
    pass


class SavedVoiceRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        *,
        voice_id: str,
        model_id: str,
        label: str,
        relative_path: str,
        transcript: str | None,
    ) -> SavedVoice:
        now = _utc_now()
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT INTO saved_voices
                   (id, model_id, label, relative_path, transcript, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (voice_id, model_id, label, relative_path, transcript, now, now),
            )
            row = connection.execute(
                "SELECT * FROM saved_voices WHERE id = ?", (voice_id,)
            ).fetchone()
        return _voice(_required(row))

    def get(self, voice_id: str) -> SavedVoice:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM saved_voices WHERE id = ?", (voice_id,)
            ).fetchone()
        if row is None:
            raise SavedVoiceNotFoundError(voice_id)
        return _voice(row)

    def list_for_model(self, model_id: str) -> tuple[SavedVoice, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM saved_voices WHERE model_id = ? ORDER BY created_at, id", (model_id,)
            ).fetchall()
        return tuple(_voice(row) for row in rows)

    def delete(self, voice_id: str) -> SavedVoice:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM saved_voices WHERE id = ?", (voice_id,)
            ).fetchone()
            voice = _voice(_required(row))
            connection.execute("DELETE FROM saved_voices WHERE id = ?", (voice_id,))
        return voice


def _voice(row: Row) -> SavedVoice:
    return SavedVoice(
        id=row["id"],
        model_id=row["model_id"],
        label=row["label"],
        relative_path=row["relative_path"],
        transcript=row["transcript"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _required(row: Row | None) -> Row:
    if row is None:
        raise SavedVoiceNotFoundError("saved Voice was not found")
    return row


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
