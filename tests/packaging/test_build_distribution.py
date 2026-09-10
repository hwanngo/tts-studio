from __future__ import annotations

import os
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest


def _load_build_script() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "build_distribution.py"
    spec = spec_from_file_location("build_distribution", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load build script: {script}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_uses_the_pnpm_command_shim_but_not_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    distribution_builder = _load_build_script()
    monkeypatch.setattr(os, "name", "nt")

    assert distribution_builder._build_tool("pnpm") == "pnpm.cmd"
    assert distribution_builder._build_tool("uv") == "uv"


def test_build_aborts_if_static_destination_is_redirected_after_frontend_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution_builder = _load_build_script()
    repository = tmp_path / "repository"
    package_parent = repository / "src" / "tts_studio"
    web_dist = repository / "web" / "dist"
    outside_package = tmp_path / "outside-package"
    outside_static = outside_package / "static"
    sentinel = outside_static / "keep.txt"

    package_parent.mkdir(parents=True)
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text("new build", encoding="utf-8")
    outside_static.mkdir(parents=True)
    sentinel.write_text("do not remove", encoding="utf-8")

    def redirect_during_frontend_build(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "pnpm":
            package_parent.rmdir()
            package_parent.symlink_to(outside_package, target_is_directory=True)
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr(distribution_builder.subprocess, "run", redirect_during_frontend_build)

    with pytest.raises(RuntimeError, match="refusing to access path outside repository"):
        try:
            distribution_builder.build_distribution(repository)
        finally:
            assert sentinel.is_file()
            assert sentinel.read_text(encoding="utf-8") == "do not remove"
            assert not (outside_static / "index.html").exists()


def test_build_emits_the_protocol_wheel_with_the_root_release_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution_builder = _load_build_script()
    repository = tmp_path / "repository"
    output_dir = tmp_path / "artifacts"
    (repository / "src" / "tts_studio").mkdir(parents=True)
    fake_worker = repository / "workers" / "fake"
    fake_worker.mkdir(parents=True)
    vieneu_worker = repository / "workers" / "vieneu"
    vieneu_worker.mkdir(parents=True)
    openai_worker = repository / "workers" / "openai_compatible"
    openai_worker.mkdir(parents=True)
    web_dist = repository / "web" / "dist"
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text("web build", encoding="utf-8")
    commands: list[list[str]] = []

    def record_command(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == repository.resolve()
        assert check is True
        commands.append(command)
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr(distribution_builder.subprocess, "run", record_command)

    distribution_builder.build_distribution(
        repository, output_dir, include_test_adapters=True
    )

    assert commands == [
        ["pnpm", "--dir", "web", "build"],
        [
            "uv",
            "build",
            "--package",
            "tts-studio-protocol",
            "--wheel",
            "--out-dir",
            str(output_dir.resolve()),
        ],
        [
            "uv",
            "build",
            "--package",
            "tts-studio-worker-sdk",
            "--wheel",
            "--out-dir",
            str(output_dir.resolve()),
        ],
        [
            "uv",
            "build",
            "--wheel",
            str(fake_worker.resolve()),
            "--out-dir",
            str(output_dir.resolve()),
        ],
        [
            "uv",
            "build",
            "--wheel",
            str(vieneu_worker.resolve()),
            "--out-dir",
            str(output_dir.resolve()),
        ],
        [
            "uv",
            "build",
            "--wheel",
            str(openai_worker.resolve()),
            "--out-dir",
            str(output_dir.resolve()),
        ],
        [
            "uv",
            "build",
            "--package",
            "tts-studio",
            "--out-dir",
            str(output_dir.resolve()),
        ],
    ]
    assert (repository / "src" / "tts_studio" / "static" / "index.html").read_text(
        encoding="utf-8"
    ) == "web build"

    commands.clear()
    distribution_builder.build_distribution(repository, output_dir)

    assert all(str(fake_worker.resolve()) not in command for command in commands)
