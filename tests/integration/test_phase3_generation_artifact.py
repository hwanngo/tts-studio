from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import wave
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPOSITORY_ROOT / "scripts" / "build_distribution.py"


def _identity_bound_unlink_available() -> bool:
    try:
        _ = ctypes.CDLL(None).funlinkat
    except AttributeError, OSError:
        return False
    return True


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
    try:
        with urlopen(request, timeout=5) as response:
            if response.status == 204:
                return response.status, None
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


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


def _wait_for_terminal_generation(base_url: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, job = _json_request(f"{base_url}/api/v1/generations/{job_id}")
        assert status == 200
        if job["state"] in {"completed", "cancelled", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("installed Core generation did not reach a terminal state")


def _wait_for_generation_state(base_url: str, job_id: str, states: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, job = _json_request(f"{base_url}/api/v1/generations/{job_id}")
        assert status == 200
        if job["state"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"generation did not reach one of {sorted(states)}")


def _embedded_studio_assets(base_url: str) -> tuple[str, str]:
    with urlopen(f"{base_url}/", timeout=5) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"
        index = response.read().decode("utf-8")
    scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', index)
    assert scripts
    script_url = f"{base_url}{scripts[0]}"
    with urlopen(script_url, timeout=5) as response:
        assert response.status == 200
        bundle = response.read().decode("utf-8")
    return index, bundle


def _artifact_paths(data_dir: Path) -> tuple[Path, ...]:
    audio = data_dir / "audio"
    return tuple(sorted(path for path in audio.iterdir() if not path.name.startswith(".")))


def _assert_no_generation_partials(data_dir: Path) -> None:
    assert not tuple((data_dir / "staging").glob(".generation-*"))
    assert not tuple((data_dir / "audio").glob(".generation-*"))


def _start_core(
    core_python: Path,
    worker_environment: Path,
    data_dir: Path,
) -> tuple[subprocess.Popen[str], str]:
    port = _available_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    server_environment = os.environ.copy()
    server_environment.pop("MYPYPATH", None)
    server_environment.pop("PYTHONPATH", None)
    shadow_bin = data_dir.parent / f"shadow-bin-{port}"
    shadow_bin.mkdir()
    shadow_worker = shadow_bin / "tts-studio-fake-worker"
    shadow_worker.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    shadow_worker.chmod(0o700)
    worker_bin = str(_environment_executable(worker_environment, "python").parent)
    server_environment["PATH"] = os.pathsep.join(
        [str(shadow_bin), worker_bin, server_environment.get("PATH", "")]
    )
    server = subprocess.Popen(
        [
            str(_environment_executable(core_python.parent.parent, "tts")),
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
        cwd=data_dir.parent,
        env=server_environment,
        start_new_session=os.name != "nt",
    )
    return server, base_url


def _stop_server(server: subprocess.Popen[str]) -> None:
    if server.poll() is None:
        try:
            server.terminate()
        except ProcessLookupError:
            pass
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_process_tree(server)
        server.wait(timeout=5)


def _kill_process_tree(server: subprocess.Popen[str]) -> None:
    if server.poll() is not None:
        return
    if os.name != "nt":
        os.killpg(os.getpgid(server.pid), signal.SIGKILL)
    else:
        server.kill()


def _safe_model_directory(data_dir: Path, cache_path: str) -> Path:
    relative = PurePosixPath(cache_path)
    assert len(relative.parts) == 2
    assert relative.parts[0] == "models"
    assert "\\" not in cache_path
    model_directory = data_dir / relative.parts[0] / relative.parts[1]
    metadata = model_directory.lstat()
    assert not model_directory.is_symlink()
    assert stat.S_ISDIR(metadata.st_mode)
    return model_directory


def test_built_artifacts_complete_phase3_generation_workflow(tmp_path: Path) -> None:
    provided_dist = os.environ.get("TTS_STUDIO_TEST_DIST_DIR")
    distribution_dir = (
        Path(provided_dist).resolve(strict=True) if provided_dist else tmp_path / "dist"
    )
    core_environment = tmp_path / "core-environment"
    worker_environment = tmp_path / "worker-environment"
    data_dir = tmp_path / "data"

    if not provided_dist:
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
                "from tts_studio.generated.api import GenerationJobResponse; "
                "assert importlib.util.find_spec('tts_studio_fake_worker') is None; "
                "assert importlib.util.find_spec('tts_studio_worker_sdk') is None; "
                "print(GenerationJobResponse.__name__)"
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env=isolated_environment,
    )
    assert core_import.stdout.strip() == "GenerationJobResponse"
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

    server, base_url = _start_core(core_python, worker_environment, data_dir)
    try:
        system = _wait_for_ready_core(base_url, server)
        assert system["data_dir"] == str(data_dir)
        index, bundle = _embedded_studio_assets(base_url)
        assert "TTS Studio" in index
        assert "Create speech" in bundle
        assert "/api/v1/generations" in bundle
        assert "/api/v1/history" in bundle

        status, validation = _json_request(
            f"{base_url}/api/v1/models/validate",
            method="POST",
            payload={"repository_id": "fixtures/compatible", "requested_revision": "main"},
        )
        assert status == 200
        assert validation["compatible"] is True

        status, download = _json_request(
            f"{base_url}/api/v1/downloads",
            method="POST",
            payload={"repository_id": "fixtures/compatible", "variant": "fp32"},
        )
        assert status == 202
        download_deadline = time.monotonic() + 15
        while time.monotonic() < download_deadline:
            status, download = _json_request(f"{base_url}/api/v1/downloads/{download['id']}")
            assert status == 200
            if download["state"] in {"completed", "cancelled", "failed"}:
                break
            time.sleep(0.05)
        assert download["state"] == "completed"
        model_id = download["target_model_id"]
        assert isinstance(model_id, str)
        model_status, models = _json_request(f"{base_url}/api/v1/models")
        assert model_status == 200
        model = next(item for item in models if item["id"] == model_id)
        model_directory = _safe_model_directory(data_dir, model["cache_path"])
        assert {path.name for path in model_directory.iterdir()} == {"config.json", "model.bin"}

        status, voices = _json_request(f"{base_url}/api/v1/voices?model_id={model_id}")
        assert status == 200
        assert [voice["id"] for voice in voices] == ["fake-neutral"]
        cli_voices = subprocess.run(
            [
                str(_environment_executable(core_environment, "tts")),
                "voices",
                "list",
                "--model",
                model_id,
                "--url",
                base_url,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            env=isolated_environment,
        )
        assert "fake-neutral" in cli_voices.stdout

        status, queued = _json_request(
            f"{base_url}/api/v1/generations",
            method="POST",
            payload={
                "model_id": model_id,
                "voice_id": "fake-neutral",
                "text": "A retained artifact crosses the installed Core seam.",
            },
        )
        assert status == 202
        completed = _wait_for_terminal_generation(base_url, queued["id"])
        assert completed["state"] == "completed"
        artifact_id = completed["artifact_id"]
        assert isinstance(artifact_id, str)
        history_status, history = _json_request(f"{base_url}/api/v1/history")
        assert history_status == 200
        artifact = next(item for item in history if item["id"] == artifact_id)

        with urlopen(f"{base_url}{completed['artifact_url']}", timeout=5) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "audio/wav"
            downloaded_wav = response.read()
        assert len(downloaded_wav) == artifact["byte_size"]
        assert hashlib.sha256(downloaded_wav).hexdigest() == artifact["sha256"]
        with wave.open(io.BytesIO(downloaded_wav), "rb") as wav:
            assert wav.getframerate() == 48_000
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getnframes() == artifact["frame_count"]
            assert len(wav.readframes(wav.getnframes())) == completed["bytes_written"]
        assert artifact["sample_rate"] == 48_000
        assert artifact["channel_count"] == 1

        database_path = data_dir / "database" / "tts-studio.sqlite3"
        with sqlite3.connect(database_path) as connection:
            stored_path = connection.execute(
                "SELECT path FROM audio_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()[0]
        assert stored_path == f"audio/{artifact_id}.wav"
        assert (data_dir / stored_path).is_file()
        assert _artifact_paths(data_dir) == (data_dir / stored_path,)
        _assert_no_generation_partials(data_dir)

        cli_output = tmp_path / "cli-output.wav"
        cli_speak = subprocess.run(
            [
                str(_environment_executable(core_environment, "tts")),
                "speak",
                "Installed CLI generation.",
                "--model",
                model_id,
                "--voice",
                "fake-neutral",
                "--output",
                str(cli_output),
                "--url",
                base_url,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            env=isolated_environment,
        )
        assert f"Wrote {cli_output}." in cli_speak.stdout
        with wave.open(str(cli_output), "rb") as wav:
            assert wav.getframerate() == 48_000
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2

        retained_audio_path = data_dir / stored_path
        delete_status, delete_body = _json_request(
            f"{base_url}/api/v1/history/{artifact_id}", method="DELETE"
        )
        history_status, history = _json_request(f"{base_url}/api/v1/history")
        assert history_status == 200
        if _identity_bound_unlink_available():
            assert delete_status == 204
            assert delete_body is None
            assert not retained_audio_path.exists()
            assert artifact_id not in {item["id"] for item in history}
        else:
            assert delete_status == 503
            assert delete_body["error"]["code"] == "artifact_delete_failed"
            assert retained_audio_path.exists()
            assert artifact_id in {item["id"] for item in history}

        audio_before_nonretained = _artifact_paths(data_dir)
        status, nonretained_queued = _json_request(
            f"{base_url}/api/v1/generations",
            method="POST",
            payload={
                "model_id": model_id,
                "voice_id": "fake-neutral",
                "text": "This output is intentionally not retained.",
                "retain_artifact": False,
            },
        )
        assert status == 202
        nonretained = _wait_for_terminal_generation(base_url, nonretained_queued["id"])
        assert nonretained["state"] == "completed"
        assert nonretained["artifact_id"] is None
        assert nonretained["text"] == ""
        with sqlite3.connect(database_path) as connection:
            assert (
                connection.execute(
                    "SELECT text FROM generation_jobs WHERE id = ?", (nonretained["id"],)
                ).fetchone()[0]
                == ""
            )
        history_status, history = _json_request(f"{base_url}/api/v1/history")
        assert history_status == 200
        assert all(item["job_id"] != nonretained["id"] for item in history)
        assert _artifact_paths(data_dir) == audio_before_nonretained
        _assert_no_generation_partials(data_dir)

        status, active_queued = _json_request(
            f"{base_url}/api/v1/generations",
            method="POST",
            payload={
                "model_id": model_id,
                "voice_id": "fake-neutral",
                "text": "cancel me " * 1000,
            },
        )
        assert status == 202
        _wait_for_generation_state(
            base_url, active_queued["id"], {"queued", "loading", "generating"}
        )
        remove_status, remove_body = _json_request(
            f"{base_url}/api/v1/models/{model_id}/remove", method="POST"
        )
        assert remove_status == 409
        assert remove_body["error"]["code"] == "model_in_use"
        cancel_status, _ = _json_request(
            f"{base_url}/api/v1/generations/{active_queued['id']}/cancel", method="POST"
        )
        assert cancel_status == 200
        cancelled = _wait_for_terminal_generation(base_url, active_queued["id"])
        assert cancelled["state"] == "cancelled"
        assert cancelled["artifact_id"] is None
        _assert_no_generation_partials(data_dir)

        status, recovery_queued = _json_request(
            f"{base_url}/api/v1/generations",
            method="POST",
            payload={
                "model_id": model_id,
                "voice_id": "fake-neutral",
                "text": "recover me " * 900,
            },
        )
        assert status == 202
        _wait_for_generation_state(base_url, recovery_queued["id"], {"generating", "finalizing"})
        assert tuple((data_dir / "staging").glob(".generation-*.pcm"))
        _kill_process_tree(server)
        server.wait(timeout=5)

        server, base_url = _start_core(core_python, worker_environment, data_dir)
        _wait_for_ready_core(base_url, server)
        recovery_status, recovered = _json_request(
            f"{base_url}/api/v1/generations/{recovery_queued['id']}"
        )
        assert recovery_status == 200
        assert recovered["state"] == "failed"
        assert recovered["error"]["code"] == "recovery_required"
        _assert_no_generation_partials(data_dir)

        retry_url = f"{base_url}/api/v1/generations/{recovered['id']}/retry"
        retry_status, retry = _json_request(retry_url, method="POST")
        assert retry_status == 202
        assert retry["id"] != recovered["id"]
        retried = _wait_for_terminal_generation(base_url, retry["id"])
        assert retried["state"] == "completed"
        assert _json_request(retry_url, method="POST")[1]["id"] == retry["id"]
        with sqlite3.connect(database_path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM generation_jobs WHERE retry_of = ?", (recovered["id"],)
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM audio_artifacts WHERE job_id IN (?, ?)",
                    (recovered["id"], retry["id"]),
                ).fetchone()[0]
                == 1
            )
    finally:
        _stop_server(server)
