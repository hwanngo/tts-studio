from pathlib import Path

import pytest

from tts_studio.generation.registry import GenerationRegistry
from tts_studio.providers.domain import ProviderProfile
from tts_studio.providers.registry import ProviderInUseError, ProviderRegistry
from tts_studio.providers.service import (
    InvalidProviderProfileError,
    ProviderSecretMissingError,
    ProviderService,
)
from tts_studio.storage.db import Database


def test_provider_profile_round_trip_never_persists_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    registry = ProviderRegistry(database)
    profile = registry.create(
        kind="openai_compatible", label="Local", base_url="http://127.0.0.1:9000/v1",
        model="tts-1", api_key_env="TTS_PROVIDER_KEY",
    )
    assert isinstance(profile, ProviderProfile)
    monkeypatch.setenv("TTS_PROVIDER_KEY", "secret-value")
    assert ProviderService(registry).resolve_api_key(profile) == "secret-value"
    with database.read() as connection:
        columns = connection.execute("PRAGMA table_info(provider_profiles)").fetchall()
        values = connection.execute("SELECT * FROM provider_profiles").fetchone()
    assert "api_key" not in {row[1] for row in columns}
    assert "secret-value" not in repr(tuple(values))


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com/v1",
        "ftp://127.0.0.1/v1",
        "https://",
        "https://user:password@provider.example/v1",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://169.254.169.254/v1",
        "https://10.0.0.1/v1",
        "https://provider.example:abc/v1",
    ],
)
def test_provider_profile_rejects_unsafe_base_url(tmp_path: Path, base_url: str) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    with pytest.raises(InvalidProviderProfileError):
        ProviderRegistry(database).create(
            kind="openai_compatible", label="Bad", base_url=base_url,
            model="tts-1", api_key_env="TTS_PROVIDER_KEY",
        )


def test_missing_provider_secret_is_a_safe_configuration_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    profile = ProviderRegistry(database).create(
        kind="openai_compatible", label="Cloud", base_url="https://provider.example/v1",
        model="tts-1", api_key_env="TTS_PROVIDER_KEY",
    )
    monkeypatch.delenv("TTS_PROVIDER_KEY", raising=False)
    with pytest.raises(ProviderSecretMissingError, match="TTS_PROVIDER_KEY"):
        ProviderService(ProviderRegistry(database)).resolve_api_key(profile)


def test_provider_delete_refuses_an_active_generation_pin(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    providers = ProviderRegistry(database)
    profile = providers.create(
        kind="openai_compatible", label="Cloud", base_url="https://provider.example/v1",
        model="tts-1", api_key_env="TTS_PROVIDER_KEY",
    )
    GenerationRegistry(database).create_job(
        model_id=f"provider:{profile.id}", engine_id="openai_compatible", voice_id="alloy",
        provider_id=profile.id, text="hello", correlation_id="correlation",
    )
    with pytest.raises(ProviderInUseError):
        providers.delete(profile.id)
