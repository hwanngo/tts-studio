"""SQLite repository for reference metadata and lifecycle transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Connection, Row
from uuid import uuid4

from tts_studio.references.domain import (
    InvalidReferenceTransitionError,
    ReferenceMetadata,
    ReferenceRecording,
    ReferenceState,
)
from tts_studio.storage.db import Database


class ReferenceRecordNotFoundError(LookupError):
    """Raised when a reference ID is not present."""


class ReferenceRegistry:
    """The only repository allowed to write reference metadata."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create_uploaded(
        self,
        *,
        model_id: str,
        relative_path: str,
        byte_size: int,
        sha256: str,
        transcript_present: bool,
        expires_at: str,
        now: str | None = None,
        reference_id: str | None = None,
    ) -> ReferenceRecording:
        identifier = reference_id or str(uuid4())
        timestamp = now or _utc_now()
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT INTO reference_recordings
                (id, model_id, relative_path, byte_size, sha256, container,
                 sample_rate_hz, channels, duration_ms, transcript_present, state,
                 expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '', 0, 0, 0, ?, 'uploaded', ?, ?, ?)""",
                (
                    identifier,
                    model_id,
                    relative_path,
                    byte_size,
                    sha256,
                    int(transcript_present),
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM reference_recordings WHERE id = ?", (identifier,)
            ).fetchone()
        return _recording(_required_row(row))

    def get(self, reference_id: str) -> ReferenceRecording:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM reference_recordings WHERE id = ?", (reference_id,)
            ).fetchone()
        if row is None:
            raise ReferenceRecordNotFoundError(
                f"Reference Recording {reference_id!r} was not found"
            )
        return _recording(row)

    def list_all(self) -> tuple[ReferenceRecording, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM reference_recordings ORDER BY created_at, id"
            ).fetchall()
        return tuple(_recording(row) for row in rows)

    def mark_validated(
        self, reference_id: str, metadata: ReferenceMetadata, *, now: str | None = None
    ) -> ReferenceRecording:
        timestamp = now or _utc_now()
        with self._database.transaction() as connection:
            current = _recording_or_missing(connection, reference_id)
            if current.state is not ReferenceState.UPLOADED:
                raise InvalidReferenceTransitionError(
                    f"reference cannot be validated from {current.state.value}"
                )
            connection.execute(
                """UPDATE reference_recordings SET container = ?, sample_rate_hz = ?,
                channels = ?, duration_ms = ?, state = 'validated', updated_at = ? WHERE id = ?""",
                (
                    metadata.container,
                    metadata.sample_rate_hz,
                    metadata.channels,
                    metadata.duration_ms,
                    timestamp,
                    reference_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM reference_recordings WHERE id = ?", (reference_id,)
            ).fetchone()
        return _recording(_required_row(row))

    def claim_for_generation(
        self, reference_id: str, *, now: str | None = None
    ) -> ReferenceRecording:
        timestamp = now or _utc_now()
        with self._database.transaction() as connection:
            current = _recording_or_missing(connection, reference_id)
            if current.state is not ReferenceState.VALIDATED:
                raise InvalidReferenceTransitionError(
                    f"reference cannot be claimed from {current.state.value}"
                )
            if _parse_time(current.expires_at) <= _parse_time(timestamp):
                connection.execute(
                    "UPDATE reference_recordings SET state = 'expired', updated_at = ? WHERE id = ?",
                    (timestamp, reference_id),
                )
                raise InvalidReferenceTransitionError("reference has expired")
            connection.execute(
                "UPDATE reference_recordings SET state = 'consumed', updated_at = ? WHERE id = ?",
                (timestamp, reference_id),
            )
            row = connection.execute(
                "SELECT * FROM reference_recordings WHERE id = ?", (reference_id,)
            ).fetchone()
        return _recording(_required_row(row))

    def expire(self, *, now: str | None = None) -> tuple[ReferenceRecording, ...]:
        timestamp = now or _utc_now()
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM reference_recordings
                WHERE state IN ('uploaded', 'validated') AND expires_at <= ?
                ORDER BY expires_at, id""",
                (timestamp,),
            ).fetchall()
            connection.executemany(
                "UPDATE reference_recordings SET state = 'expired', updated_at = ? WHERE id = ?",
                ((timestamp, row["id"]) for row in rows),
            )
        return tuple(_recording(row) for row in rows)

    def delete_metadata(self, reference_id: str, *, now: str | None = None) -> None:
        with self._database.transaction() as connection:
            current = _recording_or_missing(connection, reference_id)
            if current.state not in {
                ReferenceState.UPLOADED,
                ReferenceState.VALIDATED,
                ReferenceState.EXPIRED,
                ReferenceState.CONSUMED,
                ReferenceState.DELETED,
                ReferenceState.CLEANUP_FAILED,
            }:
                raise InvalidReferenceTransitionError(
                    f"reference cannot be deleted from {current.state.value}"
                )
            connection.execute("DELETE FROM reference_recordings WHERE id = ?", (reference_id,))

    def mark_cleanup_failed(
        self, reference_id: str, *, now: str | None = None
    ) -> ReferenceRecording:
        timestamp = now or _utc_now()
        with self._database.transaction() as connection:
            _recording_or_missing(connection, reference_id)
            connection.execute(
                "UPDATE reference_recordings SET state = 'cleanup_failed', updated_at = ? WHERE id = ?",
                (timestamp, reference_id),
            )
            row = connection.execute(
                "SELECT * FROM reference_recordings WHERE id = ?", (reference_id,)
            ).fetchone()
        return _recording(_required_row(row))


def _recording(row: Row) -> ReferenceRecording:
    return ReferenceRecording(
        id=row["id"],
        model_id=row["model_id"],
        relative_path=row["relative_path"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        container=row["container"],
        sample_rate_hz=row["sample_rate_hz"],
        channels=row["channels"],
        duration_ms=row["duration_ms"],
        transcript_present=bool(row["transcript_present"]),
        state=ReferenceState(row["state"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _recording_or_missing(connection: Connection, reference_id: str) -> ReferenceRecording:
    row = connection.execute(
        "SELECT * FROM reference_recordings WHERE id = ?", (reference_id,)
    ).fetchone()
    if row is None:
        raise ReferenceRecordNotFoundError(f"Reference Recording {reference_id!r} was not found")
    return _recording(row)


def _required_row(row: Row | None) -> Row:
    if row is None:
        raise RuntimeError("reference write did not produce its expected record")
    return row


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
