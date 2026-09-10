"""End-to-end native reference upload and generation contract."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.storage.db import Database


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
        job_id="download-reference-integration",
        repository_id="fixtures/compatible",
        requested_revision=None,
        engine_installation_id="fake@0.2.0",
        staging_path="staging/download-reference-integration",
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
        cache_path="models/model-reference-integration",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )

def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\0\0" * 2400)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_reference_upload_can_be_consumed_by_generation(core) -> None:
    app, client = core
    uploaded = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible", "transcript": "hello"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 201
    reference_id = uploaded.json()["id"]
    generation = await client.post(
        "/api/v1/generations",
        json={
            "model_id": "fixtures/compatible",
            "reference_id": reference_id,
            "text": "reference generation",
        },
    )
    assert generation.status_code == 202
    assert generation.json()["reference_id"] == reference_id
    await app.state.generation_service.wait(generation.json()["id"])
    completed = await client.get(f"/api/v1/generations/{generation.json()['id']}")
    assert completed.json()["state"] == "completed"
    assert completed.json()["reference_id"] is None
    assert not (app.state.storage_layout.reference_staging / reference_id).exists()
