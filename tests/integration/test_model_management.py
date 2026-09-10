from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPOSITORY_ROOT / "scripts" / "build_distribution.py"


def _environment_executable(environment: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe" if name in {"python", "tts", "tts-studio-fake-worker"} else ""
        return environment / "Scripts" / f"{name}{suffix}"
    return environment / "bin" / name


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> tuple[int, Any]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:
        if response.status == 204:
            return response.status, None
        return response.status, json.load(response)


def _wait_for_ready_core(base_url: str, server: subprocess.Popen[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if server.poll() is not None:
            output = server.stdout.read() if server.stdout is not None else ""
            raise AssertionError(f"installed server exited before becoming ready:\n{output}")
        try:
            status, document = _json_request(f"{base_url}/api/v1/system")
        except URLError:
            time.sleep(0.05)
            continue
        workers = document.get("workers", [])
        if status == 200 and any(
            worker.get("engine_id") == "fake" and worker.get("status") == "ready"
            for worker in workers
        ):
            return document
        time.sleep(0.05)
    output = ""
    if server.poll() is not None and server.stdout is not None:
        output = server.stdout.read()
    raise AssertionError(f"installed Core did not report a ready fake adapter:\n{output}")


def _wait_for_download(base_url: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, job = _json_request(f"{base_url}/api/v1/downloads/{job_id}")
        assert status == 200
        if job["state"] in {"completed", "cancelled", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("installed Core download did not reach a terminal state")


def _read_sse_event(response: Any) -> dict[str, Any]:
    fields: dict[str, str] = {}
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("SSE stream ended before the next event")
        decoded = line.decode("utf-8").rstrip("\r\n")
        if not decoded:
            return {
                "id": int(fields["id"]),
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        field, separator, value = decoded.partition(":")
        if separator:
            fields[field] = value.lstrip()


def _stop_server(server: subprocess.Popen[str]) -> None:
    if server.poll() is None:
        try:
            server.terminate()
        except ProcessLookupError:
            pass
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def _safe_managed_model_path(data_dir: Path, cache_path: str) -> Path:
    relative = PurePosixPath(cache_path)
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "models"
        or "\\" in cache_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise AssertionError(f"unsafe managed model path: {cache_path!r}")
    models_root = data_dir / "models"
    root_metadata = models_root.lstat()
    if models_root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise AssertionError("managed models root is unsafe")
    model_directory = models_root / relative.parts[1]
    if model_directory.parent != models_root:
        raise AssertionError("managed model path escaped its direct parent")
    directory_metadata = model_directory.lstat()
    if model_directory.is_symlink() or not stat.S_ISDIR(directory_metadata.st_mode):
        raise AssertionError("activated model directory is unsafe")
    return model_directory


def _assert_managed_model_files(model_directory: Path) -> None:
    children = tuple(model_directory.iterdir())
    assert {child.name for child in children} == {"config.json", "model.bin"}
    for child in children:
        metadata = child.lstat()
        assert not child.is_symlink()
        assert stat.S_ISREG(metadata.st_mode)


def test_built_artifacts_complete_model_management_workflow(tmp_path: Path) -> None:
    distribution_dir = tmp_path / "dist"
    core_environment = tmp_path / "core-environment"
    worker_environment = tmp_path / "worker-environment"
    data_dir = tmp_path / "data"

    subprocess.run(
        [
            sys.executable,
            str(_BUILD_SCRIPT),
            "--include-test-adapters",
            "--out-dir",
            str(distribution_dir),
        ],
        cwd=_REPOSITORY_ROOT,
        check=True,
    )
    root_wheels = tuple(distribution_dir.glob("tts_studio-*.whl"))
    protocol_wheels = tuple(distribution_dir.glob("tts_studio_protocol-*.whl"))
    worker_sdk_wheels = tuple(distribution_dir.glob("tts_studio_worker_sdk-*.whl"))
    fake_worker_wheels = tuple(distribution_dir.glob("tts_studio_fake_worker-*.whl"))
    assert len(root_wheels) == 1
    assert len(protocol_wheels) == 1
    assert len(worker_sdk_wheels) == 1
    assert len(fake_worker_wheels) == 1

    isolated_environment = os.environ.copy()
    isolated_environment.pop("MYPYPATH", None)
    isolated_environment.pop("PYTHONPATH", None)

    for environment in (core_environment, worker_environment):
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(environment)],
            cwd=tmp_path,
            check=True,
            env=isolated_environment,
        )

    core_python = _environment_executable(core_environment, "python")
    worker_python = _environment_executable(worker_environment, "python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(core_python),
            "--find-links",
            str(distribution_dir),
            str(protocol_wheels[0]),
            str(root_wheels[0]),
        ],
        cwd=tmp_path,
        check=True,
        env=isolated_environment,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(worker_python),
            "--find-links",
            str(distribution_dir),
            str(protocol_wheels[0]),
            str(worker_sdk_wheels[0]),
            str(fake_worker_wheels[0]),
        ],
        cwd=tmp_path,
        check=True,
        env=isolated_environment,
    )
    for python in (core_python, worker_python):
        subprocess.run(
            ["uv", "pip", "check", "--python", str(python)],
            cwd=tmp_path,
            check=True,
            env=isolated_environment,
        )

    core_import = subprocess.run(
        [
            str(core_python),
            "-c",
            (
                "import importlib.util; "
                "from tts_studio.generated.api import ModelValidationResponse; "
                "assert importlib.util.find_spec('tts_studio_fake_worker') is None; "
                "assert importlib.util.find_spec('tts_studio_worker_sdk') is None; "
                "print(ModelValidationResponse.__name__)"
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env=isolated_environment,
    )
    assert core_import.stdout.strip() == "ModelValidationResponse"
    subprocess.run(
        [
            str(worker_python),
            "-c",
            (
                "import importlib.util, tts_studio_fake_worker, tts_studio_protocol; "
                "assert importlib.util.find_spec('tts_studio') is None"
            ),
        ],
        cwd=tmp_path,
        check=True,
        env=isolated_environment,
    )

    server_environment = isolated_environment.copy()
    shadow_bin = tmp_path / "shadow-bin"
    shadow_bin.mkdir()
    shadow_worker = shadow_bin / "tts-studio-fake-worker"
    shadow_worker.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    shadow_worker.chmod(0o700)
    worker_bin = str(_environment_executable(worker_environment, "python").parent)
    server_environment["PATH"] = os.pathsep.join(
        [str(shadow_bin), worker_bin, server_environment.get("PATH", "")]
    )
    port = _available_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            str(_environment_executable(core_environment, "tts")),
            "serve",
            "--include-test-adapters",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=tmp_path,
        env=server_environment,
    )
    try:
        system = _wait_for_ready_core(base_url, server)
        assert system["data_dir"] == str(data_dir)

        status, compatible = _json_request(
            f"{base_url}/api/v1/models/validate",
            method="POST",
            payload={"repository_id": "fixtures/compatible", "requested_revision": "main"},
        )
        assert status == 200
        assert compatible["compatible"] is True
        assert compatible["selected_engine_id"] == "fake"
        assert compatible["results"][0]["resolved_commit"] == (
            "1c6d281855eeb808859fc335a5ef01f66e82f4a3"
        )

        status, incompatible = _json_request(
            f"{base_url}/api/v1/models/validate",
            method="POST",
            payload={"repository_id": "fixtures/incompatible"},
        )
        assert status == 200
        assert incompatible["compatible"] is False
        assert incompatible["selected_engine_id"] is None
        assert incompatible["results"][0]["error_code"] == "model_incompatible"

        status, queued = _json_request(
            f"{base_url}/api/v1/downloads",
            method="POST",
            payload={"repository_id": "fixtures/compatible", "variant": "int8"},
        )
        assert status == 202
        completed = _wait_for_download(base_url, queued["id"])
        assert completed["state"] == "completed"
        assert completed["target_model_id"] is not None

        events_url = f"{base_url}/api/v1/events?download_id={queued['id']}"
        with urlopen(Request(events_url, headers={"Last-Event-ID": "0"}), timeout=5) as stream:
            first_event = _read_sse_event(stream)
        resumed_events: list[dict[str, Any]] = []
        with urlopen(
            Request(events_url, headers={"Last-Event-ID": str(first_event["id"])}),
            timeout=5,
        ) as stream:
            while True:
                event = _read_sse_event(stream)
                resumed_events.append(event)
                if event["event"] == "model.activated":
                    break
        assert first_event["event"] == "download.queued"
        assert all(event["id"] > first_event["id"] for event in resumed_events)
        assert [event["id"] for event in resumed_events] == sorted(
            event["id"] for event in resumed_events
        )
        assert resumed_events[-1]["data"]["model_id"] == completed["target_model_id"]

        status, models = _json_request(f"{base_url}/api/v1/models")
        assert status == 200
        assert len(models) == 1
        model = models[0]
        assert model["id"] == completed["target_model_id"]
        assert model["repository_id"] == "fixtures/compatible"
        assert model["runtime_variant"] == "int8"
        assert not Path(model["cache_path"]).is_absolute()
        managed_model_directory = _safe_managed_model_path(data_dir, model["cache_path"])
        _assert_managed_model_files(managed_model_directory)

        status, body = _json_request(
            f"{base_url}/api/v1/models/{model['id']}/remove",
            method="POST",
        )
        assert status == 204
        assert body is None
        assert not managed_model_directory.is_symlink()
        assert not managed_model_directory.exists()
        assert _json_request(f"{base_url}/api/v1/models") == (200, [])
    finally:
        _stop_server(server)
