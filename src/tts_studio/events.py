"""Durable, bounded model-management events owned by the Core."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from tts_studio.storage.db import Database

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

_EVENT_TYPE = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_WINDOWS_UNC_PATH = re.compile(r"\\\\[^\\/\s]+[\\/][^\\/\s]+")
_FORWARD_SLASH_UNC_PATH = re.compile(r"(?<![A-Za-z0-9/:])//[^/\s]+/[^/\s]+")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9/])/(?!/)[^\s]+")
_URI_PATH = re.compile(r"(?:file|socket|unix):/{1,3}", re.IGNORECASE)
_BEARER_SECRET = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_HF_SECRET = re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "command",
        "environment",
        "secret",
        "stack",
        "token",
        "traceback",
        "working_directory",
    }
)
_TERMINAL_EVENT_TYPES = frozenset(
    {
        "model.activated",
        "download.cancelled",
        "download.failed",
        "generation.completed",
        "generation.cancelled",
        "generation.failed",
    }
)
_EVENT_STREAM_KINDS = frozenset({"download", "generation"})
_AUDIO_PAYLOAD_KEYS = frozenset(
    {
        "audio",
        "audio_bytes",
        "audio_data",
        "audio_chunk",
        "audio_chunks",
        "pcm",
        "pcm_bytes",
        "pcm_data",
        "pcm_chunk",
        "pcm_chunks",
        "wav",
        "wav_bytes",
        "wav_data",
    }
)


class UnsafeEventPayloadError(ValueError):
    """Raised before sensitive or non-JSON event data can be persisted."""


@dataclass(frozen=True)
class DurableEvent:
    id: int
    event_type: str
    stream_kind: str | None
    stream_id: str | None
    payload: JsonObject
    created_at: str

    @property
    def download_id(self) -> str | None:
        """Compatibility view for existing Download Job consumers."""
        return self.stream_id if self.stream_kind == "download" else None

    @property
    def terminal(self) -> bool:
        return self.event_type in _TERMINAL_EVENT_TYPES

    def public_data(self) -> JsonObject:
        return {**self.payload, "sequence_id": self.id}


@dataclass(frozen=True)
class EventBatch:
    events: tuple[DurableEvent, ...]
    cursor_expired: bool
    reset_cursor: int


class EventStore:
    """Persist ordered events and prune them without per-client in-memory queues."""

    def __init__(self, database: Database, *, retention_limit: int = 1_000) -> None:
        if not isinstance(retention_limit, int) or isinstance(retention_limit, bool):
            raise TypeError("event retention limit must be an integer")
        if retention_limit < 1:
            raise ValueError("event retention limit must be positive")
        self._database = database
        self._retention_limit = retention_limit

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        download_id: str | None = None,
        stream_kind: str | None = None,
        stream_id: str | None = None,
    ) -> DurableEvent:
        safe_type = _safe_event_type(event_type)
        safe_payload = _safe_object(payload)
        payload_job_id = safe_payload.get("job_id")
        if (stream_kind is None) != (stream_id is None):
            raise UnsafeEventPayloadError("stream_kind and stream_id must be supplied together")
        is_generation_event = safe_type.startswith("generation.")
        inferred_kind: str | None = event_type.split(".", maxsplit=1)[0]
        if inferred_kind not in _EVENT_STREAM_KINDS:
            # Existing model events used job_id as a Download Job identity.
            inferred_kind = "download" if event_type.startswith("model.") else None
        if download_id is not None:
            if stream_kind is not None and stream_kind != "download":
                raise UnsafeEventPayloadError("download_id conflicts with stream_kind")
            stream_kind = "download"
            stream_id = download_id
        if is_generation_event and (stream_kind != "generation" or stream_id is None):
            raise UnsafeEventPayloadError(
                "generation events require an explicit generation stream identity"
            )
        if (
            not is_generation_event
            and stream_kind is None
            and inferred_kind is not None
            and isinstance(payload_job_id, str)
        ):
            stream_kind = inferred_kind
            stream_id = payload_job_id
        if (
            isinstance(payload_job_id, str)
            and stream_id is not None
            and stream_id != payload_job_id
        ):
            raise UnsafeEventPayloadError("stream_id does not match payload job_id")
        safe_kind = _safe_stream_kind(stream_kind)
        safe_stream_id = _safe_identifier(stream_id, "stream_id")
        if safe_kind is not None and safe_stream_id is None:
            raise UnsafeEventPayloadError("stream_id is required for a typed stream")
        if is_generation_event and _contains_audio_payload(safe_payload):
            raise UnsafeEventPayloadError("generation event payloads cannot contain audio bytes")
        encoded = json.dumps(
            safe_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        created_at = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO model_events(event_type, download_id, stream_kind, stream_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    safe_type,
                    safe_stream_id if safe_kind == "download" else None,
                    safe_kind,
                    safe_stream_id,
                    encoded,
                    created_at,
                ),
            )
            event_id = cast(int, cursor.lastrowid)
            connection.execute(
                "UPDATE model_event_cursor SET last_event_id = ? WHERE singleton = 1",
                (event_id,),
            )
            connection.execute(
                """
                DELETE FROM model_events
                WHERE id < COALESCE(
                    (
                        SELECT id FROM model_events
                        ORDER BY id DESC
                        LIMIT 1 OFFSET ?
                    ),
                    0
                )
                """,
                (self._retention_limit - 1,),
            )
        return DurableEvent(
            id=event_id,
            event_type=safe_type,
            stream_kind=safe_kind,
            stream_id=safe_stream_id,
            payload=safe_payload,
            created_at=created_at,
        )

    def append_progress(
        self,
        *,
        job_id: str,
        phase: str,
        bytes_downloaded: int,
        total_bytes: int | None,
        message: str,
    ) -> DurableEvent:
        payload = build_download_progress_payload(
            job_id=job_id,
            phase=phase,
            bytes_downloaded=bytes_downloaded,
            total_bytes=total_bytes,
            message=message,
        )
        return self.append("download.progress", payload, download_id=job_id)

    def read_after(
        self,
        cursor: int,
        *,
        download_id: str | None = None,
        stream_kind: str | None = None,
        stream_id: str | None = None,
        limit: int = 100,
    ) -> EventBatch:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("event cursor must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1_000:
            raise ValueError("event batch limit must be between 1 and 1000")
        if (stream_kind is None) != (stream_id is None):
            raise UnsafeEventPayloadError("stream_kind and stream_id must be supplied together")
        if download_id is not None:
            if stream_kind is not None and (stream_kind != "download" or stream_id != download_id):
                raise UnsafeEventPayloadError("download_id conflicts with stream identity")
            stream_kind, stream_id = "download", download_id
        safe_kind = _safe_stream_kind(stream_kind)
        safe_stream_id = _safe_identifier(stream_id, "stream_id")
        if safe_kind is not None and safe_stream_id is None:
            raise UnsafeEventPayloadError("stream_id is required for a typed stream")
        with self._database.read() as connection:
            latest = int(
                connection.execute(
                    "SELECT last_event_id FROM model_event_cursor WHERE singleton = 1"
                ).fetchone()[0]
            )
            oldest_row = connection.execute("SELECT MIN(id) FROM model_events").fetchone()
            oldest = cast(int | None, oldest_row[0])
            expired = (oldest is not None and cursor < oldest - 1) or cursor > latest
            if expired:
                return EventBatch(events=(), cursor_expired=True, reset_cursor=latest)
            if safe_kind is None:
                rows = connection.execute(
                    "SELECT * FROM model_events WHERE id > ? ORDER BY id LIMIT ?",
                    (cursor, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM model_events WHERE id > ? AND stream_kind = ? AND stream_id = ? "
                    "ORDER BY id LIMIT ?",
                    (cursor, safe_kind, safe_stream_id, limit),
                ).fetchall()
        return EventBatch(
            events=tuple(_event_from_row(row) for row in rows),
            cursor_expired=False,
            reset_cursor=latest,
        )

    def initial_cursor(self) -> int:
        """Start a new client immediately before the oldest retained event."""
        with self._database.read() as connection:
            row = connection.execute("SELECT MIN(id) FROM model_events").fetchone()
        oldest = cast(int | None, row[0])
        return max(0, oldest - 1) if oldest is not None else 0

    def retained_count(self) -> int:
        with self._database.read() as connection:
            row = connection.execute("SELECT COUNT(*) FROM model_events").fetchone()
        return int(row[0])


def build_download_progress_payload(
    *,
    job_id: str,
    phase: str,
    bytes_downloaded: int,
    total_bytes: int | None,
    message: str,
) -> JsonObject:
    """Build the single public progress shape used by every Download Job event."""
    if (
        not isinstance(bytes_downloaded, int)
        or isinstance(bytes_downloaded, bool)
        or bytes_downloaded < 0
    ):
        raise UnsafeEventPayloadError("bytes_downloaded must be non-negative")
    if total_bytes is not None and (
        not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes < bytes_downloaded
    ):
        raise UnsafeEventPayloadError("total_bytes cannot be below downloaded bytes")
    payload: JsonObject = {
        "job_id": job_id,
        "phase": phase,
        "bytes_downloaded": bytes_downloaded,
        "message": message,
    }
    if total_bytes is not None:
        payload["total_bytes"] = total_bytes
        payload["percentage"] = (
            100.0 if total_bytes == 0 else round(bytes_downloaded * 100 / total_bytes, 2)
        )
    return payload


def _event_from_row(row: Any) -> DurableEvent:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise TypeError("persisted model event payload is not an object")
    return DurableEvent(
        id=row["id"],
        event_type=row["event_type"],
        stream_kind=row["stream_kind"],
        stream_id=row["stream_id"],
        payload=cast(JsonObject, payload),
        created_at=row["created_at"],
    )


def _safe_stream_kind(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _EVENT_STREAM_KINDS:
        raise UnsafeEventPayloadError("stream_kind is invalid")
    return value


def _contains_audio_payload(value: JsonValue) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _AUDIO_PAYLOAD_KEYS or _contains_audio_payload(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_audio_payload(item) for item in value)
    return False


def _safe_event_type(value: str) -> str:
    if not isinstance(value, str) or _EVENT_TYPE.fullmatch(value) is None:
        raise UnsafeEventPayloadError("event type is invalid")
    return value


def _safe_identifier(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in "\r\n\0")
    ):
        raise UnsafeEventPayloadError(f"{field} is invalid")
    if _unsafe_text(value):
        raise UnsafeEventPayloadError(f"{field} contains unsafe data")
    return value


def _safe_object(value: Mapping[str, Any]) -> JsonObject:
    if not isinstance(value, Mapping):
        raise UnsafeEventPayloadError("event payload must be an object")
    return {str(key): _safe_value(str(key), item) for key, item in value.items()}


def _safe_value(key: str, value: Any) -> JsonValue:
    if key.casefold() in _SENSITIVE_KEYS:
        raise UnsafeEventPayloadError("event payload contains a sensitive field")
    if isinstance(value, float) and not math.isfinite(value):
        raise UnsafeEventPayloadError("event payload contains a non-finite number")
    if value is None or isinstance(value, (bool, int, float)):
        return cast(JsonScalar, value)
    if isinstance(value, str):
        if _unsafe_text(value):
            raise UnsafeEventPayloadError("event payload contains unsafe text")
        return value
    if isinstance(value, Mapping):
        return _safe_object(value)
    if isinstance(value, (list, tuple)):
        return [_safe_value("item", item) for item in value]
    raise UnsafeEventPayloadError("event payload contains a non-JSON value")


def _unsafe_text(value: str) -> bool:
    return bool(
        _WINDOWS_ABSOLUTE_PATH.search(value)
        or _WINDOWS_UNC_PATH.search(value)
        or _FORWARD_SLASH_UNC_PATH.search(value)
        or _POSIX_ABSOLUTE_PATH.search(value)
        or _URI_PATH.search(value)
        or _BEARER_SECRET.search(value)
        or _HF_SECRET.search(value)
        or any(character in value for character in "\r\0")
    )
