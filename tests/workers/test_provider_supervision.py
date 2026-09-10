import os
from pathlib import Path

from tts_studio.workers.adapters import discover_adapters
from tts_studio.workers.supervisor import _worker_environment


def test_provider_worker_is_discovered_from_its_locked_project() -> None:
    repository = Path(__file__).resolve().parents[2]

    descriptor = next(
        item for item in discover_adapters(repository) if item.engine_id == "openai_compatible"
    )

    assert descriptor.launch.command[:3] == ("uv", "run", "--frozen")
    assert descriptor.launch.command[-1] == "tts-studio-openai-compatible-worker"
    assert descriptor.launch.cwd == repository.resolve()


def test_worker_environment_does_not_inherit_provider_credentials(monkeypatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER_KEY", "secret-value")
    monkeypatch.setenv("UNRELATED_APPLICATION_SECRET", "other-secret")

    environment = _worker_environment()

    assert "TTS_PROVIDER_KEY" not in environment
    assert "UNRELATED_APPLICATION_SECRET" not in environment
    assert environment.get("PATH") == os.environ.get("PATH")
