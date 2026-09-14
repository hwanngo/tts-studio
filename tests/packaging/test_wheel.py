from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPOSITORY_ROOT / "scripts" / "build_distribution.py"
_CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s)]+))\s*\)", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\s+(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_HTML_URL_ATTRIBUTE = re.compile(
    r"\b(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE
)


def _remote_urls(document: str) -> tuple[str, ...]:
    matches = (
        *_CSS_URL.findall(document),
        *_CSS_IMPORT.findall(document),
        *_HTML_URL_ATTRIBUTE.findall(document),
    )
    urls = tuple(dict.fromkeys(next(value for value in match if value) for match in matches))
    return tuple(url for url in urls if url.casefold().startswith(("http:", "https:", "//")))


def _environment_executable(environment: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe" if name in {"python", "tts"} else ""
        return environment / "Scripts" / f"{name}{suffix}"
    return environment / "bin" / name


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _offline_dependency_is_unavailable(result: subprocess.CompletedProcess[str]) -> bool:
    diagnostics = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode != 0 and (
        "not found in the cache" in diagnostics
        or (
            "needs to be downloaded from a registry" in diagnostics
            and "network was disabled" in diagnostics
        )
    )


def _wait_for_json(url: str, server: subprocess.Popen[str]) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.poll() is not None:
            output = server.stdout.read() if server.stdout is not None else ""
            raise AssertionError(f"installed server exited before becoming ready:\n{output}")
        try:
            with urlopen(url, timeout=1) as response:
                return response.status, json.load(response)
        except URLError:
            time.sleep(0.05)
    raise AssertionError("installed server did not become ready")


def test_installed_wheel_serves_web_ui_and_api(tmp_path: Path) -> None:
    provided_dist = os.environ.get("TTS_STUDIO_TEST_DIST_DIR")
    distribution_dir = (
        Path(provided_dist).resolve(strict=True) if provided_dist else tmp_path / "dist"
    )
    environment = tmp_path / "installed"
    worker_environment = tmp_path / "worker-installed"
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
    fake_wheels = tuple(distribution_dir.glob("tts_studio_fake_worker-*.whl"))
    openai_wheels = tuple(distribution_dir.glob("tts_studio_openai_compatible_worker-*.whl"))
    vieneu_wheels = tuple(distribution_dir.glob("tts_studio_vieneu_worker-*.whl"))
    assert len(root_wheels) == 1
    assert len(protocol_wheels) == 1
    assert len(worker_sdk_wheels) == 1
    assert len(fake_wheels) == 1
    assert len(openai_wheels) == 1
    assert len(vieneu_wheels) == 1

    with zipfile.ZipFile(openai_wheels[0]) as openai_wheel:
        openai_names = set(openai_wheel.namelist())
    assert "tts_studio_openai_worker/service.py" in openai_names
    assert "tts_studio_openai_worker/http.py" in openai_names

    with zipfile.ZipFile(root_wheels[0]) as root_wheel:
        root_names = set(root_wheel.namelist())
        root_metadata_name = next(
            name for name in root_names if name.endswith(".dist-info/METADATA")
        )
        root_metadata = root_wheel.read(root_metadata_name).decode("utf-8")
        root_index_name = next(name for name in root_names if name.endswith("/static/index.html"))
        root_index = root_wheel.read(root_index_name).decode("utf-8")
        root_styles = "\n".join(
            root_wheel.read(name).decode("utf-8")
            for name in root_names
            if "/static/assets/" in name and name.endswith(".css")
        )
    assert not any("tts_studio_vieneu_worker" in name for name in root_names)
    assert not any("tts_studio_worker_sdk" in name for name in root_names)
    assert "Requires-Dist: tts-studio-worker-sdk" not in root_metadata
    assert "License-Expression: MIT" in root_metadata
    assert "License-File: LICENSE" in root_metadata
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in root_names)
    assert _remote_urls(root_index) == ()
    assert _remote_urls(root_styles) == ()
    with zipfile.ZipFile(vieneu_wheels[0]) as vieneu_wheel:
        vieneu_names = set(vieneu_wheel.namelist())
        vieneu_metadata = next(
            name for name in vieneu_names if name.endswith(".dist-info/METADATA")
        )
        vieneu_entry_points = next(
            name for name in vieneu_names if name.endswith(".dist-info/entry_points.txt")
        )
    assert "tts_studio_vieneu_worker/main.py" in vieneu_names
    assert "tts_studio_vieneu_worker/service.py" in vieneu_names
    assert "tts_studio_vieneu_worker/runtime.py" in vieneu_names
    with zipfile.ZipFile(vieneu_wheels[0]) as vieneu_wheel:
        metadata = vieneu_wheel.read(vieneu_metadata).decode("utf-8")
        entry_points = vieneu_wheel.read(vieneu_entry_points).decode("utf-8")
    assert "Requires-Dist: vieneu==3.6.3" in metadata
    assert "tts-studio-protocol" in metadata
    assert "tts-studio-worker-sdk" in metadata
    assert "tts-studio-vieneu-worker = tts_studio_vieneu_worker.main:main" in entry_points
    with zipfile.ZipFile(protocol_wheels[0]) as protocol_wheel:
        protocol_payload = protocol_wheel.read(
            "tts_studio_protocol/engine/v1/engine_pb2_grpc.py"
        ).decode("utf-8")
    assert "ValidateReference" in protocol_payload
    assert "Synthesize" in protocol_payload
    with zipfile.ZipFile(fake_wheels[0]) as fake_wheel:
        assert "tts_studio_fake_worker/service.py" in fake_wheel.namelist()

    isolated_environment = os.environ.copy()
    isolated_environment.pop("MYPYPATH", None)
    isolated_environment.pop("PYTHONPATH", None)

    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        cwd=tmp_path,
        check=True,
        env=isolated_environment,
    )
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(worker_environment)],
        cwd=tmp_path,
        check=True,
        env=isolated_environment,
    )
    installed_python = _environment_executable(environment, "python")
    worker_python = _environment_executable(worker_environment, "python")
    root_install = [
        "uv",
        "pip",
        "install",
        "--python",
        str(installed_python),
        "--offline",
        "--find-links",
        str(distribution_dir),
        str(root_wheels[0]),
        str(protocol_wheels[0]),
    ]
    root_dependency_probe = subprocess.run(
        [*root_install, "--dry-run"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=isolated_environment,
    )
    if root_dependency_probe.returncode != 0:
        if _offline_dependency_is_unavailable(root_dependency_probe):
            pytest.skip(
                "offline Core dependency closure is unavailable in the local uv cache: "
                + root_dependency_probe.stderr.strip()
            )
        pytest.fail(
            "offline Core dependency probe failed for a reason other than a missing cached "
            f"artifact (exit {root_dependency_probe.returncode}):\n"
            f"stdout:\n{root_dependency_probe.stdout}\nstderr:\n{root_dependency_probe.stderr}"
        )
    subprocess.run(root_install, cwd=tmp_path, check=True, env=isolated_environment)
    worker_install = [
        "uv",
        "pip",
        "install",
        "--python",
        str(worker_python),
        "--offline",
        "--find-links",
        str(distribution_dir),
        str(protocol_wheels[0]),
        str(vieneu_wheels[0]),
    ]
    dependency_probe = subprocess.run(
        [*worker_install, "--dry-run"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=isolated_environment,
    )
    worker_dependency_skip_reason: str | None = None
    if dependency_probe.returncode == 0:
        subprocess.run(
            worker_install,
            cwd=tmp_path,
            check=True,
            env=isolated_environment,
        )
        subprocess.run(
            ["uv", "pip", "check", "--python", str(worker_python)],
            cwd=tmp_path,
            check=True,
            env=isolated_environment,
        )
        subprocess.run(
            [str(worker_python), "-c", "import tts_studio_vieneu_worker"],
            cwd=tmp_path,
            check=True,
            env=isolated_environment,
        )
    elif _offline_dependency_is_unavailable(dependency_probe):
        worker_dependency_skip_reason = (
            "offline VieNeu dependency closure is unavailable in the local uv cache: "
            + dependency_probe.stderr.strip()
        )
    else:
        pytest.fail(
            "offline VieNeu dependency probe failed for a reason other than a missing cached "
            f"artifact (exit {dependency_probe.returncode}):\n"
            f"stdout:\n{dependency_probe.stdout}\nstderr:\n{dependency_probe.stderr}"
        )
    subprocess.run(
        ["uv", "pip", "check", "--python", str(installed_python)],
        cwd=tmp_path,
        check=True,
        env=isolated_environment,
    )

    installed_tts = _environment_executable(environment, "tts")
    help_result = subprocess.run(
        [str(installed_tts), "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=isolated_environment,
    )
    assert "Usage" in help_result.stdout
    assert "serve" in help_result.stdout

    import_result = subprocess.run(
        [
            str(installed_python),
            "-c",
            (
                "import tts_studio, tts_studio_protocol; "
                "from tts_studio.generated.api import ModelValidationResponse; "
                "from tts_studio.providers.service import ProviderService; "
                "from tts_studio.server.app import create_app; "
                "from tts_studio.server.routes.openai import SpeechRequest; "
                "print(tts_studio.__version__, ModelValidationResponse.__name__, "
                "ProviderService.__name__, create_app.__name__, SpeechRequest.__name__)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=isolated_environment,
    )
    assert import_result.stdout.strip() == (
        "0.1.0 ModelValidationResponse ProviderService create_app SpeechRequest"
    )
    for module in (
        "vieneu",
        "tts_studio_worker_sdk",
        "tts_studio_fake_worker",
        "tts_studio_vieneu_worker",
    ):
        core_worker_import = subprocess.run(
            [str(installed_python), "-c", f"import {module}"],
            cwd=tmp_path,
            env=isolated_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert core_worker_import.returncode != 0, module

    port = _available_loopback_port()
    server = subprocess.Popen(
        [
            str(installed_tts),
            "serve",
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
        env=isolated_environment,
    )
    try:
        system_status, system = _wait_for_json(f"http://127.0.0.1:{port}/api/v1/system", server)
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            page_status = response.status
            page = response.read().decode("utf-8")

        assert system_status == 200
        assert system["status"] == "healthy"
        assert page_status == 200
        assert "TTS Studio" in page
    finally:
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
    if worker_dependency_skip_reason is not None:
        pytest.skip(worker_dependency_skip_reason)
