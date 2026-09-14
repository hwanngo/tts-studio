"""Release compatibility is enforced by wheel metadata, outside the workspace."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from email.parser import Parser
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = {
    "tts-studio": ".",
    "tts-studio-protocol": "packages/protocol",
    "tts-studio-worker-sdk": "packages/worker-sdk",
    "tts-studio-fake-worker": "workers/fake",
    "tts-studio-openai-compatible-worker": "workers/openai_compatible",
    "tts-studio-vieneu-worker": "workers/vieneu",
}
EDGES = [
    (name, dependency)
    for name in PROJECTS
    for dependency in ("tts-studio-protocol", "tts-studio-worker-sdk")
    if name != "tts-studio-protocol"
    and dependency != name
    and (name != "tts-studio" or dependency == "tts-studio-protocol")
]


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "MYPYPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)
    return subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Path]]:
    directory = tmp_path_factory.mktemp("release-set")
    artifacts = directory / "artifacts"
    built: dict[str, Path] = {}
    for name, relative in PROJECTS.items():
        result = run(
            ["uv", "build", "--wheel", str(ROOT / relative), "--out-dir", str(artifacts)],
            directory,
        )
        assert result.returncode == 0, result.stderr
        built[name] = next(artifacts.glob(f"{name.replace('-', '_')}-*.whl"))
    for name in ("tts-studio-protocol", "tts-studio-worker-sdk"):
        source = directory / name
        shutil.copytree(ROOT / PROJECTS[name] / "src", source / "src")
        metadata = (ROOT / PROJECTS[name] / "pyproject.toml").read_text()
        current = tomllib.loads(metadata)["project"]["version"]
        metadata = metadata.replace(f'version = "{current}"', 'version = "0.0.0"')
        metadata = metadata.replace(f"=={current}", "==0.0.0")
        (source / "pyproject.toml").write_text(metadata)
        result = run(
            ["uv", "build", "--wheel", str(source), "--out-dir", str(directory / "older")],
            directory,
        )
        assert result.returncode == 0, result.stderr
        built[f"older:{name}"] = next((directory / "older").glob(f"{name.replace('-', '_')}-*.whl"))
    return directory, built


@pytest.mark.parametrize(("consumer", "dependency"), EDGES)
def test_installer_rejects_mixed_internal_releases(
    wheels: tuple[Path, dict[str, Path]], tmp_path: Path, consumer: str, dependency: str
) -> None:
    directory, built = wheels
    environment = tmp_path / "installed"
    created = run(["uv", "venv", "--python", sys.executable, str(environment)], tmp_path)
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            "--find-links",
            str(directory / "artifacts"),
            str(built[consumer]),
            str(built[f"older:{dependency}"]),
        ],
        tmp_path,
    )
    assert result.returncode != 0, "installer accepted incompatible release artifacts"
    release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert f"{consumer}=={release} depends on {dependency}=={release}" in " ".join(
        result.stderr.split()
    ), result.stderr
    assert "unsatisfiable" in result.stderr, result.stderr


def test_every_wheel_constrains_internal_dependencies_to_its_release(
    wheels: tuple[Path, dict[str, Path]],
) -> None:
    _, built = wheels
    release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for name in PROJECTS:
        with zipfile.ZipFile(built[name]) as wheel:
            metadata_name = next(path for path in wheel.namelist() if path.endswith("/METADATA"))
            metadata = Parser().parsestr(wheel.read(metadata_name).decode())
        assert metadata["Version"] == release
        requirements = metadata.get_all("Requires-Dist", [])
        for consumer, dependency in EDGES:
            if consumer == name:
                assert f"{dependency}=={release}" in requirements


@pytest.mark.parametrize(
    "consumer", ["tts-studio", "tts-studio-fake-worker", "tts-studio-openai-compatible-worker"]
)
def test_matching_release_wheels_install_without_crossing_core_worker_boundary(
    wheels: tuple[Path, dict[str, Path]],
    tmp_path: Path,
    consumer: str,
) -> None:
    _, built = wheels
    environment = tmp_path / "installed"
    created = run(["uv", "venv", "--python", sys.executable, str(environment)], tmp_path)
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    names = [consumer, "tts-studio-protocol"]
    if consumer != "tts-studio":
        names.append("tts-studio-worker-sdk")
    installed = run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            *[str(built[name]) for name in names],
        ],
        tmp_path,
    )
    assert installed.returncode == 0, installed.stderr
    checked = run(["uv", "pip", "check", "--python", str(python)], tmp_path)
    assert checked.returncode == 0, checked.stderr
    inspected = run(
        [
            str(python),
            "-I",
            "-c",
            "import json, importlib.metadata as m; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))",
        ],
        tmp_path,
    )
    assert inspected.returncode == 0, inspected.stderr
    versions = json.loads(inspected.stdout)
    assert len({versions[name] for name in names}) == 1
    assert ("tts-studio" in versions) == (consumer == "tts-studio")
    assert ("tts-studio-worker-sdk" in versions) == (consumer != "tts-studio")
    modules = {
        "tts-studio": "tts_studio.server.app",
        "tts-studio-fake-worker": "tts_studio_fake_worker.service",
        "tts-studio-openai-compatible-worker": "tts_studio_openai_worker.service",
    }
    imported = run([str(python), "-I", "-c", f"import {modules[consumer]}"], tmp_path)
    assert imported.returncode == 0, imported.stderr


def test_strict_source_types_with_only_built_internal_dependencies(
    wheels: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    _, built = wheels
    environment = tmp_path / "typing"
    created = run(["uv", "venv", "--python", sys.executable, str(environment)], tmp_path)
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            f"mypy=={version('mypy')}",
            str(built["tts-studio"]),
            str(built["tts-studio-protocol"]),
            str(built["tts-studio-worker-sdk"]),
        ],
        tmp_path,
    )
    assert installed.returncode == 0, installed.stderr
    checked = run(
        [
            str(python),
            "-I",
            "-m",
            "mypy",
            "--platform",
            "linux",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
        ],
        ROOT,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_release_command_propagates_the_root_version_to_buildable_package_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    for relative in PROJECTS.values():
        destination = repository / relative
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative / "pyproject.toml", destination / "pyproject.toml")
    script = repository / "scripts" / "sync_release_versions.py"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts" / script.name, script)
    root_metadata = repository / "pyproject.toml"
    original = root_metadata.read_text()
    release = tomllib.loads(original)["project"]["version"]
    root_metadata.write_text(original.replace(f'version = "{release}"', 'version = "0.0.0"'))

    rejected = run([sys.executable, str(script), "--check"], tmp_path)
    assert rejected.returncode == 1
    assert "packages/protocol/pyproject.toml" in rejected.stdout
    synchronized = run([sys.executable, str(script)], tmp_path)
    assert synchronized.returncode == 0, synchronized.stderr
    checked = run([sys.executable, str(script), "--check"], tmp_path)
    assert checked.returncode == 0, checked.stdout + checked.stderr

    for relative in ("packages/protocol", "packages/worker-sdk"):
        shutil.copytree(ROOT / relative / "src", repository / relative / "src")
    built = run(
        [
            "uv",
            "build",
            "--wheel",
            str(repository / "packages/worker-sdk"),
            "--out-dir",
            str(tmp_path / "artifacts"),
        ],
        tmp_path,
    )
    assert built.returncode == 0, built.stderr
    artifact = tmp_path / "artifacts" / "tts_studio_worker_sdk-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(artifact) as wheel:
        metadata = wheel.read("tts_studio_worker_sdk-0.0.0.dist-info/METADATA").decode()
    assert "Requires-Dist: tts-studio-protocol==0.0.0" in metadata
