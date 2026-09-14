from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.storage.db import Database


def _activate_installed_vieneu_shaped_model(registry: ModelRegistry) -> None:
    registry.upsert_engine_installation(
        engine_installation_id="fake@0.2.0",
        engine_id="fake",
        version="0.2.0",
        command=["uv", "run", "--project", "workers/fake", "tts-studio-fake-worker"],
        working_directory=str(Path(__file__).resolve().parents[2]),
        environment={},
        capabilities={
            "model_lifecycle": True,
            "preset_voices": True,
            "streaming_synthesis": True,
        },
        lifecycle_state="ready",
    )
    download = registry.create_download_job(
        job_id="download-vieneu-preset",
        repository_id="pnnbao-ump/VieNeu-TTS-v3-Turbo",
        requested_revision="8b7e9cffb4b41918cb638b9f62f0a751184d14a6",
        engine_installation_id="fake@0.2.0",
        staging_path="staging/download-vieneu-preset",
        correlation_id="vieneu-preset-correlation",
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
        model_id="vieneu-installed",
        repository_id="pnnbao-ump/VieNeu-TTS-v3-Turbo",
        requested_revision="8b7e9cffb4b41918cb638b9f62f0a751184d14a6",
        resolved_commit="8b7e9cffb4b41918cb638b9f62f0a751184d14a6",
        engine_installation_id="fake@0.2.0",
        compatibility_evidence={"engine_id": "fake", "adapter": "vieneu"},
        runtime_variant="fp32",
        manifest={},
        checksum_summary={},
        byte_size=1,
        cache_path="models/vieneu-installed",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


@pytest.mark.asyncio
async def test_installed_vieneu_preset_uses_runtime_voice_and_core_wav_pipeline(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.resolve(tmp_path / ".tts-studio"), include_test_adapters=True)
    database = Database(app.state.storage_layout.database_path)
    database.migrate()
    _activate_installed_vieneu_shaped_model(ModelRegistry(database))

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        voices = await client.get("/api/v1/voices", params={"model_id": "vieneu-installed"})
        assert voices.status_code == 200
        assert voices.json() == [
            {
                "id": "fake-neutral",
                "label": "Fake Neutral",
                "capabilities": ["preset", "deterministic"],
            }
        ]

        created = await client.post(
            "/api/v1/generations",
            json={
                "model_id": "vieneu-installed",
                "voice_id": "fake-neutral",
                "text": "runtime preset",
            },
        )
        assert created.status_code == 202
        job = await app.state.generation_service.wait(created.json()["id"])
        assert job.model_id == "vieneu-installed"
        assert job.voice_id == "fake-neutral"
        assert job.artifact_id is not None

        artifact = app.state.generation_service.list_history()[0]
        with wave.open(str(app.state.generation_service.read_artifact(artifact.id)), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                48_000,
                1,
                2,
            )
            assert wav.readframes(wav.getnframes())

        detail: dict[str, Any] = (await client.get(f"/api/v1/generations/{job.id}")).json()
        assert detail["model_id"] == "vieneu-installed"
