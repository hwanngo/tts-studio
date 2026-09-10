from __future__ import annotations

import httpx

from tts_studio import client as client_module
from tts_studio.client import CoreApiError, CoreClient
from tts_studio.generated.api import (
    ClearRetentionResponse,
    RuntimeResponse,
    ServiceOperationResponse,
    ServiceResponse,
    SettingsResponse,
)


def _response(method: str, url: str, *, json: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=json, request=httpx.Request(method, url))


def test_settings_and_runtime_clients_use_public_routes(monkeypatch) -> None:
    calls: list[tuple[str, str, object | None, dict[str, str]]] = []

    def request(self, method: str, url: str, **kwargs):
        calls.append((method, url, kwargs.get("json"), kwargs.get("headers", {})))
        if url.endswith("/settings") and method == "GET":
            return _response(method, url, json={
                "retain_audio_by_default": True, "artifact_max_age_days": None,
                "artifact_max_storage_bytes": None, "api_token_env": "TTS_STUDIO_API_TOKEN",
                "host": "127.0.0.1", "port": 7860, "restart_required": False,
                "retention": {"retained_count": 0, "retained_bytes": 0,
                              "max_age_days": None, "max_storage_bytes": None},
            })
        if url.endswith("/settings") and method == "PATCH":
            return _response(method, url, json={
                "retain_audio_by_default": False, "artifact_max_age_days": 7,
                "artifact_max_storage_bytes": None, "api_token_env": "TTS_STUDIO_API_TOKEN",
                "host": "127.0.0.1", "port": 7860, "restart_required": False,
                "retention": {"retained_count": 0, "retained_bytes": 0,
                              "max_age_days": 7, "max_storage_bytes": None},
            })
        if url.endswith("/runtime"):
            return _response(method, url, json={"version": "1", "host": "127.0.0.1", "port": 7860,
                "data_dir": "/tmp/data", "generation_status": "idle", "active_generations": {},
                "workers": [], "storage_accessible": True, "database_accessible": True,
                "startup_diagnostics": {}})
        if url.endswith("/service"):
            return _response(method, url, json={"status": "installed", "installed": True,
                "running": True, "healthy": True, "message": "ok"})
        if "/service/" in url:
            return _response(method, url, json={"operation": url.rsplit("/", 1)[-1],
                "changed": True, "message": "ok"})
        if url.endswith("/retention/clear"):
            return _response(method, url, json={"deleted": 2, "skipped": 0, "failed": 0, "issues": []})
        raise AssertionError((method, url))

    monkeypatch.setattr(httpx.Client, "request", request)
    client = CoreClient("http://127.0.0.1:7860")
    settings = client.settings()
    assert isinstance(settings, SettingsResponse)
    assert settings.api_token_env == "TTS_STUDIO_API_TOKEN"
    assert settings.retention.retained_count == 0
    updated = client.update_settings(retain_audio_by_default=False, artifact_max_age_days=7)
    assert updated.restart_required is False
    cleared = client.clear_retention()
    assert isinstance(cleared, ClearRetentionResponse)
    assert cleared.deleted == 2
    runtime = client.runtime_status()
    assert isinstance(runtime, RuntimeResponse)
    assert runtime.storage_accessible is True
    service = client.service_status()
    assert isinstance(service, ServiceResponse)
    assert service.healthy is True
    operations = [client.install_service(), client.uninstall_service(), client.restart_service()]
    assert all(isinstance(result, ServiceOperationResponse) for result in operations)

    assert calls[1][:3] == ("PATCH", "/api/v1/settings", {
        "retain_audio_by_default": False, "artifact_max_age_days": 7
    })
    assert calls[2][:3] == ("POST", "/api/v1/settings/retention/clear", {"confirm": True})
    assert [calls[index][2] for index in (5, 6, 7)] == [{"confirm": True}] * 3
    assert all("'api_token':" not in str(body) for _, _, body, _ in calls)


def test_update_settings_preserves_explicit_null(monkeypatch) -> None:
    seen: list[object] = []

    def request(self, method: str, url: str, **kwargs):
        seen.append(kwargs["json"])
        return _response(method, url, json={
            "retain_audio_by_default": True, "artifact_max_age_days": None,
            "artifact_max_storage_bytes": None, "api_token_env": None,
            "host": "127.0.0.1", "port": 7860, "restart_required": False,
            "retention": {"retained_count": 0, "retained_bytes": 0,
                          "max_age_days": None, "max_storage_bytes": None},
        })

    monkeypatch.setattr(httpx.Client, "request", request)
    CoreClient("http://127.0.0.1:7860").update_settings(artifact_max_age_days=None)
    assert seen == [{"artifact_max_age_days": None}]


def test_client_forwards_configured_auth_without_payload_secret(monkeypatch) -> None:
    monkeypatch.setenv("TTS_STUDIO_API_TOKEN_ENV", "TOKEN_NAME")
    monkeypatch.setenv("TOKEN_NAME", "secret-value")
    seen: dict[str, object] = {}

    def request(self, method: str, url: str, **kwargs):
        seen.update(kwargs)
        return _response(method, url, json={
            "retain_audio_by_default": False, "artifact_max_age_days": 7,
            "artifact_max_storage_bytes": None, "api_token_env": "TOKEN_NAME",
            "host": "127.0.0.1", "port": 7860, "restart_required": False,
            "retention": {"retained_count": 0, "retained_bytes": 0,
                          "max_age_days": 7, "max_storage_bytes": None},
        })

    monkeypatch.setattr(httpx.Client, "request", request)
    CoreClient("http://127.0.0.1:7860").update_settings(retain_audio_by_default=False)
    assert seen["headers"] == {"Authorization": "Bearer secret-value"}
    assert seen["json"] == {"retain_audio_by_default": False}
    assert "secret-value" not in str(seen["json"])


def test_client_preserves_stable_api_errors(monkeypatch) -> None:
    def request(self, method: str, url: str, **kwargs):
        return _response(method, url, json={"error": {"code": "confirmation_required",
            "message": "Explicit confirmation is required.", "source": "settings",
            "retryable": False, "correlation_id": "c1"}}, status_code=422)

    monkeypatch.setattr(httpx.Client, "request", request)
    try:
        CoreClient("http://127.0.0.1:7860").clear_retention()
    except CoreApiError as error:
        assert str(error) == "Explicit confirmation is required."
    else:
        raise AssertionError("expected stable CoreApiError")


def test_create_generation_omits_optional_fields_unless_explicit(monkeypatch) -> None:
    seen: list[object] = []

    def request(self, method: str, url: str, **kwargs):
        seen.append(kwargs["json"])
        return _response(method, url, json={
            "id": "job-1", "model_id": "model", "engine_id": "fake", "voice_id": "voice",
            "reference_id": None, "saved_voice_id": None, "text": "hello", "retain_artifact": True,
            "state": "queued", "bytes_written": 0, "frame_count": 0, "sample_rate": None,
            "channel_count": None, "artifact_id": None, "artifact_url": None, "error": None,
            "cancellation_requested": False, "correlation_id": "c1", "created_at": "now",
            "updated_at": "now",
        })

    monkeypatch.setattr(httpx.Client, "request", request)
    client = CoreClient("http://127.0.0.1:7860")
    client.create_generation("model", "voice", "hello")
    client.create_generation("model", "voice", "hello", retain_artifact=True)
    client.create_generation("model", "voice", "hello", retain_artifact=False)
    client.create_generation("model", "voice", "hello", speed=1.25, pitch=-0.5, volume=0.75)
    assert seen == [
        {"model_id": "model", "voice_id": "voice", "text": "hello"},
        {"model_id": "model", "voice_id": "voice", "text": "hello", "retain_artifact": True},
        {"model_id": "model", "voice_id": "voice", "text": "hello", "retain_artifact": False},
        {
            "model_id": "model",
            "voice_id": "voice",
            "text": "hello",
            "speed": 1.25,
            "pitch": -0.5,
            "volume": 0.75,
        },
    ]


def test_preview_voice_posts_typed_request_and_returns_wav_bytes(monkeypatch) -> None:
    seen: list[tuple[str, str, object, object]] = []

    def request(self, method: str, url: str, **kwargs):
        seen.append((method, url, kwargs.get("json"), kwargs.get("timeout")))
        return httpx.Response(
            200,
            content=b"RIFF-preview",
            headers={"Content-Type": "audio/wav"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx.Client, "request", request)

    result = CoreClient("http://127.0.0.1:7860").preview_voice(
        "model/one", "voice one", "Preview this voice."
    )

    assert result == b"RIFF-preview"
    assert seen == [
        (
            "POST",
            "/api/v1/voices/preview",
            {"model_id": "model/one", "voice_id": "voice one", "text": "Preview this voice."},
            client_module._GENERATION_CREATION_TIMEOUT,
        )
    ]
