import json
import sqlite3
from pathlib import Path

import pytest
from tts_studio_protocol.engine.v1 import engine_pb2

import tts_studio.generation.service as service_module
from tts_studio.generation.domain import AlignmentState, GenerationState
from tts_studio.generation.registry import GenerationRegistry, InvalidRegistryDataError
from tts_studio.generation.service import GenerationCapabilityError, GenerationService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.generation import AlignmentCapability, WorkerCapabilities


def _registry(tmp_path: Path) -> GenerationRegistry:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = GenerationRegistry(database)
    job = registry.create_job(
        job_id="generation-one",
        model_id="model-one",
        engine_id="fake",
        voice_id="voice-one",
        text="hello world",
        correlation_id="correlation-one",
    )
    for state in (
        GenerationState.LOADING,
        GenerationState.GENERATING,
        GenerationState.FINALIZING,
        GenerationState.COMPLETED,
    ):
        registry.transition_job(job.id, state)
    registry.create_artifact(
        job_id=job.id,
        artifact_id="artifact-one",
        path="audio/artifact-one.wav",
        byte_size=4,
        sha256="a" * 64,
        sample_rate=48_000,
        channel_count=1,
        frame_count=2,
    )
    registry.create_alignment(job.id)
    return registry


def _stored_result(*, unit: object = "word", transcript: str = "hello world", units: object | None = None) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "job_id": "generation-one",
            "artifact_id": "artifact-one",
            "transcript": transcript,
            "sample_rate_hz": 48_000,
            "total_frames": 2,
            "unit": unit,
            "aligner": "fake-aligner",
            "units": units if units is not None else [
                {
                    "text": "hello world",
                    "source_start": 0,
                    "source_end": 11,
                    "start_frames": 0,
                    "end_frames": 2,
                    "confidence": 1.0,
                    "estimated": False,
                }
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _replace_stored_result(registry: GenerationRegistry, result_json: str) -> None:
    with registry._database.transaction() as connection:
        connection.execute(
            "UPDATE generation_alignments SET result_json = ?, state = 'completed' WHERE job_id = ?",
            (result_json, "generation-one"),
        )


def test_stale_alignment_is_not_reused_after_artifact_replacement(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with sqlite3.connect(registry._database.path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE audio_artifacts SET id = ?, path = ?, byte_size = ?, sha256 = ?, frame_count = ? WHERE id = ?",
            ("artifact-two", "audio/artifact-two.wav", 8, "b" * 64, 4, "artifact-one"),
        )
        connection.execute(
            "UPDATE generation_jobs SET artifact_id = ? WHERE id = ?",
            ("artifact-two", "generation-one"),
        )

    with pytest.raises(LookupError):
        registry.get_alignment("generation-one")

    replacement = registry.create_alignment("generation-one")
    assert replacement.artifact_id == "artifact-two"
    assert replacement.state.value == "queued"


def test_get_alignment_rejects_invalid_unit_type_as_typed_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _replace_stored_result(registry, _stored_result(unit=[]))

    with pytest.raises(InvalidRegistryDataError, match="unit"):
        registry.get_alignment("generation-one")


def test_get_alignment_rejects_escaped_lone_surrogate_as_typed_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _replace_stored_result(registry, _stored_result(transcript="\ud800"))

    with pytest.raises(InvalidRegistryDataError, match="UTF-8"):
        registry.get_alignment("generation-one")


@pytest.mark.parametrize("state", ("queued", "running", "completed", "failed"))
def test_resume_alignments_normalizes_malformed_persisted_rows(
    tmp_path: Path, state: str
) -> None:
    registry = _registry(tmp_path)
    with registry._database.transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE generation_alignments SET state = ?, result_json = ?, error_json = ? WHERE job_id = ?",
            (state, "not-json", "not-json", "generation-one"),
        )

    class _Scheduler:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def submit(self, job_id: str) -> None:
            self.submitted.append(job_id)

    scheduler = _Scheduler()
    service = object.__new__(GenerationService)
    service._registry = registry
    service._alignment_scheduler = scheduler

    service.resume_alignments()

    recovered = registry.get_alignment("generation-one")
    assert recovered.state is AlignmentState.FAILED
    assert recovered.error == {
        "code": "alignment_recovery_required",
        "message": "Alignment state was malformed and requires retry.",
        "retryable": True,
    }
    assert scheduler.submitted == []


def test_alignment_capability_requires_advertised_capability_bit() -> None:
    capabilities = WorkerCapabilities(
        "fake",
        "1",
        frozenset(),
        1,
        AlignmentCapability(("word",), ("en",), "fake-aligner"),
    )

    with pytest.raises(GenerationCapabilityError, match="alignment"):
        service_module._require_alignment_capability(capabilities)


def test_worker_result_rejects_empty_units_for_non_empty_transcript() -> None:
    response = engine_pb2.AlignResponse(
        result=engine_pb2.AlignmentResult(
            schema_version=1,
            transcript="hello world",
            sample_rate_hz=48_000,
            total_frames=2,
            unit="word",
            aligner="fake-aligner",
        )
    )

    with pytest.raises(ValueError, match="no alignment units"):
        service_module._alignment_result_from_worker(
            response,
            job=type("Job", (), {"id": "job-one", "text": "hello world"})(),
            artifact=type("Artifact", (), {"id": "artifact-one", "sample_rate": 48_000, "frame_count": 2})(),
            capability=AlignmentCapability(("word",), ("en",), "fake-aligner"),
        )


def test_persisted_result_rejects_empty_units_for_non_empty_transcript(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _replace_stored_result(registry, _stored_result(units=[]))

    with pytest.raises(InvalidRegistryDataError, match="alignment units"):
        registry.get_alignment("generation-one")
