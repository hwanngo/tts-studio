"""CLI coverage through an isolated Core's public HTTP interface."""

from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Thread
from typing import NoReturn, cast

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from tts_studio.cli import app
from tts_studio.client import CoreClient
from tts_studio.config import Settings
from tts_studio.server.app import create_app

runner = CliRunner()


@pytest.fixture
def core_url(tmp_path: Path) -> Iterator[str]:
    """Run an isolated Core and its fake Worker over real HTTP and gRPC."""
    if sys.platform == "win32":
        pytest.skip("threaded Uvicorn Core fixtures cannot start Worker adapters on Windows CI")
    data_dir = tmp_path / ".tts-studio"
    core = create_app(Settings.resolve(data_dir), include_test_adapters=True)

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


def _download(core_url: str, repository_id: str = "fixtures/compatible") -> dict[str, object]:
    result = runner.invoke(
        app,
        [
            "models",
            "download",
            repository_id,
            "--revision",
            "main",
            "--variant",
            "fp32",
            "--poll-interval",
            "0.01",
            "--json",
            "--url",
            core_url,
        ],
    )
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert isinstance(document, dict)
    return document


def test_validate_uses_public_core_and_prints_compatibility(core_url: str) -> None:
    result = runner.invoke(
        app,
        [
            "models",
            "validate",
            "fixtures/compatible",
            "--revision",
            "main",
            "--url",
            core_url,
        ],
    )

    assert result.exit_code == 0
    assert "Compatible: yes" in result.output
    assert "Selected engine: fake" in result.output


def test_validate_json_is_a_single_core_response_document(core_url: str) -> None:
    result = runner.invoke(
        app,
        ["models", "validate", "fixtures/incompatible", "--json", "--url", core_url],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "compatible": False,
        "repository_id": "fixtures/incompatible",
        "requested_revision": None,
        "results": [
            {
                "available": True,
                "available_variants": [],
                "compatible": False,
                "engine_id": "fake",
                "engine_version": "0.2.0",
                "error_code": "model_incompatible",
                "error_retryable": False,
                "estimated_bytes": None,
                "evidence": [
                    {
                        "code": "fake_fixture_incompatible",
                        "message": "The fake adapter rejected this deterministic fixture",
                    }
                ],
                "required_files": [],
                "resolved_commit": None,
            },
            {
                "available": True,
                "available_variants": [],
                "compatible": False,
                "engine_id": "openai_compatible",
                "engine_version": "0.1.0",
                "error_code": "model_incompatible",
                "error_retryable": False,
                "estimated_bytes": None,
                "evidence": [],
                "required_files": [],
                "resolved_commit": None,
            },
            {
                "available": True,
                "available_variants": [],
                "compatible": False,
                "engine_id": "vieneu",
                "engine_version": "unknown",
                "error_code": "model_incompatible",
                "error_retryable": False,
                "estimated_bytes": None,
                "evidence": [],
                "required_files": [],
                "resolved_commit": None,
            },
        ],
        "selected_engine_id": None,
    }


def test_list_prints_core_managed_installation(core_url: str) -> None:
    _download(core_url)

    result = runner.invoke(app, ["models", "list", "--url", core_url])

    assert result.exit_code == 0
    assert "fixtures/compatible" in result.output
    assert "Variant: fp32" in result.output
    assert "State: unloaded" in result.output
    assert "Cache: models/" in result.output


def test_download_waits_and_labels_unknown_total_progress(core_url: str) -> None:
    result = runner.invoke(
        app,
        [
            "models",
            "download",
            "fixtures/slow",
            "--variant",
            "fp32",
            "--poll-interval",
            "0.01",
            "--url",
            core_url,
        ],
    )

    assert result.exit_code == 0
    assert "Job:" in result.output
    assert "bytes downloaded (size unavailable)" in result.output
    assert "Model:" in result.output


def test_download_no_wait_json_returns_the_core_queued_job(core_url: str) -> None:
    result = runner.invoke(
        app,
        [
            "models",
            "download",
            "fixtures/compatible",
            "--no-wait",
            "--json",
            "--url",
            core_url,
        ],
    )

    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["repository_id"] == "fixtures/compatible"
    assert document["state"] == "queued"
    assert document["target_model_id"] is None


def test_cancel_uses_the_public_core_endpoint(core_url: str) -> None:
    started = runner.invoke(
        app,
        [
            "models",
            "download",
            "fixtures/slow",
            "--variant",
            "fp32",
            "--no-wait",
            "--json",
            "--url",
            core_url,
        ],
    )
    download_id = json.loads(started.output)["id"]

    result = runner.invoke(
        app,
        ["models", "cancel", download_id, "--url", core_url],
    )

    assert started.exit_code == 0
    assert result.exit_code == 0
    assert f"Cancellation requested for {download_id}." in result.output


def test_remove_uses_the_public_core_endpoint(core_url: str) -> None:
    model_id = _download(core_url)["target_model_id"]
    assert isinstance(model_id, str)

    result = runner.invoke(
        app,
        ["models", "remove", model_id, "--url", core_url],
    )
    listing = runner.invoke(app, ["models", "list", "--json", "--url", core_url])

    assert result.exit_code == 0
    assert f"Removed {model_id}." in result.output
    assert listing.exit_code == 0
    assert json.loads(listing.output) == []


def test_api_failure_has_stable_exit_and_safe_error_rendering(core_url: str) -> None:
    result = runner.invoke(
        app,
        ["models", "download", "fixtures/incompatible", "--url", core_url],
    )

    assert result.exit_code == 4
    assert "model_incompatible: No installed engine adapter can run this repository." in (
        result.output
    )
    assert "(correlation:" in result.output
    assert "Traceback" not in result.output


def test_api_failure_json_is_the_core_error_envelope(core_url: str) -> None:
    result = runner.invoke(
        app,
        [
            "models",
            "download",
            "fixtures/incompatible",
            "--json",
            "--url",
            core_url,
        ],
    )

    assert result.exit_code == 4
    error = json.loads(result.output)["error"]
    assert error["code"] == "model_incompatible"
    assert error["message"] == "No installed engine adapter can run this repository."
    assert error["source"] == "model_registry"
    assert error["retryable"] is False
    assert isinstance(error["correlation_id"], str)
    assert error["details"] == {}


def test_models_transport_failure_keeps_core_unavailable_exit_three() -> None:
    result = runner.invoke(app, ["models", "list", "--url", "http://127.0.0.1:1"])

    assert result.exit_code == 3
    assert "Unable to reach Core" in result.output


def test_models_transport_failure_preserves_json_mode() -> None:
    result = runner.invoke(
        app,
        ["models", "list", "--json", "--url", "http://127.0.0.1:1"],
    )

    assert result.exit_code == 3
    assert json.loads(result.output) == {
        "error": {
            "code": "core_unavailable",
            "correlation_id": "unavailable",
            "details": {},
            "message": "The Core is unavailable.",
            "retryable": True,
            "source": "transport",
        }
    }
    assert "Traceback" not in result.output


class RequestObserved(Exception):
    pass


def test_model_operations_use_operation_appropriate_read_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, float | None]] = []

    def observe_request(
        client: httpx.Client,
        method: str,
        path: str,
        **kwargs: object,
    ) -> NoReturn:
        del method
        timeout = kwargs.get("timeout", client.timeout)
        read_timeout = timeout.read if isinstance(timeout, httpx.Timeout) else cast(float, timeout)
        observed.append((path, read_timeout))
        raise RequestObserved

    monkeypatch.setattr(httpx.Client, "request", observe_request)
    client = CoreClient("http://127.0.0.1:7860")
    operations: tuple[Callable[[], object], ...] = (
        lambda: client.validate_model("fixtures/compatible"),
        lambda: client.start_download("fixtures/compatible", variant="fp32"),
        lambda: client.remove_model("model-id"),
    )

    for operation in operations:
        with pytest.raises(RequestObserved):
            operation()

    assert observed == [
        ("/api/v1/models/validate", 15.0),
        ("/api/v1/downloads", 30.0),
        ("/api/v1/models/model-id/remove", 60.0),
    ]


def test_core_client_forwards_configured_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setenv("TTS_STUDIO_API_TOKEN_ENV", "TTS_STUDIO_TEST_API_TOKEN")
    monkeypatch.setenv("TTS_STUDIO_TEST_API_TOKEN", "private-token-value")

    def observe_request(
        client: httpx.Client,
        method: str,
        path: str,
        **kwargs: object,
    ) -> NoReturn:
        del client, method, path
        observed.update(kwargs)
        raise RequestObserved

    monkeypatch.setattr(httpx.Client, "request", observe_request)

    with pytest.raises(RequestObserved):
        CoreClient("http://127.0.0.1:7860").system_status()

    assert observed["headers"] == {"Authorization": "Bearer private-token-value"}
