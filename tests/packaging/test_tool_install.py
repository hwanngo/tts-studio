from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_distribution.py"


def _offline_dependency_is_unavailable(result: subprocess.CompletedProcess[str]) -> bool:
    diagnostics = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode != 0 and (
        "not found in the cache" in diagnostics
        or (
            "needs to be downloaded from a registry" in diagnostics
            and "network was disabled" in diagnostics
        )
    )


def test_uv_tool_install_exposes_core_and_service_commands(tmp_path: Path) -> None:
    distribution_dir = tmp_path / "dist"
    bin_dir = tmp_path / "bin"
    tool_dir = tmp_path / "tools"
    environment = os.environ.copy()
    environment.pop("MYPYPATH", None)
    environment.pop("PYTHONPATH", None)

    subprocess.run(
        [
            sys.executable,
            str(_BUILD_SCRIPT),
            "--out-dir",
            str(distribution_dir),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        env=environment,
    )
    root_wheel = next(distribution_dir.glob("tts_studio-*.whl"))
    install = [
        "uv",
        "tool",
        "install",
        "--python",
        sys.executable,
        "--offline",
        "--find-links",
        str(distribution_dir),
        "--force",
        str(root_wheel),
    ]
    install_result = subprocess.run(
        install,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={
            **environment,
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        },
    )
    if install_result.returncode != 0:
        if _offline_dependency_is_unavailable(install_result):
            pytest.skip(
                "offline uv tool dependency closure is unavailable in the local cache: "
                + install_result.stderr.strip()
            )
        pytest.fail(
            "uv tool install failed for a reason other than a missing cached artifact:\n"
            + install_result.stderr
        )
    installed_tts = bin_dir / ("tts.exe" if os.name == "nt" else "tts")
    help_result = subprocess.run(
        [str(installed_tts), "service", "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert "install" in help_result.stdout
    assert "uninstall" in help_result.stdout
