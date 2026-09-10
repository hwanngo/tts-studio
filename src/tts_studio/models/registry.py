"""Typed model and download persistence behind the Core SQLite boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import PurePosixPath
from sqlite3 import Cursor, Row
from typing import Any, cast
from uuid import uuid4

from tts_studio.storage.db import Database

JsonObject = dict[str, Any]
_UNSET = object()


class InvalidRegistryDataError(ValueError):
    """Raised when data cannot be persisted safely in the registry."""


class RegistryRecordNotFoundError(LookupError):
    """Raised when a requested durable registry record does not exist."""


class InvalidDownloadTransitionError(RuntimeError):
    """Raised when a Download Job attempts to move backward or leave a terminal state."""


class DownloadState(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    ACTIVATING = "activating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_ACTIVE_STATES = {
    DownloadState.QUEUED,
    DownloadState.VALIDATING,
    DownloadState.DOWNLOADING,
    DownloadState.VERIFYING,
    DownloadState.ACTIVATING,
}
_TERMINAL_STATES = {
    DownloadState.COMPLETED,
    DownloadState.CANCELLED,
    DownloadState.FAILED,
}
_STATE_ORDER = {
    DownloadState.QUEUED: 0,
    DownloadState.VALIDATING: 1,
    DownloadState.DOWNLOADING: 2,
    DownloadState.VERIFYING: 3,
    DownloadState.ACTIVATING: 4,
    DownloadState.COMPLETED: 5,
}


@dataclass(frozen=True)
class DownloadJob:
    id: str
    repository_id: str
    requested_revision: str | None
    engine_installation_id: str
    state: DownloadState
    bytes_downloaded: int
    total_bytes: int | None
    phase: str
    staging_path: str
    target_model_id: str | None
    cancellation_requested: bool
    correlation_id: str
    error: JsonObject | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ModelInstallation:
    id: str
    repository_id: str
    requested_revision: str | None
    resolved_commit: str
    engine_installation_id: str
    compatibility_evidence: JsonObject
    runtime_variant: str
    manifest: JsonObject
    checksum_summary: JsonObject
    byte_size: int
    cache_path: str
    desired_load_state: str
    observed_load_state: str
    replica_summary: JsonObject
    last_error: JsonObject | None
    created_at: str
    updated_at: str
    desired_replicas: int = 1


class ModelRegistry:
    """The only model-management repository allowed to issue SQLite writes."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def upsert_engine_installation(
        self,
        *,
        engine_installation_id: str,
        engine_id: str,
        version: str,
        command: list[str],
        working_directory: str,
        environment: JsonObject,
        capabilities: JsonObject,
        lifecycle_state: str,
        last_error: JsonObject | None = None,
    ) -> None:
        now = _utc_now()
        command_json = _encode_json(command, "command")
        environment_json = _encode_json_object(environment, "environment")
        capabilities_json = _encode_json_object(capabilities, "capabilities")
        last_error_json = _encode_optional_object(last_error, "last_error")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO engine_installations (
                    id, engine_id, version, command_json, working_directory,
                    environment_json, capabilities_json, lifecycle_state,
                    last_error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    engine_id = excluded.engine_id,
                    version = excluded.version,
                    command_json = excluded.command_json,
                    working_directory = excluded.working_directory,
                    environment_json = excluded.environment_json,
                    capabilities_json = excluded.capabilities_json,
                    lifecycle_state = excluded.lifecycle_state,
                    last_error_json = excluded.last_error_json,
                    updated_at = excluded.updated_at
                """,
                (
                    _required_text(engine_installation_id, "engine_installation_id"),
                    _required_text(engine_id, "engine_id"),
                    _required_text(version, "version"),
                    command_json,
                    _required_text(working_directory, "working_directory"),
                    environment_json,
                    capabilities_json,
                    _required_text(lifecycle_state, "lifecycle_state"),
                    last_error_json,
                    now,
                    now,
                ),
            )

    def create_download_job(
        self,
        *,
        repository_id: str,
        engine_installation_id: str,
        staging_path: str,
        correlation_id: str,
        requested_revision: str | None = None,
        job_id: str | None = None,
    ) -> DownloadJob:
        durable_id = job_id or str(uuid4())
        now = _utc_now()
        safe_staging_path = _managed_relative_path(staging_path, "staging")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO download_jobs (
                    id, repository_id, requested_revision, engine_installation_id,
                    state, bytes_downloaded, total_bytes, phase, staging_path,
                    target_model_id, cancellation_requested, correlation_id,
                    error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, NULL, 'queued', ?, NULL, 0, ?, NULL, ?, ?)
                """,
                (
                    _required_text(durable_id, "job_id"),
                    _required_text(repository_id, "repository_id"),
                    requested_revision,
                    _required_text(engine_installation_id, "engine_installation_id"),
                    safe_staging_path,
                    _required_text(correlation_id, "correlation_id"),
                    now,
                    now,
                ),
            )
            row = _required_row(
                connection.execute("SELECT * FROM download_jobs WHERE id = ?", (durable_id,))
            )
        return _download_job(row)

    def get_download_job(self, job_id: str) -> DownloadJob:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise RegistryRecordNotFoundError(f"Download Job {job_id!r} was not found")
        return _download_job(row)

    def list_download_jobs(self) -> tuple[DownloadJob, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM download_jobs ORDER BY created_at, id"
            ).fetchall()
        return tuple(_download_job(row) for row in rows)

    def request_download_cancellation(self, job_id: str) -> DownloadJob:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise RegistryRecordNotFoundError(f"Download Job {job_id!r} was not found")
            current = _download_job(row)
            if current.state not in _TERMINAL_STATES:
                connection.execute(
                    "UPDATE download_jobs SET cancellation_requested = 1, updated_at = ? "
                    "WHERE id = ?",
                    (_utc_now(), job_id),
                )
            updated = _required_row(
                connection.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,))
            )
        return _download_job(updated)

    def transition_download_job(
        self,
        job_id: str,
        state: DownloadState,
        *,
        phase: str | None = None,
        bytes_downloaded: int | None = None,
        total_bytes: int | None | object = _UNSET,
        error: JsonObject | None = None,
    ) -> DownloadJob:
        error_json = _encode_optional_object(error, "error")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise RegistryRecordNotFoundError(f"Download Job {job_id!r} was not found")
            current = _download_job(row)
            _validate_transition(current.state, state)
            next_bytes = current.bytes_downloaded if bytes_downloaded is None else bytes_downloaded
            next_total = current.total_bytes if total_bytes is _UNSET else total_bytes
            if not isinstance(next_bytes, int) or isinstance(next_bytes, bool) or next_bytes < 0:
                raise InvalidRegistryDataError("bytes_downloaded must be a non-negative integer")
            if next_bytes < current.bytes_downloaded:
                raise InvalidRegistryDataError("bytes_downloaded cannot decrease")
            if next_total is not None and (
                not isinstance(next_total, int)
                or isinstance(next_total, bool)
                or next_total < next_bytes
            ):
                raise InvalidRegistryDataError(
                    "total_bytes must be a non-negative integer not below downloaded bytes"
                )
            connection.execute(
                """
                UPDATE download_jobs SET
                    state = ?, phase = ?, bytes_downloaded = ?, total_bytes = ?,
                    error_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state.value,
                    phase if phase is not None else current.phase,
                    next_bytes,
                    next_total,
                    error_json,
                    _utc_now(),
                    job_id,
                ),
            )
            updated = _required_row(
                connection.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,))
            )
        return _download_job(updated)

    def mark_recovery_failure(self, job_id: str, error: JsonObject) -> DownloadJob:
        current = self.get_download_job(job_id)
        if current.state not in _ACTIVE_STATES:
            raise InvalidDownloadTransitionError(
                f"only an active Download Job can fail recovery, not {current.state.value}"
            )
        return self.transition_download_job(job_id, DownloadState.FAILED, error=error)

    def list_models(self) -> tuple[ModelInstallation, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM model_installations ORDER BY repository_id, id"
            ).fetchall()
        return tuple(_model_installation(row) for row in rows)

    def get_model(self, model_id: str) -> ModelInstallation:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM model_installations WHERE id = ?", (model_id,)
            ).fetchone()
        if row is None:
            raise RegistryRecordNotFoundError(f"Model Installation {model_id!r} was not found")
        return _model_installation(row)

    def update_replica_summary(
        self, model_id: str, replica_summary: JsonObject
    ) -> ModelInstallation:
        encoded = _encode_json_object(replica_summary, "replica_summary")
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE model_installations SET replica_summary_json = ?, updated_at = ? "
                "WHERE id = ?",
                (encoded, _utc_now(), model_id),
            )
            if cursor.rowcount != 1:
                raise RegistryRecordNotFoundError(f"Model Installation {model_id!r} was not found")
            row = _required_row(
                connection.execute("SELECT * FROM model_installations WHERE id = ?", (model_id,))
            )
        return _model_installation(row)

    def set_desired_replicas(self, model_id: str, count: int) -> ModelInstallation:
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 8:
            raise InvalidRegistryDataError("desired_replicas must be an integer between 1 and 8")
        with self._database.transaction() as connection:
            current_row = connection.execute(
                "SELECT replica_summary_json FROM model_installations WHERE id = ?", (model_id,)
            ).fetchone()
            if current_row is None:
                raise RegistryRecordNotFoundError(f"Model Installation {model_id!r} was not found")
            summary = _decode_object(current_row["replica_summary_json"], "replica_summary_json")
            summary["desired_replicas"] = count
            cursor = connection.execute(
                "UPDATE model_installations SET replica_summary_json = ?, updated_at = ? WHERE id = ?",
                (_encode_json_object(summary, "replica_summary"), _utc_now(), model_id),
            )
            if cursor.rowcount != 1:
                raise RegistryRecordNotFoundError(f"Model Installation {model_id!r} was not found")
            row = _required_row(connection.execute("SELECT * FROM model_installations WHERE id = ?", (model_id,)))
        return _model_installation(row)

    def activate_model(
        self,
        *,
        download_job_id: str,
        model_id: str,
        repository_id: str,
        requested_revision: str | None,
        resolved_commit: str,
        engine_installation_id: str,
        compatibility_evidence: JsonObject,
        runtime_variant: str,
        manifest: JsonObject,
        checksum_summary: JsonObject,
        byte_size: int,
        cache_path: str,
        desired_load_state: str,
        observed_load_state: str,
        replica_summary: JsonObject,
        last_error: JsonObject | None = None,
    ) -> ModelInstallation:
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
            raise InvalidRegistryDataError("byte_size must be a non-negative integer")
        safe_cache_path = _managed_relative_path(cache_path, "models")
        evidence_json = _encode_json_object(compatibility_evidence, "compatibility_evidence")
        manifest_json = _encode_json_object(manifest, "manifest")
        checksums_json = _encode_json_object(checksum_summary, "checksum_summary")
        replicas_json = _encode_json_object(replica_summary, "replica_summary")
        last_error_json = _encode_optional_object(last_error, "last_error")
        now = _utc_now()

        with self._database.transaction() as connection:
            job_row = connection.execute(
                "SELECT * FROM download_jobs WHERE id = ?", (download_job_id,)
            ).fetchone()
            if job_row is None:
                raise RegistryRecordNotFoundError(f"Download Job {download_job_id!r} was not found")
            job = _download_job(job_row)
            if job.state is not DownloadState.ACTIVATING:
                raise InvalidDownloadTransitionError(
                    "a model can only activate from an activating Download Job"
                )
            if job.repository_id != repository_id:
                raise InvalidRegistryDataError("model repository does not match its Download Job")
            if job.engine_installation_id != engine_installation_id:
                raise InvalidRegistryDataError("model engine does not match its Download Job")

            connection.execute(
                "DELETE FROM model_installations WHERE repository_id = ?", (repository_id,)
            )
            connection.execute(
                """
                INSERT INTO model_installations (
                    id, repository_id, requested_revision, resolved_commit,
                    engine_installation_id, compatibility_evidence_json,
                    runtime_variant, manifest_json, checksum_summary_json,
                    byte_size, cache_path, desired_load_state, observed_load_state,
                    replica_summary_json, last_error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _required_text(model_id, "model_id"),
                    _required_text(repository_id, "repository_id"),
                    requested_revision,
                    _required_text(resolved_commit, "resolved_commit"),
                    _required_text(engine_installation_id, "engine_installation_id"),
                    evidence_json,
                    _required_text(runtime_variant, "runtime_variant"),
                    manifest_json,
                    checksums_json,
                    byte_size,
                    safe_cache_path,
                    _required_text(desired_load_state, "desired_load_state"),
                    _required_text(observed_load_state, "observed_load_state"),
                    replicas_json,
                    last_error_json,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE download_jobs SET
                    state = 'completed', phase = 'completed', target_model_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (model_id, now, download_job_id),
            )
            model_row = _required_row(
                connection.execute("SELECT * FROM model_installations WHERE id = ?", (model_id,))
            )
        return _model_installation(model_row)

    def remove_model(self, model_id: str) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute("DELETE FROM model_installations WHERE id = ?", (model_id,))
            return cursor.rowcount == 1


def _validate_transition(current: DownloadState, target: DownloadState) -> None:
    if current in _TERMINAL_STATES:
        raise InvalidDownloadTransitionError(
            f"terminal Download Job state {current.value} cannot transition"
        )
    if target in {DownloadState.CANCELLED, DownloadState.FAILED}:
        return
    if target not in _STATE_ORDER or _STATE_ORDER[target] < _STATE_ORDER[current]:
        raise InvalidDownloadTransitionError(
            f"Download Job cannot move from {current.value} to {target.value}"
        )


def _download_job(row: Row) -> DownloadJob:
    return DownloadJob(
        id=row["id"],
        repository_id=row["repository_id"],
        requested_revision=row["requested_revision"],
        engine_installation_id=row["engine_installation_id"],
        state=DownloadState(row["state"]),
        bytes_downloaded=row["bytes_downloaded"],
        total_bytes=row["total_bytes"],
        phase=row["phase"],
        staging_path=row["staging_path"],
        target_model_id=row["target_model_id"],
        cancellation_requested=bool(row["cancellation_requested"]),
        correlation_id=row["correlation_id"],
        error=_decode_optional_object(row["error_json"], "error_json"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _model_installation(row: Row) -> ModelInstallation:
    return ModelInstallation(
        id=row["id"],
        repository_id=row["repository_id"],
        requested_revision=row["requested_revision"],
        resolved_commit=row["resolved_commit"],
        engine_installation_id=row["engine_installation_id"],
        compatibility_evidence=_decode_object(
            row["compatibility_evidence_json"], "compatibility_evidence_json"
        ),
        runtime_variant=row["runtime_variant"],
        manifest=_decode_object(row["manifest_json"], "manifest_json"),
        checksum_summary=_decode_object(row["checksum_summary_json"], "checksum_summary_json"),
        byte_size=row["byte_size"],
        cache_path=row["cache_path"],
        desired_load_state=row["desired_load_state"],
        observed_load_state=row["observed_load_state"],
        replica_summary=_decode_object(row["replica_summary_json"], "replica_summary_json"),
        desired_replicas=int(
            _decode_object(row["replica_summary_json"], "replica_summary_json").get(
                "desired_replicas", 1
            )
        ),
        last_error=_decode_optional_object(row["last_error_json"], "last_error_json"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _required_row(cursor: Cursor) -> Row:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("registry write did not produce its expected record")
    return cast(Row, row)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRegistryDataError(f"{field} must be a non-empty string")
    return value


def _managed_relative_path(value: str, top_level: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or "//" in value
        or path.parts[0] != top_level
        or len(path.parts) < 2
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvalidRegistryDataError(
            f"path must be a normalized managed-relative path below {top_level}/"
        )
    return path.as_posix()


def _encode_optional_object(value: JsonObject | None, field: str) -> str | None:
    return None if value is None else _encode_json_object(value, field)


def _encode_json_object(value: JsonObject, field: str) -> str:
    if not isinstance(value, dict):
        raise InvalidRegistryDataError(f"{field} must be a JSON object")
    return _encode_json(value, field)


def _encode_json(value: Any, field: str) -> str:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise InvalidRegistryDataError(f"{field} must contain valid JSON data") from error


def _decode_optional_object(value: str | None, field: str) -> JsonObject | None:
    return None if value is None else _decode_object(value, field)


def _decode_object(value: str, field: str) -> JsonObject:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise InvalidRegistryDataError(f"stored {field} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise InvalidRegistryDataError(f"stored {field} is not a JSON object")
    return decoded


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
