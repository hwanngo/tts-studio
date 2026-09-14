"""Typed persistence for Generation Jobs and retained Audio Artifacts."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import PurePosixPath
from sqlite3 import Cursor, Row
from typing import Any, cast
from uuid import uuid4

from tts_studio.generation.domain import (
    AlignmentJob,
    AlignmentResult,
    AlignmentState,
    AlignmentUnit,
    AudioArtifact,
    GenerationJob,
    GenerationState,
    SynthesisOptions,
)
from tts_studio.storage.db import Database

JsonObject = dict[str, Any]


class InvalidRegistryDataError(ValueError):
    """Raised when a generation value cannot be persisted safely."""


class InvalidGenerationTransitionError(RuntimeError):
    """Raised when a Generation Job leaves its legal state machine."""


class GenerationRecordNotFoundError(LookupError):
    """Raised when a requested generation record does not exist."""


RegistryRecordNotFoundError = GenerationRecordNotFoundError


_ACTIVE_STATES = {
    GenerationState.LOADING,
    GenerationState.GENERATING,
    GenerationState.FINALIZING,
}
_CANCELLABLE_STATES = {
    GenerationState.QUEUED,
    GenerationState.LOADING,
    GenerationState.GENERATING,
}
_MAX_ALIGNMENT_TRANSCRIPT = 2_000
_MAX_ALIGNMENT_UNITS = 10_000
_MAX_ALIGNMENT_RESULT_BYTES = 1024 * 1024
_MAX_ALIGNMENT_ERROR_BYTES = 16 * 1024
_ALIGNMENT_STATES = {
    AlignmentState.QUEUED,
    AlignmentState.RUNNING,
    AlignmentState.COMPLETED,
    AlignmentState.FAILED,
}
_TRANSITIONS = {
    GenerationState.QUEUED: {
        GenerationState.LOADING,
        GenerationState.CANCELLED,
        GenerationState.FAILED,
    },
    GenerationState.LOADING: {
        GenerationState.GENERATING,
        GenerationState.CANCELLED,
        GenerationState.FAILED,
    },
    GenerationState.GENERATING: {
        GenerationState.FINALIZING,
        GenerationState.CANCELLED,
        GenerationState.FAILED,
    },
    GenerationState.FINALIZING: {GenerationState.COMPLETED, GenerationState.FAILED},
    GenerationState.COMPLETED: set(),
    GenerationState.CANCELLED: set(),
    GenerationState.FAILED: set(),
}


class GenerationRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_job(
        self,
        *,
        model_id: str,
        engine_id: str,
        voice_id: str | None = None,
        reference_id: str | None = None,
        saved_voice_id: str | None = None,
        provider_id: str | None = None,
        text: str,
        retain_artifact: bool = True,
        options: SynthesisOptions | None = None,
        correlation_id: str,
        job_id: str | None = None,
        retry_of: str | None = None,
    ) -> GenerationJob:
        durable_id = _required_text(job_id or str(uuid4()), "job_id")
        _validate_source(voice_id, reference_id, saved_voice_id)
        if not isinstance(text, str) or "\0" in text:
            raise InvalidRegistryDataError("text must be a string without NUL characters")
        if not isinstance(retain_artifact, bool):
            raise InvalidRegistryDataError("retain_artifact must be a boolean")
        now = _utc_now()
        with self._database.transaction() as connection:
            if retry_of is not None:
                existing = connection.execute(
                    "SELECT * FROM generation_jobs WHERE retry_of = ?", (retry_of,)
                ).fetchone()
                if existing is not None:
                    return _job(existing)
                original = _job_or_missing(
                    connection.execute(
                        "SELECT * FROM generation_jobs WHERE id = ?", (retry_of,)
                    ).fetchone(),
                    retry_of,
                )
                artifact = connection.execute(
                    "SELECT id FROM audio_artifacts WHERE job_id = ?", (retry_of,)
                ).fetchone()
                if not original.can_retry or artifact is not None:
                    raise InvalidGenerationTransitionError("Generation Job cannot be retried")
            connection.execute(
                """INSERT INTO generation_jobs (
                    id, model_id, engine_id, voice_id, reference_id, saved_voice_id, provider_id, text, retain_artifact,
                    speed, pitch, volume, state, bytes_written, frame_count, sample_rate, channel_count, artifact_id,
                    correlation_id, cancellation_requested, error_json, created_at, updated_at, retry_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, NULL, NULL, NULL, ?, 0, NULL, ?, ?, ?)""",
                (
                    durable_id,
                    _required_text(model_id, "model_id"),
                    _required_text(engine_id, "engine_id"),
                    _optional_text(voice_id, "voice_id"),
                    _optional_text(reference_id, "reference_id"),
                    _optional_text(saved_voice_id, "saved_voice_id"),
                    _optional_text(provider_id, "provider_id"),
                    text,
                    int(retain_artifact),
                    options.speed if options is not None else None,
                    options.pitch if options is not None else None,
                    options.volume if options is not None else None,
                    _required_text(correlation_id, "correlation_id"),
                    now,
                    now,
                    retry_of,
                ),
            )
            row = _required_row(
                connection.execute("SELECT * FROM generation_jobs WHERE id = ?", (durable_id,))
            )
        return _job(row)

    def get_retry(self, job_id: str) -> GenerationJob | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM generation_jobs WHERE retry_of = ?", (job_id,)
            ).fetchone()
        return _job(row) if row is not None else None

    def get_job(self, job_id: str) -> GenerationJob:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise GenerationRecordNotFoundError(f"Generation Job {job_id!r} was not found")
        return _job(row)

    def clear_reference_id(self, job_id: str) -> GenerationJob:
        with self._database.transaction() as connection:
            _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            connection.execute(
                "UPDATE generation_jobs SET reference_id = NULL, updated_at = ? WHERE id = ?",
                (_utc_now(), job_id),
            )
            row = _required_row(
                connection.execute("SELECT * FROM generation_jobs WHERE id = ?", (job_id,))
            )
        return _job(row)

    def list_jobs(self) -> tuple[GenerationJob, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM generation_jobs ORDER BY created_at, id"
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def request_cancellation(self, job_id: str) -> GenerationJob:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            current = _job_or_missing(row, job_id)
            if current.state in _CANCELLABLE_STATES:
                connection.execute(
                    "UPDATE generation_jobs SET cancellation_requested = 1, updated_at = ? WHERE id = ?",
                    (_utc_now(), job_id),
                )
            return _job(
                _required_row(
                    connection.execute("SELECT * FROM generation_jobs WHERE id = ?", (job_id,))
                )
            )

    def transition_job(
        self,
        job_id: str,
        state: GenerationState,
        *,
        bytes_written: int | None = None,
        frame_count: int | None = None,
        sample_rate: int | None = None,
        channel_count: int | None = None,
        error: JsonObject | None = None,
    ) -> GenerationJob:
        if not isinstance(state, GenerationState):
            raise InvalidGenerationTransitionError("state must be a GenerationState")
        error_json = _encode_optional_object(error, "error")
        with self._database.transaction() as connection:
            current = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            if state not in _TRANSITIONS[current.state]:
                raise InvalidGenerationTransitionError(
                    f"Generation Job cannot move from {current.state.value} to {state.value}"
                )
            if current.cancellation_requested and state not in {
                GenerationState.CANCELLED,
                GenerationState.FAILED,
            }:
                raise InvalidGenerationTransitionError(
                    "a cancellation request must be resolved before the Generation Job can advance"
                )
            next_bytes = current.bytes_written if bytes_written is None else bytes_written
            next_frames = current.frame_count if frame_count is None else frame_count
            if not isinstance(next_bytes, int) or isinstance(next_bytes, bool) or next_bytes < 0:
                raise InvalidRegistryDataError("bytes_written must be a non-negative integer")
            if not isinstance(next_frames, int) or isinstance(next_frames, bool) or next_frames < 0:
                raise InvalidRegistryDataError("frame_count must be a non-negative integer")
            if next_bytes < current.bytes_written or next_frames < current.frame_count:
                raise InvalidRegistryDataError("generation byte and frame counts cannot decrease")
            connection.execute(
                """UPDATE generation_jobs SET state = ?, bytes_written = ?, frame_count = ?,
                   sample_rate = COALESCE(?, sample_rate), channel_count = COALESCE(?, channel_count),
                   error_json = ?, updated_at = ?, text = CASE
                       WHEN retain_artifact = 0 AND ? IN ('completed', 'cancelled', 'failed') THEN ''
                       ELSE text END WHERE id = ?""",
                (
                    state.value,
                    next_bytes,
                    next_frames,
                    sample_rate,
                    channel_count,
                    error_json,
                    _utc_now(),
                    state.value,
                    job_id,
                ),
            )
            return _job(
                _required_row(
                    connection.execute("SELECT * FROM generation_jobs WHERE id = ?", (job_id,))
                )
            )

    def create_artifact(
        self,
        *,
        job_id: str,
        artifact_id: str | None = None,
        path: str,
        byte_size: int,
        sha256: str,
        sample_rate: int,
        channel_count: int,
        frame_count: int,
        duration_ms: int | None = None,
    ) -> AudioArtifact:
        safe_path = _managed_audio_path(path)
        _non_negative(byte_size, "byte_size")
        _positive(sample_rate, "sample_rate")
        _positive(channel_count, "channel_count")
        _non_negative(frame_count, "frame_count")
        if duration_ms is not None:
            _non_negative(duration_ms, "duration_ms")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise InvalidRegistryDataError("sha256 must be a 64-character digest")
        durable_id = _required_text(artifact_id or str(uuid4()), "artifact_id")
        now = _utc_now()
        with self._database.transaction() as connection:
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            if not job.retain_artifact:
                raise InvalidRegistryDataError("cannot create an artifact for a non-retained job")
            connection.execute(
                """INSERT INTO audio_artifacts
                   (id, job_id, path, byte_size, sha256, sample_rate, channel_count,
                    frame_count, duration_ms, created_at, retained_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    durable_id,
                    job_id,
                    safe_path,
                    byte_size,
                    sha256,
                    sample_rate,
                    channel_count,
                    frame_count,
                    duration_ms,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE generation_jobs SET artifact_id = ?, updated_at = ? WHERE id = ?",
                (durable_id, now, job_id),
            )
            row = _required_row(
                connection.execute("SELECT * FROM audio_artifacts WHERE id = ?", (durable_id,))
            )
        return _artifact(row)

    def get_alignment(self, job_id: str) -> AlignmentJob:
        with self._database.transaction() as connection:
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            row = connection.execute(
                "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was not found"
                )
            if not _alignment_matches_current_artifact(connection, job, row):
                connection.execute("DELETE FROM generation_alignments WHERE job_id = ?", (job_id,))
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was stale"
                )
            return _alignment(row)

    def create_alignment(self, job_id: str) -> AlignmentJob:
        now = _utc_now()
        with self._database.transaction() as connection:
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            if job.state is not GenerationState.COMPLETED:
                raise InvalidRegistryDataError("alignment requires a completed Generation Job")
            if not job.retain_artifact or job.artifact_id is None:
                raise InvalidRegistryDataError("alignment requires a retained Audio Artifact")
            artifact = connection.execute(
                "SELECT id FROM audio_artifacts WHERE id = ? AND job_id = ?",
                (job.artifact_id, job_id),
            ).fetchone()
            if artifact is None:
                raise InvalidRegistryDataError("alignment requires a retained Audio Artifact")
            existing = connection.execute(
                "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if _alignment_matches_current_artifact(connection, job, existing):
                    return _alignment(existing)
                connection.execute("DELETE FROM generation_alignments WHERE job_id = ?", (job_id,))
            connection.execute(
                """INSERT INTO generation_alignments
                   (job_id, artifact_id, state, result_json, error_json, created_at, updated_at)
                   VALUES (?, ?, 'queued', NULL, NULL, ?, ?)""",
                (job_id, job.artifact_id, now, now),
            )
            return _alignment(
                _required_row(
                    connection.execute(
                        "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                    )
                )
            )

    def retry_alignment(self, job_id: str) -> AlignmentJob:
        with self._database.transaction() as connection:
            current = _alignment_or_missing(
                connection.execute(
                    "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            if (
                current.state is not AlignmentState.FAILED
                or current.error is None
                or current.error.get("retryable") is not True
            ):
                raise InvalidRegistryDataError("alignment failure is not retryable")
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            if (
                job.state is not GenerationState.COMPLETED
                or not job.retain_artifact
                or job.artifact_id != current.artifact_id
            ):
                raise InvalidRegistryDataError(
                    "alignment requires a completed Generation Job with its retained Audio Artifact"
                )
            artifact = connection.execute(
                "SELECT id FROM audio_artifacts WHERE id = ? AND job_id = ?",
                (current.artifact_id, job_id),
            ).fetchone()
            if artifact is None:
                raise InvalidRegistryDataError("alignment requires a retained Audio Artifact")
            connection.execute(
                "UPDATE generation_alignments SET state = 'queued', result_json = NULL, error_json = NULL, updated_at = ? WHERE job_id = ?",
                (_utc_now(), job_id),
            )
            return _alignment(
                _required_row(
                    connection.execute(
                        "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                    )
                )
            )

    def start_alignment(self, job_id: str) -> AlignmentJob:
        with self._database.transaction() as connection:
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            row = connection.execute(
                "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was not found"
                )
            if not _alignment_matches_current_artifact(connection, job, row):
                connection.execute("DELETE FROM generation_alignments WHERE job_id = ?", (job_id,))
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was stale"
                )
            current = _alignment(row)
            if current.state is AlignmentState.QUEUED:
                connection.execute(
                    "UPDATE generation_alignments SET state = 'running', updated_at = ? WHERE job_id = ?",
                    (_utc_now(), job_id),
                )
            elif current.state is not AlignmentState.RUNNING:
                raise InvalidRegistryDataError("alignment is terminal")
            return _alignment(
                _required_row(
                    connection.execute(
                        "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                    )
                )
            )

    def complete_alignment(self, job_id: str, result: AlignmentResult) -> AlignmentJob:
        result_json = _encode_alignment_result(result)
        with self._database.transaction() as connection:
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            row = connection.execute(
                "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was not found"
                )
            if not _alignment_matches_current_artifact(connection, job, row):
                connection.execute("DELETE FROM generation_alignments WHERE job_id = ?", (job_id,))
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was stale"
                )
            current = _alignment(row)
            if current.state not in {AlignmentState.QUEUED, AlignmentState.RUNNING}:
                raise InvalidRegistryDataError("alignment is terminal")
            artifact = (
                connection.execute(
                    "SELECT sample_rate, frame_count, id FROM audio_artifacts WHERE id = ? AND job_id = ?",
                    (job.artifact_id, job_id),
                ).fetchone()
                if job.artifact_id
                else None
            )
            if artifact is None:
                raise InvalidRegistryDataError("alignment requires a retained Audio Artifact")
            _validate_alignment_result(result, job, artifact)
            connection.execute(
                "UPDATE generation_alignments SET state = 'completed', result_json = ?, error_json = NULL, updated_at = ? WHERE job_id = ?",
                (result_json, _utc_now(), job_id),
            )
            return _alignment(
                _required_row(
                    connection.execute(
                        "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                    )
                )
            )

    def fail_alignment(self, job_id: str, error: JsonObject) -> AlignmentJob:
        error_json = _encode_optional_object(error, "error", max_bytes=_MAX_ALIGNMENT_ERROR_BYTES)
        with self._database.transaction() as connection:
            job = _job_or_missing(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
                job_id,
            )
            row = connection.execute(
                "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was not found"
                )
            if not _alignment_matches_current_artifact(connection, job, row):
                connection.execute("DELETE FROM generation_alignments WHERE job_id = ?", (job_id,))
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was stale"
                )
            current = _alignment(row)
            if current.state not in {AlignmentState.QUEUED, AlignmentState.RUNNING}:
                raise InvalidRegistryDataError("alignment is terminal")
            connection.execute(
                "UPDATE generation_alignments SET state = 'failed', result_json = NULL, error_json = ?, updated_at = ? WHERE job_id = ?",
                (error_json, _utc_now(), job_id),
            )
            return _alignment(
                _required_row(
                    connection.execute(
                        "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                    )
                )
            )

    def recover_alignment(self, job_id: str) -> AlignmentJob:
        """Normalize a malformed persisted alignment so startup can continue."""
        error_json = _encode_optional_object(
            {
                "code": "alignment_recovery_required",
                "message": "Alignment state was malformed and requires retry.",
                "retryable": True,
            },
            "error",
            max_bytes=_MAX_ALIGNMENT_ERROR_BYTES,
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise GenerationRecordNotFoundError(
                    f"Alignment for Generation Job {job_id!r} was not found"
                )
            connection.execute(
                """UPDATE generation_alignments
                   SET state = 'failed', result_json = NULL, error_json = ?, updated_at = ?
                   WHERE job_id = ?""",
                (error_json, _utc_now(), job_id),
            )
            return _alignment(
                _required_row(
                    connection.execute(
                        "SELECT * FROM generation_alignments WHERE job_id = ?", (job_id,)
                    )
                )
            )

    def list_history(self) -> tuple[AudioArtifact, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM audio_artifacts ORDER BY retained_at DESC, id DESC"
            ).fetchall()
        return tuple(_artifact(row) for row in rows)

    def delete_artifact(self, artifact_id: str) -> bool:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT job_id FROM audio_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM audio_artifacts WHERE id = ?", (artifact_id,))
            connection.execute(
                "UPDATE generation_jobs SET artifact_id = NULL, updated_at = ? WHERE id = ?",
                (_utc_now(), row[0]),
            )
            return True

    def mark_recovery_failure(self, job_id: str, error: JsonObject) -> GenerationJob:
        current = self.get_job(job_id)
        if current.state not in _ACTIVE_STATES and current.state is not GenerationState.QUEUED:
            raise InvalidGenerationTransitionError(
                f"only an active Generation Job can fail recovery, not {current.state.value}"
            )
        return self.transition_job(job_id, GenerationState.FAILED, error=error)


def _alignment_matches_current_artifact(connection: Any, job: GenerationJob, row: Row) -> bool:
    if not job.retain_artifact or job.artifact_id is None or row["artifact_id"] != job.artifact_id:
        return False
    artifact = connection.execute(
        "SELECT id FROM audio_artifacts WHERE id = ? AND job_id = ?",
        (job.artifact_id, job.id),
    ).fetchone()
    return artifact is not None


def _alignment(row: Row) -> AlignmentJob:
    state = row["state"]
    if state not in {item.value for item in _ALIGNMENT_STATES}:
        raise InvalidRegistryDataError("stored alignment state is invalid")
    return AlignmentJob(
        job_id=row["job_id"],
        artifact_id=row["artifact_id"],
        state=AlignmentState(state),
        result=_decode_alignment_result(row["result_json"]),
        error=_decode_alignment_error(row["error_json"]),
        created_at=_stored_timestamp(row["created_at"], "created_at"),
        updated_at=_stored_timestamp(row["updated_at"], "updated_at"),
    )


def _alignment_or_missing(row: Row | None, job_id: str) -> AlignmentJob:
    if row is None:
        raise GenerationRecordNotFoundError(
            f"Alignment for Generation Job {job_id!r} was not found"
        )
    return _alignment(row)


def _validate_alignment_result(result: AlignmentResult, job: GenerationJob, artifact: Row) -> None:
    _validate_alignment_shape(result)
    if result.job_id != job.id:
        raise InvalidRegistryDataError("job_id does not match Generation Job")
    if result.artifact_id != artifact["id"]:
        raise InvalidRegistryDataError("artifact_id does not match Audio Artifact")
    if result.transcript != job.text:
        raise InvalidRegistryDataError("transcript does not match Generation Job text")
    if result.sample_rate_hz != artifact["sample_rate"]:
        raise InvalidRegistryDataError("sample_rate does not match Audio Artifact")
    if result.total_frames != artifact["frame_count"]:
        raise InvalidRegistryDataError("frames do not match Audio Artifact")


def _validate_alignment_shape(result: AlignmentResult) -> None:
    if (
        not isinstance(result, AlignmentResult)
        or type(result.schema_version) is not int
        or result.schema_version != 1
    ):
        raise InvalidRegistryDataError("alignment result schema is unsupported")
    if not isinstance(result.job_id, str) or not result.job_id.strip() or "\0" in result.job_id:
        raise InvalidRegistryDataError("job_id is invalid")
    if (
        not isinstance(result.artifact_id, str)
        or not result.artifact_id.strip()
        or "\0" in result.artifact_id
    ):
        raise InvalidRegistryDataError("artifact_id is invalid")
    if (
        not isinstance(result.transcript, str)
        or len(result.transcript) > _MAX_ALIGNMENT_TRANSCRIPT
        or "\0" in result.transcript
    ):
        raise InvalidRegistryDataError("transcript exceeds alignment bounds")
    if type(result.sample_rate_hz) is not int or result.sample_rate_hz <= 0:
        raise InvalidRegistryDataError("sample_rate must be a positive integer")
    if type(result.total_frames) is not int or result.total_frames < 0:
        raise InvalidRegistryDataError("frames must be a non-negative integer")
    if not isinstance(result.unit, str) or result.unit not in {"word", "phoneme", "character"}:
        raise InvalidRegistryDataError("alignment unit is invalid")
    if not isinstance(result.aligner, str) or not result.aligner.strip() or "\0" in result.aligner:
        raise InvalidRegistryDataError("aligner is invalid")
    if not isinstance(result.units, tuple) or len(result.units) > _MAX_ALIGNMENT_UNITS:
        raise InvalidRegistryDataError("unit count exceeds alignment bounds")
    if result.transcript and not result.units:
        raise InvalidRegistryDataError("non-empty transcripts require alignment units")
    try:
        transcript_bytes = result.transcript.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidRegistryDataError("transcript is not valid UTF-8") from error
    previous_source_end = previous_frame_end = 0
    for unit in result.units:
        if (
            not isinstance(unit, AlignmentUnit)
            or not isinstance(unit.text, str)
            or not unit.text
            or "\0" in unit.text
        ):
            raise InvalidRegistryDataError("alignment unit text is invalid")
        if (
            type(unit.source_start) is not int
            or type(unit.source_end) is not int
            or not 0 <= unit.source_start < unit.source_end <= len(transcript_bytes)
            or unit.source_start < previous_source_end
        ):
            raise InvalidRegistryDataError("alignment source range is invalid")
        try:
            source_text = transcript_bytes[unit.source_start : unit.source_end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidRegistryDataError("alignment source range is not UTF-8 aligned") from error
        if unit.text != source_text:
            raise InvalidRegistryDataError("alignment unit text does not match transcript")
        if (
            type(unit.start_frames) is not int
            or type(unit.end_frames) is not int
            or not 0 <= unit.start_frames <= unit.end_frames <= result.total_frames
            or unit.start_frames < previous_frame_end
        ):
            raise InvalidRegistryDataError("alignment frame range is invalid")
        if (
            not isinstance(unit.confidence, (int, float))
            or isinstance(unit.confidence, bool)
            or not math.isfinite(unit.confidence)
            or not 0 <= unit.confidence <= 1
        ):
            raise InvalidRegistryDataError("alignment confidence is invalid")
        if not isinstance(unit.estimated, bool):
            raise InvalidRegistryDataError("alignment estimated flag is invalid")
        previous_source_end, previous_frame_end = unit.source_end, unit.end_frames


def _encode_alignment_result(result: AlignmentResult) -> str:
    _validate_alignment_shape(result)
    payload = {
        "schema_version": result.schema_version,
        "job_id": result.job_id,
        "artifact_id": result.artifact_id,
        "transcript": result.transcript,
        "sample_rate_hz": result.sample_rate_hz,
        "total_frames": result.total_frames,
        "unit": result.unit,
        "aligner": result.aligner,
        "units": [
            {
                "text": item.text,
                "source_start": item.source_start,
                "source_end": item.source_end,
                "start_frames": item.start_frames,
                "end_frames": item.end_frames,
                "confidence": item.confidence,
                "estimated": item.estimated,
            }
            for item in result.units
        ],
    }
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise InvalidRegistryDataError("alignment result must contain valid JSON data") from error
    if len(encoded.encode("utf-8")) > _MAX_ALIGNMENT_RESULT_BYTES:
        raise InvalidRegistryDataError("alignment result exceeds size bounds")
    return encoded


def _decode_alignment_error(value: str | None) -> JsonObject | None:
    if value is not None and len(value.encode("utf-8")) > _MAX_ALIGNMENT_ERROR_BYTES:
        raise InvalidRegistryDataError("stored error_json exceeds size bounds")
    return _decode_optional_object(value)


def _decode_alignment_result(value: str | None) -> AlignmentResult | None:
    if value is None:
        return None
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InvalidRegistryDataError("stored alignment result is not valid UTF-8") from error
    if encoded_size > _MAX_ALIGNMENT_RESULT_BYTES:
        raise InvalidRegistryDataError("stored alignment result exceeds size bounds")
    try:
        payload = json.loads(value)
        units = tuple(AlignmentUnit(**item) for item in payload["units"])
        result = AlignmentResult(
            units=units,
            **{
                key: payload[key]
                for key in (
                    "schema_version",
                    "job_id",
                    "artifact_id",
                    "transcript",
                    "sample_rate_hz",
                    "total_frames",
                    "unit",
                    "aligner",
                )
            },
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidRegistryDataError("stored alignment result is invalid") from error
    _validate_alignment_shape(result)
    return result


def _job(row: Row) -> GenerationJob:
    return GenerationJob(
        id=row["id"],
        model_id=row["model_id"],
        engine_id=row["engine_id"],
        voice_id=row["voice_id"],
        reference_id=row["reference_id"],
        saved_voice_id=row["saved_voice_id"],
        provider_id=row["provider_id"],
        text=row["text"],
        retain_artifact=bool(row["retain_artifact"]),
        options=(
            SynthesisOptions(speed=row["speed"], pitch=row["pitch"], volume=row["volume"])
            if any(row[name] is not None for name in ("speed", "pitch", "volume"))
            else None
        ),
        state=GenerationState(row["state"]),
        bytes_written=row["bytes_written"],
        frame_count=row["frame_count"],
        sample_rate=row["sample_rate"],
        channel_count=row["channel_count"],
        artifact_id=row["artifact_id"],
        correlation_id=row["correlation_id"],
        cancellation_requested=bool(row["cancellation_requested"]),
        error=_decode_optional_object(row["error_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _artifact(row: Row) -> AudioArtifact:
    return AudioArtifact(
        id=row["id"],
        job_id=row["job_id"],
        path=row["path"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        sample_rate=row["sample_rate"],
        channel_count=row["channel_count"],
        frame_count=row["frame_count"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"],
        retained_at=row["retained_at"],
    )


def _job_or_missing(row: Row | None, job_id: str) -> GenerationJob:
    if row is None:
        raise GenerationRecordNotFoundError(f"Generation Job {job_id!r} was not found")
    return _job(row)


def _required_row(cursor: Cursor) -> Row:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("generation write did not produce its expected record")
    return cast(Row, row)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise InvalidRegistryDataError(f"{field} must be a non-empty safe string")
    return value


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _validate_source(
    voice_id: str | None, reference_id: str | None, saved_voice_id: str | None = None
) -> None:
    if sum(value is not None for value in (voice_id, reference_id, saved_voice_id)) != 1:
        raise InvalidRegistryDataError("generation job requires exactly one voice source")


def _managed_audio_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or path.parts[:1] != ("audio",)
        or len(path.parts) < 2
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvalidRegistryDataError(
            "path must be a normalized managed-relative path below audio/"
        )
    return path.as_posix()


def _non_negative(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidRegistryDataError(f"{field} must be a non-negative integer")


def _positive(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidRegistryDataError(f"{field} must be a positive integer")


def _encode_optional_object(
    value: JsonObject | None, field: str, *, max_bytes: int | None = None
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRegistryDataError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise InvalidRegistryDataError(f"{field} must contain valid JSON data") from error
    if max_bytes is not None and len(encoded.encode("utf-8")) > max_bytes:
        raise InvalidRegistryDataError(f"{field} exceeds size bounds")
    return encoded


def _decode_optional_object(value: str | None) -> JsonObject | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRegistryDataError("stored error_json is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise InvalidRegistryDataError("stored error_json is not a JSON object")
    return cast(JsonObject, decoded)


def _stored_timestamp(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise InvalidRegistryDataError(f"stored {field} is invalid")
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
