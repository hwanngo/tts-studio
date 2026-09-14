import asyncio
import hashlib
import io
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.config import Settings
from tts_studio.generation.domain import AlignmentState, AlignmentUnit
from tts_studio.generation.service import (
    GenerationArtifactNotFoundError,
    GenerationCapabilityError,
    GenerationRequestError,
)
from tts_studio.server.app import create_app
from tts_studio.server.routes.openai import SpeechRequest, create_speech
from tts_studio.voices.registry import SavedVoiceNotFoundError
from tts_studio.workers.generation import WorkerOperationError


@pytest.mark.asyncio
async def test_openai_speech_rejects_unsupported_response_format(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "model", "input": "hello", "voice": "voice", "response_format": "mp3"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_speech_request"


@pytest.mark.asyncio
async def test_openai_speech_rejects_oversized_input_before_generation(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "model", "input": "x" * 10_001, "voice": "voice"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_openai_speech_uses_core_generation_and_returns_wav(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF-test")

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            assert kwargs["model_id"] == "model"
            assert kwargs["voice_id"] == "voice"
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        def open_artifact(self, artifact_id: str):
            assert artifact_id == "artifact"
            return audio.open("rb")

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech", json={"model": "model", "input": "hello", "voice": "voice"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFF-test"


@pytest.mark.asyncio
async def test_openai_json_returns_alignment_and_base64_wav(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    audio = b"RIFF-test"

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        async def request_alignment(self, job_id: str, correlation_id: str) -> object:
            return SimpleNamespace()

        def get_alignment(self, job_id: str) -> object:
            return SimpleNamespace(
                state=AlignmentState.COMPLETED,
                result=SimpleNamespace(
                    schema_version=1,
                    job_id=job_id,
                    artifact_id="artifact",
                    transcript="hello",
                    sample_rate_hz=24000,
                    total_frames=240,
                    unit="frames",
                    aligner="fake",
                    units=(
                        AlignmentUnit(
                            text="hello",
                            source_start=0,
                            source_end=5,
                            start_frames=0,
                            end_frames=240,
                            confidence=0.5,
                            estimated=True,
                        ),
                    ),
                ),
            )

        def list_history(self) -> list[object]:
            return [
                SimpleNamespace(
                    id="artifact",
                    byte_size=len(audio),
                    sha256=hashlib.sha256(audio).hexdigest(),
                )
            ]

        def open_artifact(self, artifact_id: str) -> io.BytesIO:
            assert artifact_id == "artifact"
            return io.BytesIO(audio)

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "model", "input": "hello", "voice": "voice", "response_format": "json"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["job_id"] == "job"
    assert body["audio"] == "UklGRi10ZXN0"
    assert body["audio_format"] == "wav"
    assert body["media_type"] == "audio/wav"
    assert body["encoding"] == "base64"
    assert body["units"][0]["start_ms"] == 0.0
    assert body["units"][0]["end_ms"] == 10.0


@pytest.mark.asyncio
async def test_openai_speech_maps_unsafe_artifact_to_stable_error(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        def open_artifact(self, artifact_id: str):
            raise GenerationArtifactNotFoundError

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech", json={"model": "model", "input": "hello", "voice": "voice"}
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_speech_request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (GenerationRequestError("invalid"), 422, "unsupported_speech_request"),
        (GenerationCapabilityError("unsupported"), 503, "capability_unsupported"),
        (
            WorkerOperationError(
                engine_pb2.WorkerError(code="provider_timeout", message="timeout", retryable=True)
            ),
            503,
            "provider_timeout",
        ),
    ],
)
async def test_openai_speech_maps_generation_failures(
    tmp_path: Path, error: Exception, status_code: int, code: str
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            raise error

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech", json={"model": "model", "input": "hello", "voice": "voice"}
        )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_openai_speech_maps_failed_job_to_worker_error(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(
                artifact_id=None,
                state=SimpleNamespace(value="failed"),
                error={"code": "provider_timeout", "retryable": True},
            )

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech", json={"model": "model", "input": "hello", "voice": "voice"}
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_timeout"


@pytest.mark.asyncio
async def test_openai_speech_closes_thread_result_when_request_is_cancelled_during_open(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))
    started = threading.Event()
    release = threading.Event()
    closed = asyncio.Event()
    closed_loop = asyncio.get_running_loop()

    class Handle:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            closed_loop.call_soon_threadsafe(closed.set)

    handle = Handle()

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        def open_artifact(self, artifact_id: str) -> Handle:
            started.set()
            assert release.wait(timeout=5)
            return handle

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    request = SimpleNamespace(
        app=app,
        state=SimpleNamespace(correlation_id="cancelled-open"),
    )
    request_task = asyncio.create_task(
        create_speech(
            SpeechRequest(model="model", input="hello", voice="voice"),
            request,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    request_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    await asyncio.wait_for(closed.wait(), timeout=2)
    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_openai_json_rejects_oversized_artifact_before_opening(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        async def request_alignment(self, job_id: str, correlation_id: str) -> object:
            return SimpleNamespace()

        def get_alignment(self, job_id: str) -> object:
            return SimpleNamespace(
                state=AlignmentState.COMPLETED,
                result=SimpleNamespace(
                    schema_version=1,
                    job_id=job_id,
                    artifact_id="artifact",
                    transcript="hello",
                    sample_rate_hz=24000,
                    total_frames=1,
                    unit="frames",
                    aligner="fake",
                    units=(),
                ),
            )

        def list_history(self) -> list[object]:
            return [SimpleNamespace(id="artifact", byte_size=33 * 1024 * 1024)]

        def open_artifact(self, artifact_id: str) -> object:
            raise AssertionError("oversized artifact must be rejected before opening")

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "model", "input": "hello", "voice": "voice", "response_format": "json"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_speech_request"


@pytest.mark.asyncio
async def test_openai_json_rejects_same_size_bytes_changed_after_open(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        async def request_alignment(self, job_id: str, correlation_id: str) -> object:
            return SimpleNamespace()

        def get_alignment(self, job_id: str) -> object:
            return SimpleNamespace(
                state=AlignmentState.COMPLETED,
                result=SimpleNamespace(
                    schema_version=1,
                    job_id=job_id,
                    artifact_id="artifact",
                    transcript="hello",
                    sample_rate_hz=24000,
                    total_frames=1,
                    unit="frames",
                    aligner="fake",
                    units=(),
                ),
            )

        def list_history(self) -> list[object]:
            return [
                SimpleNamespace(
                    id="artifact",
                    byte_size=4,
                    sha256=hashlib.sha256(b"good").hexdigest(),
                )
            ]

        def open_artifact(self, artifact_id: str) -> object:
            del artifact_id
            return io.BytesIO(b"evil")

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "model", "input": "hello", "voice": "voice", "response_format": "json"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "artifact_invalid"


@pytest.mark.asyncio
async def test_openai_json_maps_post_alignment_artifact_tamper(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    class FakeSavedVoices:
        def get(self, voice_id: str) -> object:
            raise SavedVoiceNotFoundError(voice_id)

    class FakeGeneration:
        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(id="job")

        async def wait(self, job_id: str) -> object:
            return SimpleNamespace(artifact_id="artifact")

        async def request_alignment(self, job_id: str, correlation_id: str) -> object:
            return SimpleNamespace()

        def get_alignment(self, job_id: str) -> object:
            return SimpleNamespace(
                state=AlignmentState.COMPLETED,
                result=SimpleNamespace(
                    schema_version=1,
                    job_id=job_id,
                    artifact_id="artifact",
                    transcript="hello",
                    sample_rate_hz=24000,
                    total_frames=1,
                    unit="frames",
                    aligner="fake",
                    units=(),
                ),
            )

        def list_history(self) -> list[object]:
            return [SimpleNamespace(id="artifact", byte_size=4)]

        def open_artifact(self, artifact_id: str) -> object:
            raise GenerationArtifactNotFoundError

    app.state.saved_voice_service = FakeSavedVoices()
    app.state.generation_service = FakeGeneration()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "model", "input": "hello", "voice": "voice", "response_format": "json"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_not_found"
