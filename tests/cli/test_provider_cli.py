from typer.testing import CliRunner

from tts_studio.cli import app
from tts_studio.generated.api import ProviderResponse


def test_provider_list_json_uses_core_client(monkeypatch) -> None:
    profile = ProviderResponse(
        id="provider-one", kind="openai_compatible", label="Local",
        base_url="http://127.0.0.1:9000/v1", model="tts-1", api_key_env="TTS_PROVIDER_KEY",
        created_at="2026-09-08T00:00:00Z", updated_at="2026-09-08T00:00:00Z",
    )
    monkeypatch.setattr("tts_studio.cli.CoreClient.list_providers", lambda self: [profile])
    result = CliRunner().invoke(app, ["providers", "list", "--json"])
    assert result.exit_code == 0
    assert "TTS_PROVIDER_KEY" in result.stdout
    assert '"api_key"' not in result.stdout


def test_provider_update_forwards_profile_fields(monkeypatch) -> None:
    calls = {}

    def update(self, provider_id, **fields):
        calls.update(provider_id=provider_id, **fields)
        return ProviderResponse(
            id=provider_id,
            kind="openai_compatible",
            label=fields["label"],
            base_url=fields["base_url"],
            model=fields["model"],
            api_key_env=fields["api_key_env"],
            created_at="2026-09-08T00:00:00Z",
            updated_at="2026-09-08T00:00:00Z",
        )

    monkeypatch.setattr("tts_studio.cli.CoreClient.update_provider", update)
    result = CliRunner().invoke(
        app,
        [
            "providers",
            "update",
            "provider-one",
            "--label",
            "Updated",
            "--base-url",
            "http://127.0.0.1:9000/v1",
            "--model",
            "tts-1",
            "--api-key-env",
            "TTS_PROVIDER_KEY",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "provider_id": "provider-one",
        "label": "Updated",
        "base_url": "http://127.0.0.1:9000/v1",
        "model": "tts-1",
        "api_key_env": "TTS_PROVIDER_KEY",
    }
