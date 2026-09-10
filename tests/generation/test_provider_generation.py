from pathlib import Path

import pytest

from tts_studio.config import Settings
from tts_studio.generation.service import GenerationModelNotFoundError
from tts_studio.server.app import create_app
from tts_studio.voices.registry import SavedVoiceNotFoundError
from tts_studio.workers.generation import build_synthesis_request


def test_provider_synthesis_request_carries_typed_configuration_without_changing_voice_source() -> None:
    request = build_synthesis_request(
        "provider/profile-1", "hello", voice_id="alloy",
        provider_config=("https://provider.example/v1", "tts-1", "secret"),
    )
    assert request.provider.base_url == "https://provider.example/v1"
    assert request.provider.model == "tts-1"
    assert request.provider.api_key == "secret"
    assert request.voice_id == "alloy"


@pytest.mark.asyncio
async def test_provider_model_without_profile_is_rejected_before_generation(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    app = create_app(Settings.resolve(tmp_path / "data"))
    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            raise GenerationModelNotFoundError

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "provider:missing", "input": "hello", "voice": "alloy"},
        )
    assert response.status_code == 404
