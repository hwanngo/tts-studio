"""HTTP behavior for transient native Reference Recording uploads."""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.config import Settings
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.storage.db import Database
from tts_studio.workers.generation import WorkerCapabilities


@pytest.fixture
async def core(tmp_path: Path):
    app = create_app(
        Settings.resolve(tmp_path / ".tts-studio"), include_test_adapters=True
    )
    database = Database(app.state.storage_layout.database_path)
    database.migrate()
    _activate_model(ModelRegistry(database))
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield app, client


def _activate_model(registry: ModelRegistry) -> None:
    registry.upsert_engine_installation(
        engine_installation_id="fake@0.2.0",
        engine_id="fake",
        version="0.2.0",
        command=["uv", "run", "--project", "workers/fake", "tts-studio-fake-worker"],
        working_directory=str(Path(__file__).resolve().parents[2]),
        environment={},
        capabilities={"model_lifecycle": True, "streaming_synthesis": True},
        lifecycle_state="ready",
    )
    download = registry.create_download_job(
        job_id="download-reference-routes",
        repository_id="fixtures/compatible",
        requested_revision=None,
        engine_installation_id="fake@0.2.0",
        staging_path="staging/download-reference-routes",
        correlation_id="download-correlation",
    )
    for state in (
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    ):
        registry.transition_download_job(download.id, state)
    registry.activate_model(
        download_job_id=download.id,
        model_id="fixtures/compatible",
        repository_id="fixtures/compatible",
        requested_revision=None,
        resolved_commit="a" * 40,
        engine_installation_id="fake@0.2.0",
        compatibility_evidence={"engine_id": "fake"},
        runtime_variant="fp32",
        manifest={},
        checksum_summary={},
        byte_size=1,
        cache_path="models/model-reference-routes",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


def _wav_bytes(*, frames: int = 2400, sample_rate: int = 24000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0\0" * frames)
    return stream.getvalue()


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code
    correlation_id = response.headers["x-correlation-id"]
    assert str(UUID(correlation_id)) == correlation_id
    error = response.json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == correlation_id
    return error


@pytest.mark.asyncio
async def test_upload_validates_with_model_worker_and_returns_safe_metadata(core: Any) -> None:
    app, client = core
    response = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible", "transcript": "hello"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201
    body = response.json()
    assert UUID(body["id"])
    assert body["model_id"] == "fixtures/compatible"
    assert body["container"] == "wav"
    assert body["sample_rate_hz"] == 24000
    assert body["channels"] == 1
    assert body["duration_ms"] == 100
    assert body["state"] == "validated"
    assert body["transcript_present"] is True
    assert "relative_path" not in body
    assert "hello" not in response.text
    assert body["evidence"]
    assert (app.state.storage_layout.reference_staging / body["id"]).exists()


@pytest.mark.asyncio
async def test_reference_openapi_describes_multipart_and_stable_routes(core: Any) -> None:
    app, _client = core
    schema = app.openapi()
    assert "/api/v1/references" in schema["paths"]
    request_schema = schema["paths"]["/api/v1/references"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    assert set(request_schema["required"]) == {"model_id", "file"}
    assert "/api/v1/references/{reference_id}" in schema["paths"]
    assert (
        schema["paths"]["/api/v1/references/{reference_id}"]["delete"]["responses"]["409"]
        ["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/ErrorEnvelope"}
    )


@pytest.mark.asyncio
async def test_upload_refreshes_lazy_reference_capability_after_load(core: Any) -> None:
    app, client = core
    worker = app.state.supervisor._workers["fake"]
    worker.capabilities = WorkerCapabilities(
        engine_id="fake", engine_version="0.2.0", supported=frozenset(), max_concurrency=1
    )
    response = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "validated"


@pytest.mark.asyncio
async def test_upload_maps_reference_capability_still_missing_after_load(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    worker = app.state.supervisor._workers["fake"]
    worker.capabilities = WorkerCapabilities(
        engine_id="fake", engine_version="0.2.0", supported=frozenset(), max_concurrency=1
    )

    async def describe_without_reference(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
            engine_id="fake",
            engine_version="0.2.0",
            capabilities=[engine_pb2.Capability(name="streaming_synthesis", supported=True)],
            max_concurrency=1,
        )

    monkeypatch.setattr(worker.stub, "Describe", describe_without_reference)
    response = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    _assert_error(response, 503, "reference_capability_unsupported")
    assert tuple(app.state.storage_layout.reference_staging.iterdir()) == ()


@pytest.mark.asyncio
async def test_upload_rejects_missing_fields_invalid_reference_and_oversize(core: Any) -> None:
    app, client = core
    missing_model = await client.post(
        "/api/v1/references", files={"file": ("sample.wav", _wav_bytes(), "audio/wav")}
    )
    _assert_error(missing_model, 422, "reference_request_invalid")

    invalid_transcript = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible", "transcript": "bad\x00text"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    _assert_error(invalid_transcript, 422, "reference_invalid")
    assert tuple(app.state.storage_layout.reference_staging.iterdir()) == ()

    oversize = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible"},
        files={"file": ("large.wav", b"x" * (20 * 1024 * 1024 + 1), "audio/wav")},
    )
    _assert_error(oversize, 422, "reference_request_invalid")
    assert tuple(app.state.storage_layout.reference_staging.iterdir()) == ()


@pytest.mark.asyncio
async def test_delete_saved_voice_maps_identity_unavailable_to_structured_503(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    directory = app.state.storage_layout.voices / "voice-route-delete"
    directory.mkdir()
    path = directory / "reference.wav"
    original = b"saved voice"
    path.write_bytes(original)
    voice = app.state.saved_voice_service._registry.create(
        voice_id="voice-route-delete",
        model_id="fixtures/compatible",
        label="Route Voice",
        relative_path="voices/voice-route-delete/reference.wav",
        transcript=None,
    )

    class LibcWithoutFunlinkat:
        pass

    monkeypatch.setattr(
        "tts_studio.storage.identity.ctypes.CDLL",
        lambda *_args, **_kwargs: LibcWithoutFunlinkat(),
    )

    response = await client.delete(f"/api/v1/saved-voices/{voice.id}")

    error = _assert_error(response, 503, "reference_cleanup_failed")
    assert error["retryable"] is True
    assert app.state.saved_voice_service.get(voice.id) == voice
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_delete_reference_is_idempotent_only_for_existing_record(core: Any) -> None:
    app, client = core
    created = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    reference_id = created.json()["id"]
    deleted = await client.delete(f"/api/v1/references/{reference_id}")
    assert deleted.status_code == 204
    assert not (app.state.storage_layout.reference_staging / reference_id).exists()
    missing = await client.delete(f"/api/v1/references/{reference_id}")
    _assert_error(missing, 404, "reference_not_found")


@pytest.mark.asyncio
async def test_delete_reference_conflicts_while_generation_owns_it(core: Any) -> None:
    app, client = core
    created = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible", "transcript": "hello"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    assert created.status_code == 201
    reference_id = created.json()["id"]
    scheduler = app.state.generation_service._scheduler
    original_submit = scheduler.submit
    scheduler.submit = lambda _job_id: None
    try:
        generation = await client.post(
            "/api/v1/generations",
            json={
                "model_id": "fixtures/compatible",
                "reference_id": reference_id,
                "text": "queued reference generation",
            },
        )
        assert generation.status_code == 202
        deleted = await client.delete(f"/api/v1/references/{reference_id}")
    finally:
        scheduler.submit = original_submit
    _assert_error(deleted, 409, "reference_in_use")
    assert (app.state.storage_layout.reference_staging / reference_id).exists()
