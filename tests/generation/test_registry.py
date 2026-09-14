from pathlib import Path

import pytest

from tts_studio.generation.domain import GenerationState, SynthesisOptions
from tts_studio.generation.registry import (
    GenerationRegistry,
    InvalidGenerationTransitionError,
    InvalidRegistryDataError,
)
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _registry(tmp_path: Path) -> GenerationRegistry:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return GenerationRegistry(database)


def _job(registry: GenerationRegistry, job_id: str = "generation-one"):
    return registry.create_job(
        job_id=job_id,
        model_id="model-one",
        engine_id="fake",
        voice_id="Adam",
        text="Xin chào exact text",
        retain_artifact=True,
        correlation_id=f"correlation-{job_id}",
    )


def test_generation_options_round_trip_durably(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    job = registry.create_job(
        job_id="options-job",
        model_id="model-one",
        engine_id="fake",
        voice_id="Adam",
        text="options text",
        options=SynthesisOptions(speed=1.25, pitch=-0.5, volume=0.75),
        correlation_id="options-correlation",
    )

    assert job.options == SynthesisOptions(speed=1.25, pitch=-0.5, volume=0.75)
    assert registry.get_job(job.id).options == job.options


def test_reference_generation_job_round_trips_nullable_voice_and_reference_id(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    job = registry.create_job(
        job_id="reference-job",
        model_id="model-one",
        engine_id="fake",
        voice_id=None,
        reference_id="reference-one",
        text="reference text",
        correlation_id="reference-correlation",
    )

    assert job.voice_id is None
    assert job.reference_id == "reference-one"
    assert registry.get_job(job.id) == job


@pytest.mark.parametrize(
    ("voice_id", "reference_id"),
    [(None, None), ("Adam", "reference-one")],
)
def test_generation_job_requires_exactly_one_source(
    tmp_path: Path, voice_id: str | None, reference_id: str | None
) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(InvalidRegistryDataError, match="exactly one"):
        registry.create_job(
            model_id="model-one",
            engine_id="fake",
            voice_id=voice_id,
            reference_id=reference_id,
            text="text",
            correlation_id="correlation",
        )


def test_terminal_reference_cleanup_can_redact_reference_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    job = registry.create_job(
        model_id="model-one",
        engine_id="fake",
        voice_id=None,
        reference_id="reference-one",
        text="text",
        correlation_id="correlation",
    )

    redacted = registry.clear_reference_id(job.id)

    assert redacted.reference_id is None


def _complete(registry: GenerationRegistry, job_id: str) -> None:
    for state in (
        GenerationState.LOADING,
        GenerationState.GENERATING,
        GenerationState.FINALIZING,
        GenerationState.COMPLETED,
    ):
        registry.transition_job(job_id, state)


def test_generation_job_round_trips_immutable_domain_values(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    job = _job(registry)

    assert job.state is GenerationState.QUEUED
    assert job.text == "Xin chào exact text"
    assert job.retain_artifact is True
    assert registry.get_job(job.id) == job
    assert registry.list_jobs() == (job,)


def test_generation_state_machine_rejects_illegal_moves_and_terminal_reuse(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    job = _job(registry)

    registry.transition_job(job.id, GenerationState.LOADING)
    registry.transition_job(job.id, GenerationState.GENERATING)
    registry.transition_job(job.id, GenerationState.FINALIZING)
    completed = registry.transition_job(job.id, GenerationState.COMPLETED)
    assert completed.state is GenerationState.COMPLETED

    with pytest.raises(InvalidGenerationTransitionError):
        registry.transition_job(job.id, GenerationState.GENERATING)


def test_cancellation_request_is_linearized_against_terminal_transition(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    job = _job(registry)

    requested = registry.request_cancellation(job.id)
    assert requested.cancellation_requested is True
    cancelled = registry.transition_job(job.id, GenerationState.CANCELLED)
    assert cancelled.state is GenerationState.CANCELLED

    active = _job(registry, "active-cancel")
    registry.transition_job(active.id, GenerationState.LOADING)
    registry.transition_job(active.id, GenerationState.GENERATING)
    registry.request_cancellation(active.id)
    with pytest.raises(InvalidGenerationTransitionError):
        registry.transition_job(active.id, GenerationState.FINALIZING)
    assert registry.get_job(active.id).state is GenerationState.GENERATING

    late_request = registry.request_cancellation(job.id)
    assert late_request.cancellation_requested is True


def test_recovery_fails_active_jobs_but_leaves_queued_schedulable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    queued = _job(registry, "queued")
    active = _job(registry, "active")
    registry.transition_job(active.id, GenerationState.LOADING)

    recovered = registry.mark_recovery_failure(
        active.id,
        {"code": "recovery_required", "message": "Generation was interrupted."},
    )

    assert recovered.state is GenerationState.FAILED
    assert recovered.error == {
        "code": "recovery_required",
        "message": "Generation was interrupted.",
    }
    assert registry.get_job(queued.id).state is GenerationState.QUEUED


def test_history_is_newest_first_and_artifact_deletion_preserves_job(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    first = _job(registry, "first")
    second = _job(registry, "second")
    _complete(registry, first.id)
    _complete(registry, second.id)
    first_artifact = registry.create_artifact(
        job_id=first.id,
        artifact_id="artifact-first",
        path="audio/artifact-first.wav",
        byte_size=4,
        sha256="a" * 64,
        sample_rate=48000,
        channel_count=1,
        frame_count=2,
    )
    second_artifact = registry.create_artifact(
        job_id=second.id,
        artifact_id="artifact-second",
        path="audio/artifact-second.wav",
        byte_size=8,
        sha256="b" * 64,
        sample_rate=48000,
        channel_count=1,
        frame_count=4,
    )

    assert [item.id for item in registry.list_history()] == [
        second_artifact.id,
        first_artifact.id,
    ]
    assert registry.delete_artifact(second_artifact.id) is True
    assert registry.delete_artifact(second_artifact.id) is False
    assert registry.get_job(second.id).artifact_id is None


def test_registry_rejects_unsafe_artifact_path_and_invalid_error_json(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    job = _job(registry)
    _complete(registry, job.id)

    with pytest.raises(InvalidRegistryDataError):
        registry.create_artifact(
            job_id=job.id,
            artifact_id="artifact-one",
            path="/tmp/output.wav",
            byte_size=4,
            sha256="a" * 64,
            sample_rate=48000,
            channel_count=1,
            frame_count=2,
        )
    with pytest.raises(InvalidRegistryDataError):
        registry.transition_job(
            job.id,
            GenerationState.FAILED,
            error={"bad": float("nan")},
        )
