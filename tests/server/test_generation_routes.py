"""HTTP behavior for Core-owned generation jobs and retained audio."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.config import Settings
from tts_studio.generation.domain import GenerationState
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.server.routes.generation import (
    _ArtifactStreamingResponse,
    _parse_single_range,
    _stream_artifact,
    download_artifact,
)
from tts_studio.storage.db import Database
from tts_studio.workers.generation import WorkerOperationError


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


async def _model_id(client: AsyncClient, app: Any) -> str:
    del client, app
    return "fixtures/compatible"


def _activate_model(registry: ModelRegistry) -> None:
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
        job_id="download-generation-routes",
        repository_id="fixtures/compatible",
        requested_revision=None,
        engine_installation_id="fake@0.2.0",
        staging_path="staging/download-generation-routes",
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
        cache_path="models/model-generation-routes",
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


async def _completed_generation(
    client: AsyncClient, app: Any, *, retain_artifact: bool | None = True
) -> dict[str, Any]:
    model_id = await _model_id(client, app)
    payload: dict[str, Any] = {
        "model_id": model_id,
        "voice_id": "fake-neutral",
        "text": "HTTP generation",
    }
    if retain_artifact is not None:
        payload["retain_artifact"] = retain_artifact
    response = await client.post("/api/v1/generations", json=payload)
    assert response.status_code == 202
    job_id = response.json()["id"]
    job = await app.state.generation_service.wait(job_id)
    assert job.state.value == "completed"
    return (await client.get(f"/api/v1/generations/{job_id}")).json()


@pytest.mark.asyncio
async def test_voice_preview_returns_valid_wav_without_durable_state(core: Any) -> None:
    app, client = core
    before_jobs = await client.get("/api/v1/generations")
    before_history = await client.get("/api/v1/history")

    response = await client.post(
        "/api/v1/voices/preview",
        json={"model_id": "fixtures/compatible", "voice_id": "fake-neutral", "text": "Preview"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content), "rb") as wav:
        assert wav.getframerate() == 48_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() > 0
    assert (await client.get("/api/v1/generations")).json() == before_jobs.json()
    assert (await client.get("/api/v1/history")).json() == before_history.json()
    assert tuple(app.state.storage_layout.staging.iterdir()) == ()
    assert tuple(app.state.storage_layout.audio.iterdir()) == ()


@pytest.mark.asyncio
async def test_voice_preview_maps_validation_and_worker_errors(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    unknown_model = await client.post(
        "/api/v1/voices/preview",
        json={"model_id": "missing", "voice_id": "fake-neutral", "text": "Preview"},
    )
    _assert_error(unknown_model, 404, "model_not_found")

    unknown_voice = await client.post(
        "/api/v1/voices/preview",
        json={"model_id": "fixtures/compatible", "voice_id": "missing", "text": "Preview"},
    )
    _assert_error(unknown_voice, 422, "voice_not_found")

    empty_text = await client.post(
        "/api/v1/voices/preview",
        json={"model_id": "fixtures/compatible", "voice_id": "fake-neutral", "text": ""},
    )
    _assert_error(empty_text, 422, "generation_request_invalid")

    async def unsupported(*, model_id: str, voice_id: str, text: str) -> bytes:
        del model_id, voice_id, text
        from tts_studio.generation.service import GenerationCapabilityError
        raise GenerationCapabilityError("unsupported")

    monkeypatch.setattr(app.state.generation_service, "preview", unsupported)
    unsupported_response = await client.post(
        "/api/v1/voices/preview",
        json={"model_id": "fixtures/compatible", "voice_id": "fake-neutral", "text": "Preview"},
    )
    _assert_error(unsupported_response, 503, "capability_unsupported")


@pytest.mark.asyncio
async def test_voice_preview_maps_worker_failure(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = core
    from tts_studio.workers.generation import WorkerOperationError

    async def failed_preview(*, model_id: str, voice_id: str, text: str) -> bytes:
        del model_id, voice_id, text
        raise WorkerOperationError(
            engine_pb2.WorkerError(code="preview_failed", message="failed", retryable=True)
        )

    monkeypatch.setattr(app.state.generation_service, "preview", failed_preview)
    response = await client.post(
        "/api/v1/voices/preview",
        json={"model_id": "fixtures/compatible", "voice_id": "fake-neutral", "text": "Preview"},
    )
    error = _assert_error(response, 503, "preview_failed")
    assert error["retryable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("speed", 0.1), ("speed", 4.1), ("pitch", -1.1), ("pitch", 1.1), ("volume", -0.1), ("volume", 2.1)])
async def test_generation_rejects_out_of_range_options(core: Any, field: str, value: float) -> None:
    _app, client = core
    response = await client.post("/api/v1/generations", json={"model_id": "fixtures/compatible", "voice_id": "fake-neutral", "text": "bounded", field: value})
    _assert_error(response, 422, "generation_request_invalid")


@pytest.mark.asyncio
async def test_generation_rejects_requested_option_without_worker_capability(core: Any) -> None:
    _app, client = core
    response = await client.post("/api/v1/generations", json={"model_id": "fixtures/compatible", "voice_id": "fake-neutral", "text": "unsupported", "speed": 1.1})
    _assert_error(response, 503, "capability_unsupported")


@pytest.mark.asyncio
async def test_generation_retention_uses_persisted_default_only_when_omitted(core: Any) -> None:
    app, client = core
    update = await client.patch("/api/v1/settings", json={"retain_audio_by_default": False})
    assert update.status_code == 200

    omitted = await _completed_generation(client, app, retain_artifact=None)
    explicit_true = await _completed_generation(client, app, retain_artifact=True)
    explicit_false = await _completed_generation(client, app, retain_artifact=False)

    assert omitted["retain_artifact"] is False
    assert explicit_true["retain_artifact"] is True
    assert explicit_false["retain_artifact"] is False


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code
    correlation_id = response.headers["x-correlation-id"]
    assert str(UUID(correlation_id)) == correlation_id
    error = response.json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == correlation_id
    return error


@pytest.mark.asyncio
async def test_generation_openapi_exposes_routes_and_stable_error_models(core: Any) -> None:
    app, _client = core
    schema = app.openapi()

    assert {
        "/api/v1/voices",
        "/api/v1/voices/preview",
        "/api/v1/generations",
        "/api/v1/generations/{job_id}",
        "/api/v1/generations/{job_id}/cancel",
        "/api/v1/artifacts/{artifact_id}/audio",
        "/api/v1/history",
        "/api/v1/history/{artifact_id}",
    } <= set(schema["paths"])
    assert "style" not in str(schema)
    assert schema["paths"]["/api/v1/voices/preview"]["post"]["responses"]["200"] == {
        "description": "Successful Response",
        "content": {
            "audio/wav": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
    }
    generation_properties = schema["components"]["schemas"]["GenerationRequest"]["properties"]
    assert generation_properties["speed"]["anyOf"][0] == {"type": "number"}
    assert generation_properties["speed"]["minimum"] == 0.25
    assert generation_properties["speed"]["maximum"] == 4.0
    assert generation_properties["pitch"]["anyOf"][0] == {"type": "number"}
    assert generation_properties["pitch"]["minimum"] == -1.0
    assert generation_properties["pitch"]["maximum"] == 1.0
    assert generation_properties["volume"]["anyOf"][0] == {"type": "number"}
    assert generation_properties["volume"]["minimum"] == 0.0
    assert generation_properties["volume"]["maximum"] == 2.0
    assert (
        schema["paths"]["/api/v1/generations"]["post"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"]
        == {"$ref": "#/components/schemas/ErrorEnvelope"}
    )


@pytest.mark.asyncio
async def test_voices_generation_audio_and_history_share_core_behavior(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    model_id = await _model_id(client, app)

    voices = await client.get("/api/v1/voices", params={"model_id": model_id})
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
        json={"model_id": model_id, "voice_id": "fake-neutral", "text": "hello"},
    )
    assert created.status_code == 202
    job_id = created.json()["id"]
    assert created.json()["state"] == "queued"
    assert created.json()["artifact_url"] is None

    completed = await app.state.generation_service.wait(job_id)
    assert completed.state.value == "completed"
    detail = await client.get(f"/api/v1/generations/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["artifact_id"] is not None
    assert detail.json()["artifact_url"].endswith("/audio")
    assert detail.json()["text"] == "hello"
    assert "style" not in detail.text

    listing = await client.get("/api/v1/generations")
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == job_id

    artifact_id = detail.json()["artifact_id"]
    audio = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.content[:4] == b"RIFF"
    assert audio.headers["accept-ranges"] == "bytes"
    assert int(audio.headers["content-length"]) == len(audio.content)

    history = await client.get("/api/v1/history")
    assert history.status_code == 200
    assert history.json()[0]["id"] == artifact_id
    monkeypatch.setattr(
        app.state.generation_service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    deleted = await client.delete(f"/api/v1/history/{artifact_id}")
    assert deleted.status_code == 204
    assert (await client.get("/api/v1/history")).json() == []


@pytest.mark.asyncio
async def test_artifact_download_supports_ranges_and_head(core: Any) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    full = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")
    assert full.status_code == 200

    partial = await client.get(
        f"/api/v1/artifacts/{artifact_id}/audio", headers={"Range": "bytes=0-3"}
    )
    assert partial.status_code == 206
    assert partial.content == full.content[:4]
    assert partial.headers["content-range"] == f"bytes 0-3/{len(full.content)}"
    assert partial.headers["content-length"] == "4"
    assert partial.headers["accept-ranges"] == "bytes"

    suffix = await client.get(
        f"/api/v1/artifacts/{artifact_id}/audio", headers={"Range": "bytes=-4"}
    )
    assert suffix.status_code == 206
    assert suffix.content == full.content[-4:]

    invalid = await client.get(
        f"/api/v1/artifacts/{artifact_id}/audio",
        headers={"Range": f"bytes={len(full.content)}-"},
    )
    error = _assert_error(invalid, 416, "artifact_range_invalid")
    assert error["source"] == "generation"
    assert invalid.headers["content-range"] == f"bytes */{len(full.content)}"

    head = await client.head(f"/api/v1/artifacts/{artifact_id}/audio")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(full.content))
    assert head.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "range_value",
    ["bytes=+1-2", "bytes=1-2 ", "bytes=1_2", "bytes=1-2,4-5"],
)
async def test_artifact_download_rejects_malformed_or_multiple_ranges(
    core: Any, range_value: str
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None

    response = await client.get(
        f"/api/v1/artifacts/{artifact_id}/audio", headers={"Range": range_value}
    )

    _assert_error(response, 416, "artifact_range_invalid")


def test_artifact_range_parser_rejects_non_ascii_decimal_digits() -> None:
    assert _parse_single_range("bytes=١-٢", 16) is None


def test_artifact_range_parser_rejects_unbounded_decimal_input() -> None:
    assert _parse_single_range(f"bytes={'9' * 5000}-", 16) is None


@pytest.mark.asyncio
async def test_artifact_download_maps_unsafe_storage_to_stable_error(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None

    def unsafe(_layout: Any, _name: str):
        from tts_studio.storage.layout import UnsafeStoragePathError
        raise UnsafeStoragePathError("redirected")

    from tts_studio.storage.layout import StorageLayout
    monkeypatch.setattr(StorageLayout, "checked_directory", unsafe)
    response = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")

    _assert_error(response, 404, "artifact_not_found")


@pytest.mark.asyncio
async def test_history_delete_maps_unsafe_storage_to_stable_error(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None

    def unsafe(_layout: Any, _name: str):
        from tts_studio.storage.layout import UnsafeStoragePathError
        raise UnsafeStoragePathError("redirected")

    from tts_studio.storage.layout import StorageLayout
    monkeypatch.setattr(StorageLayout, "checked_directory", unsafe)
    response = await client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(response, 404, "artifact_not_found")


@pytest.mark.asyncio
async def test_history_delete_maps_operational_validation_error_to_storage_failure(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service

    def fail_validation(_artifact: object, _path: Path):
        raise PermissionError("managed audio is unreadable")

    monkeypatch.setattr(service, "_open_validated_artifact_for_deletion", fail_validation)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as safe_client:
        response = await safe_client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(response, 503, "artifact_delete_failed")
    history = await client.get("/api/v1/history")
    assert [item["id"] for item in history.json()] == [artifact_id]


@pytest.mark.asyncio
async def test_history_delete_fails_closed_when_identity_bound_unlink_is_unavailable(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service
    path = service.read_artifact(artifact_id)

    class LibcWithoutFunlinkat:
        pass

    monkeypatch.setattr(
        "tts_studio.storage.identity.ctypes.CDLL",
        lambda *_args, **_kwargs: LibcWithoutFunlinkat(),
    )

    response = await client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(response, 503, "artifact_delete_failed")
    assert path.exists()
    history = await client.get("/api/v1/history")
    assert [item["id"] for item in history.json()] == [artifact_id]


@pytest.mark.asyncio
async def test_history_delete_maps_post_unlink_restore_failure_to_stable_storage_error(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service
    path = service.read_artifact(artifact_id)
    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    monkeypatch.setattr(
        service,
        "_restore_artifact",
        lambda _path, _payload, **_kwargs: (_ for _ in ()).throw(OSError("restore")),
    )
    monkeypatch.setattr(
        app.state.generation_registry,
        "delete_artifact",
        lambda _artifact_id: (_ for _ in ()).throw(OSError("db")),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as safe_client:
        response = await safe_client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(response, 503, "artifact_delete_failed")
    assert not path.exists()
    history = await client.get("/api/v1/history")
    assert [item["id"] for item in history.json()] == [artifact_id]


@pytest.mark.asyncio
async def test_history_delete_preserves_operational_database_error_after_rollback(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service
    path = service.read_artifact(artifact_id)
    original = path.read_bytes()
    monkeypatch.setattr(
        service,
        "_unlink_validated_artifact",
        lambda candidate: os.unlink(candidate.name, dir_fd=candidate.directory_fd),
    )
    monkeypatch.setattr(
        app.state.generation_registry,
        "delete_artifact",
        lambda _artifact_id: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as safe_client:
        response = await safe_client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(response, 500, "internal_error")
    assert path.read_bytes() == original
    history = await client.get("/api/v1/history")
    assert [item["id"] for item in history.json()] == [artifact_id]


@pytest.mark.asyncio
async def test_artifact_download_rejects_same_size_tampering(core: Any) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    path = app.state.generation_service.read_artifact(artifact_id)
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    response = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")

    _assert_error(response, 404, "artifact_not_found")


@pytest.mark.asyncio
async def test_artifact_download_serves_snapshot_when_path_replaced_after_validation(
    core: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service
    path = service.read_artifact(artifact_id)
    original = path.read_bytes()
    original_open = service.open_artifact

    def replace_after_validation(current_id: str):
        handle = original_open(current_id)
        path.unlink()
        path.symlink_to(tmp_path / "outside.wav")
        (tmp_path / "outside.wav").write_bytes(b"outside")
        return handle

    monkeypatch.setattr(service, "open_artifact", replace_after_validation)
    response = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")

    assert response.status_code == 200
    assert response.content == original


@pytest.mark.asyncio
async def test_artifact_download_rejects_symlink_replacement(core: Any, tmp_path: Path) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    path = app.state.generation_service.read_artifact(artifact_id)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    response = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")

    _assert_error(response, 404, "artifact_not_found")


@pytest.mark.asyncio
async def test_artifact_download_closes_actual_snapshot_after_successful_delivery(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service
    original_open = service.open_artifact
    main_thread = threading.get_ident()
    opened: list[object] = []
    worker_threads: list[int] = []

    def retain_snapshot(current_id: str):
        worker_threads.append(threading.get_ident())
        handle = original_open(current_id)
        opened.append(handle)
        return handle

    monkeypatch.setattr(service, "open_artifact", retain_snapshot)

    response = await client.get(f"/api/v1/artifacts/{artifact_id}/audio")

    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"
    assert len(opened) == 1
    assert opened[0].closed is True
    assert worker_threads == [worker_threads[0]]
    assert worker_threads[0] != main_thread


@pytest.mark.asyncio
async def test_artifact_download_closes_thread_result_when_request_is_cancelled_during_open() -> None:
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

    class FakeGeneration:
        def open_artifact(self, artifact_id: str) -> Handle:
            assert artifact_id == "artifact"
            started.set()
            assert release.wait(timeout=5)
            return handle

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(generation_service=FakeGeneration())
        )
    )
    request_task = asyncio.create_task(download_artifact("artifact", request))
    assert await asyncio.to_thread(started.wait, 1)

    request_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    await asyncio.wait_for(closed.wait(), timeout=2)
    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_artifact_download_closes_thread_result_after_repeated_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()
    closed = asyncio.Event()

    class Handle:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            closed_loop.call_soon_threadsafe(closed.set)

    closed_loop = asyncio.get_running_loop()
    handle = Handle()

    class FakeGeneration:
        def open_artifact(self, artifact_id: str) -> Handle:
            assert artifact_id == "artifact"
            started.set()
            assert release.wait(timeout=5)
            return handle

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(generation_service=FakeGeneration())
        )
    )
    request_task = asyncio.create_task(download_artifact("artifact", request))
    assert await asyncio.to_thread(started.wait, 1)

    request_task.cancel()
    await asyncio.sleep(0)
    request_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    await asyncio.wait_for(closed.wait(), timeout=2)
    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_artifact_stream_closes_handle_once_after_successful_asgi_delivery() -> None:
    class Handle:
        def __init__(self) -> None:
            self.close_calls = 0
            self.reads = 0

        def seek(self, _offset: int, _whence: int = 0) -> int:
            return 0

        def read(self, _size: int) -> bytes:
            self.reads += 1
            return b"payload" if self.reads == 1 else b""

        def close(self) -> None:
            self.close_calls += 1

    handle = Handle()
    response = _ArtifactStreamingResponse(handle, media_type="audio/wav")
    messages: list[dict[str, object]] = []

    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            await asyncio.Event().wait()
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await response(
        {"type": "http", "asgi": {"version": "3.0"}, "method": "GET", "path": "/"},
        receive,
        send,
    )

    assert b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ) == b"payload"
    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_artifact_stream_closes_handle_when_asgi_send_fails() -> None:
    class Handle:
        def __init__(self) -> None:
            self.closed = False
            self.reads = 0

        def read(self, _size: int) -> bytes:
            self.reads += 1
            return b"payload" if self.reads == 1 else b""

        def close(self) -> None:
            self.closed = True

    handle = Handle()
    response = _ArtifactStreamingResponse(handle, media_type="audio/wav")

    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise RuntimeError("client disconnected")

    await response(
        {"type": "http", "asgi": {"version": "3.0"}, "method": "GET", "path": "/"},
        receive,
        send,
    )
    assert handle.closed is True


@pytest.mark.asyncio
async def test_artifact_stream_closes_handle_when_response_is_cancelled() -> None:
    class Handle:
        def __init__(self) -> None:
            self.closed = False

        def read(self, _size: int) -> bytes:
            return b"payload"

        def close(self) -> None:
            self.closed = True

    handle = Handle()
    response = _ArtifactStreamingResponse(handle, media_type="audio/wav")
    started = asyncio.Event()

    async def receive() -> dict[str, object]:
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(
        response(
            {"type": "http", "asgi": {"version": "3.0"}, "method": "GET", "path": "/"},
            receive,
            send,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.closed is True


def test_artifact_stream_closes_handle_on_iterator_failure() -> None:
    class BrokenHandle:
        closed = False

        def seek(self, _offset: int, _whence: int = 0) -> int:
            return 0

        def read(self, _size: int) -> bytes:
            raise OSError("read failed")

        def close(self) -> None:
            self.closed = True

    handle = BrokenHandle()
    iterator = _stream_artifact(handle)
    with pytest.raises(OSError, match="read failed"):
        next(iterator)


@pytest.mark.asyncio
async def test_history_delete_offloads_blocking_artifact_work(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    service = app.state.generation_service
    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fail_closed_delete(_artifact_id: str) -> bool:
        worker_threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(service, "delete_artifact", fail_closed_delete)

    deleted = await client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(deleted, 404, "artifact_not_found")
    assert worker_threads == [worker_threads[0]]
    assert worker_threads[0] != main_thread


@pytest.mark.asyncio
async def test_history_delete_reports_missing_managed_wav_without_hiding_metadata(
    core: Any,
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    artifact_id = completed["artifact_id"]
    assert artifact_id is not None
    path = app.state.generation_service.read_artifact(artifact_id)
    path.unlink()

    deleted = await client.delete(f"/api/v1/history/{artifact_id}")

    _assert_error(deleted, 404, "artifact_not_found")
    history = await client.get("/api/v1/history")
    assert [item["id"] for item in history.json()] == [artifact_id]


@pytest.mark.asyncio
async def test_generation_pcm_route_streams_core_pcm_with_format_headers(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    registry = app.state.generation_registry
    job = registry.create_job(
        job_id="pcm-route-job",
        model_id="fixtures/compatible",
        engine_id="fake",
        voice_id="fake-neutral",
        text="pcm route",
        correlation_id="pcm-route-correlation",
    )
    registry.transition_job(job.id, GenerationState.LOADING)

    async def pcm_stream(job_id: str):
        assert job_id == job.id
        yield b"\x00\x00" * 4
        yield b"\x01\x00" * 4

    monkeypatch.setattr(app.state.generation_service, "subscribe_pcm", pcm_stream)

    response = await client.get(f"/api/v1/generations/{job.id}/pcm")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-audio-sample-rate"] == "48000"
    assert response.headers["x-audio-channels"] == "1"
    assert response.headers["x-audio-encoding"] == "s16le"
    assert response.content == b"\x00\x00" * 4 + b"\x01\x00" * 4


@pytest.mark.asyncio
async def test_generation_routes_use_safe_errors_and_cancel_jobs(core: Any) -> None:
    app, client = core
    missing_model = await client.get("/api/v1/voices", params={"model_id": "missing"})
    _assert_error(missing_model, 404, "model_not_found")

    missing_job = await client.get("/api/v1/generations/missing")
    _assert_error(missing_job, 404, "generation_not_found")

    invalid = await client.post(
        "/api/v1/generations",
        json={"model_id": "missing", "voice_id": "fake-neutral", "text": ""},
    )
    _assert_error(invalid, 422, "generation_request_invalid")

    model_id = await _model_id(client, app)
    created = await client.post(
        "/api/v1/generations",
        json={
            "model_id": model_id,
            "voice_id": "fake-neutral",
            "text": "long " * 4000,
        },
    )
    job_id = created.json()["id"]
    cancelled = await client.post(f"/api/v1/generations/{job_id}/cancel")
    assert cancelled.status_code == 200
    await app.state.generation_service.wait(job_id)
    assert (await client.get(f"/api/v1/generations/{job_id}")).json()["state"] == "cancelled"
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_generation_route_exposes_ordinary_and_recovery_retryability(
    core: Any,
) -> None:
    app, client = core
    registry = app.state.generation_registry
    ordinary = registry.create_job(
        job_id="ordinary-failed-route-job",
        model_id="fixtures/compatible",
        engine_id="fake",
        voice_id="fake-neutral",
        text="ordinary failure",
        correlation_id="ordinary-failed-route-correlation",
    )
    registry.transition_job(ordinary.id, GenerationState.LOADING)
    registry.transition_job(ordinary.id, GenerationState.FAILED, error={
        "code": "provider_unavailable",
        "message": "The engine Worker failed during synthesis.",
        "retryable": False,
    })
    recovery = registry.create_job(
        job_id="recovery-failed-route-job",
        model_id="fixtures/compatible",
        engine_id="fake",
        voice_id="fake-neutral",
        text="recovery failure",
        correlation_id="recovery-failed-route-correlation",
    )
    registry.transition_job(recovery.id, GenerationState.LOADING)
    registry.transition_job(recovery.id, GenerationState.FAILED, error={
        "code": "recovery_required",
        "message": "An interrupted generation requires retry.",
        "retryable": True,
    })

    ordinary_response = await client.get(f"/api/v1/generations/{ordinary.id}")
    recovery_response = await client.get(f"/api/v1/generations/{recovery.id}")

    assert ordinary_response.json()["error"]["retryable"] is False
    assert recovery_response.json()["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_alignment_route_maps_preflight_worker_error(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core
    completed = await _completed_generation(client, app)
    job_id = completed["id"]

    async def fail_alignment(job_id: str, correlation_id: str):
        del job_id, correlation_id
        raise WorkerOperationError(
            engine_pb2.WorkerError(
                code="alignment_preflight_failed",
                message="capability refresh failed",
                retryable=False,
            )
        )

    monkeypatch.setattr(app.state.generation_service, "request_alignment", fail_alignment)
    response = await client.post(f"/api/v1/generations/{job_id}/alignment")

    error = _assert_error(response, 503, "alignment_preflight_failed")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_voice_list_worker_error_preserves_code_and_retryability(
    core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = core

    async def fail_list_voices(model_id: str):
        del model_id
        raise WorkerOperationError(
            engine_pb2.WorkerError(
                code="voice_list_failed",
                message="VieNeu preset voices could not be listed",
                retryable=True,
            )
        )

    monkeypatch.setattr(app.state.generation_service, "list_voices", fail_list_voices)

    response = await client.get("/api/v1/voices", params={"model_id": "fixtures/compatible"})

    error = _assert_error(response, 503, "voice_list_failed")
    assert error["retryable"] is True


@pytest.mark.asyncio
async def test_public_generation_reads_hide_artifacts_until_job_completion(core: Any) -> None:
    app, client = core
    registry = app.state.generation_registry
    job = registry.create_job(
        job_id="finalizing-http-job",
        model_id="fixtures/compatible",
        engine_id="fake",
        voice_id="fake-neutral",
        text="finalizing",
        correlation_id="finalizing-http-correlation",
    )
    registry.transition_job(job.id, GenerationState.LOADING)
    registry.transition_job(job.id, GenerationState.GENERATING)
    registry.transition_job(job.id, GenerationState.FINALIZING)
    artifact_path = app.state.storage_layout.audio / "generation-finalizing-http-job.wav"
    artifact_path.write_bytes(b"private artifact")
    registry.create_artifact(
        job_id=job.id,
        artifact_id="artifact-finalizing-http-job",
        path="audio/generation-finalizing-http-job.wav",
        byte_size=len(b"private artifact"),
        sha256="a" * 64,
        sample_rate=48_000,
        channel_count=1,
        frame_count=0,
    )

    detail = await client.get(f"/api/v1/generations/{job.id}")
    history = await client.get("/api/v1/history")
    audio = await client.get("/api/v1/artifacts/artifact-finalizing-http-job/audio")

    assert detail.status_code == 200
    assert detail.json()["artifact_id"] is None
    assert detail.json()["artifact_url"] is None
    assert history.status_code == 200
    assert history.json() == []
    assert audio.status_code == 404


@pytest.mark.asyncio
async def test_non_retained_generation_is_not_downloadable_or_in_history(core: Any) -> None:
    app, client = core
    job = await _completed_generation(client, app, retain_artifact=False)
    assert job["artifact_id"] is None
    assert job["artifact_url"] is None
    assert (await client.get("/api/v1/history")).json() == []
    assert tuple(app.state.storage_layout.staging.iterdir()) == ()
    assert tuple(app.state.storage_layout.audio.iterdir()) == ()


@pytest.mark.asyncio
async def test_generation_accepts_reference_id_and_rejects_zero_or_two_sources(core: Any) -> None:
    app, client = core
    model_id = await _model_id(client, app)
    neither = await client.post(
        "/api/v1/generations", json={"model_id": model_id, "text": "missing source"}
    )
    _assert_error(neither, 422, "reference_request_invalid")

    both = await client.post(
        "/api/v1/generations",
        json={
            "model_id": model_id,
            "voice_id": "fake-neutral",
            "reference_id": "reference-one",
            "text": "ambiguous source",
        },
    )
    _assert_error(both, 422, "reference_request_invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["voice_id", "reference_id"])
async def test_generation_rejects_empty_string_sources(core: Any, field: str) -> None:
    app, client = core
    model_id = await _model_id(client, app)
    response = await client.post(
        "/api/v1/generations",
        json={"model_id": model_id, field: "", "text": "empty source"},
    )
    _assert_error(response, 422, "reference_request_invalid")


@pytest.mark.asyncio
async def test_generation_maps_unknown_reference_to_stable_not_found(core: Any) -> None:
    app, client = core
    model_id = await _model_id(client, app)
    response = await client.post(
        "/api/v1/generations",
        json={"model_id": model_id, "reference_id": "missing-reference", "text": "missing"},
    )
    _assert_error(response, 404, "reference_not_found")


@pytest.mark.asyncio
async def test_generation_maps_expired_reference_to_stable_not_found(core: Any) -> None:
    app, client = core
    uploaded = await client.post(
        "/api/v1/references",
        data={"model_id": "fixtures/compatible"},
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 201
    reference_id = uploaded.json()["id"]
    with app.state.reference_service._database.transaction() as connection:
        connection.execute(
            "UPDATE reference_recordings SET state = 'expired' WHERE id = ?", (reference_id,)
        )
    response = await client.post(
        "/api/v1/generations",
        json={"model_id": "fixtures/compatible", "reference_id": reference_id, "text": "expired"},
    )
    _assert_error(response, 404, "reference_not_found")


@pytest.mark.asyncio
async def test_generation_keeps_backward_compatible_preset_voice_json(core: Any) -> None:
    app, client = core
    model_id = await _model_id(client, app)
    response = await client.post(
        "/api/v1/generations",
        json={"model_id": model_id, "voice_id": "fake-neutral", "text": "preset"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["voice_id"] == "fake-neutral"
    assert body["reference_id"] is None
    await app.state.generation_service.wait(body["id"])
