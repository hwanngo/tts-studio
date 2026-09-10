"""CLI behavior through an isolated Core's public HTTP interface."""

from __future__ import annotations

import ctypes
import json
import socket
import sys
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from typer.testing import CliRunner

from tts_studio.cli import app
from tts_studio.client import CoreClient
from tts_studio.config import Settings
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.storage.db import Database

runner = CliRunner()


def _identity_bound_unlink_available() -> bool:
    try:
        _ = ctypes.CDLL(None).funlinkat
    except (AttributeError, OSError):
        return False
    return True


@pytest.fixture
def core_url(tmp_path: Path) -> Iterator[str]:
    if sys.platform == "win32":
        pytest.skip("threaded Uvicorn Core fixtures cannot start Worker adapters on Windows CI")
    core = create_app(
        Settings.resolve(tmp_path / ".tts-studio"), include_test_adapters=True
    )
    database = Database(core.state.storage_layout.database_path)
    database.migrate()
    _activate_model(ModelRegistry(database))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()
    server = uvicorn.Server(uvicorn.Config(core, log_level="critical"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        pytest.fail("Core did not start within five seconds")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


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
        job_id="download-generation-cli",
        repository_id="fixtures/compatible",
        requested_revision=None,
        engine_installation_id="fake@0.2.0",
        staging_path="staging/download-generation-cli",
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
        cache_path="models/model-generation-cli",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


def test_generation_command_help_excludes_style() -> None:
    result = runner.invoke(app, ["speak", "--help"])

    assert result.exit_code == 0
    assert "style" not in result.output.casefold()


def test_voices_jobs_and_history_commands_use_public_core(
    core_url: str, tmp_path: Path
) -> None:
    model_id = "fixtures/compatible"
    voices = runner.invoke(
        app, ["voices", "list", "--model", model_id, "--json", "--url", core_url]
    )
    assert voices.exit_code == 0
    assert json.loads(voices.output)[0]["id"] == "fake-neutral"

    CoreClient(core_url).update_settings(retain_audio_by_default=False)
    output = tmp_path / "speech.wav"
    speak = runner.invoke(
        app,
        [
            "speak",
            "hello from cli",
            "--model",
            model_id,
            "--voice",
            "fake-neutral",
            "--output",
            str(output),
            "--url",
            core_url,
        ],
    )
    assert speak.exit_code == 0
    with wave.open(str(output), "rb") as wav:
        assert wav.getframerate() == 48_000
        assert wav.getnchannels() == 1
    assert output.read_bytes()[:4] == b"RIFF"

    jobs = runner.invoke(app, ["jobs", "list", "--json", "--url", core_url])
    assert jobs.exit_code == 0
    job_document = json.loads(jobs.output)
    assert job_document[0]["state"] == "completed"
    job_id = job_document[0]["id"]
    detail = runner.invoke(app, ["jobs", "get", job_id, "--json", "--url", core_url])
    assert detail.exit_code == 0
    assert json.loads(detail.output)["id"] == job_id

    history = runner.invoke(app, ["history", "list", "--json", "--url", core_url])
    assert history.exit_code == 0
    artifact_id = json.loads(history.output)[0]["id"]
    deleted = runner.invoke(
        app, ["history", "delete", artifact_id, "--json", "--url", core_url]
    )
    if _identity_bound_unlink_available():
        assert deleted.exit_code == 0
        assert json.loads(deleted.output) == {"id": artifact_id, "deleted": True}
    else:
        assert deleted.exit_code == 4
        assert "artifact_delete_failed" in deleted.output


def test_speak_core_failure_has_stable_exit(core_url: str) -> None:
    result = runner.invoke(
        app,
        [
            "speak",
            "hello",
            "--model",
            "missing",
            "--voice",
            "fake-neutral",
            "--output",
            "/tmp/tts-missing.wav",
            "--url",
            core_url,
        ],
    )

    assert result.exit_code == 4
    assert "model_not_found" in result.output
    assert "Traceback" not in result.output
