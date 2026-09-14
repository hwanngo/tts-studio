"""Explicit discovery of installed Engine Adapter launch descriptors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from tts_studio.storage.layout import UnsafeStoragePathError
from tts_studio.workers.process import WorkerLaunchSpec

_REQUIRED_WORKER_DISTRIBUTIONS = (
    "tts-studio-protocol",
    "tts-studio-worker-sdk",
)
_SAFE_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True)
class _WorkerDefinition:
    engine_id: str
    priority: int
    project_directory: str
    executable: str
    distribution: str
    entry_point: str
    dependency_versions: tuple[tuple[str, str], ...] = ()


_TEST_WORKER_DEFINITIONS = (
    _WorkerDefinition(
        engine_id="fake",
        priority=1000,
        project_directory="fake",
        executable="tts-studio-fake-worker",
        distribution="tts-studio-fake-worker",
        entry_point="tts_studio_fake_worker.main:main",
    ),
)

_PRODUCTION_WORKER_DEFINITIONS = (
    _WorkerDefinition(
        engine_id="openai_compatible",
        priority=1000,
        project_directory="openai_compatible",
        executable="tts-studio-openai-compatible-worker",
        distribution="tts-studio-openai-compatible-worker",
        entry_point="tts_studio_openai_worker.main:main",
    ),
    _WorkerDefinition(
        engine_id="vieneu",
        priority=1100,
        project_directory="vieneu",
        executable="tts-studio-vieneu-worker",
        distribution="tts-studio-vieneu-worker",
        entry_point="tts_studio_vieneu_worker.main:main",
        dependency_versions=(("vieneu", "3.6.3"),),
    ),
)


@dataclass(frozen=True)
class AdapterDescriptor:
    """A trusted, non-secret process descriptor for one Engine Adapter."""

    engine_id: str
    priority: int
    launch: WorkerLaunchSpec

    def __post_init__(self) -> None:
        if not self.engine_id or self.engine_id.strip() != self.engine_id:
            raise ValueError("adapter engine ID must be a non-empty normalized value")
        if self.priority < 0:
            raise ValueError("adapter priority must not be negative")


def discover_adapters(
    repository_root: Path,
    *,
    include_test_adapters: bool = False,
    runtime_root: Path | None = None,
) -> tuple[AdapterDescriptor, ...]:
    """Return trusted production adapters, optionally including test adapters.

    Discovery is intentionally explicit. Repository IDs and other public input never
    participate in building a command line. Test adapters are opt-in so a normal Core
    process never presents deterministic fixtures as user-facing engines.
    """

    root = repository_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("adapter repository root must be a directory")

    descriptors: list[AdapterDescriptor] = []
    definitions = _PRODUCTION_WORKER_DEFINITIONS + (
        _TEST_WORKER_DEFINITIONS if include_test_adapters else ()
    )
    for definition in definitions:
        active_worker = (
            _discover_active_worker(runtime_root, definition) if runtime_root is not None else None
        )
        if active_worker is not None:
            descriptors.append(_installed_descriptor(definition, active_worker))
            continue
        project = root / "workers" / definition.project_directory
        if _verified_local_project(project, definition):
            descriptors.append(_source_descriptor(root, project, definition))
            continue
        installed_worker = _discover_verified_installed_worker(definition)
        if installed_worker is not None:
            descriptors.append(_installed_descriptor(definition, installed_worker))

    return tuple(sorted(descriptors, key=lambda item: (item.priority, item.engine_id)))


def _discover_active_worker(root: Path, definition: _WorkerDefinition) -> Path | None:
    """Resolve one activated generation without silently falling back on corruption."""
    resolved_root = root.expanduser().resolve()
    workers = resolved_root / "workers"
    if not workers.exists() and not workers.is_symlink():
        return None
    if not _unredirected_directory(workers):
        raise UnsafeStoragePathError("runtime workers directory is unsafe")
    pointer = workers / "current.json"
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if not _unredirected_regular_file(pointer):
        raise UnsafeStoragePathError("active runtime pointer must be a regular file")
    try:
        document = json.loads(pointer.read_text(encoding="utf-8"))
        generation = document["generation"]
        mapping = document["engines"]
    except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("active runtime pointer is unreadable") from error
    if not isinstance(mapping, dict):
        raise TypeError("active runtime pointer is unreadable")
    entry = mapping.get(definition.engine_id)
    if entry is None:
        return None
    if (
        not isinstance(generation, str)
        or _SAFE_RUNTIME_ID.fullmatch(generation) is None
        or not isinstance(mapping, dict)
        or not isinstance(entry, dict)
        or entry.get("generation") != generation
        or not isinstance(entry.get("manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", entry["manifest_sha256"]) is None
    ):
        raise UnsafeStoragePathError("active runtime generation is unsafe")
    engine_root = workers / definition.engine_id
    if not _unredirected_directory(engine_root):
        raise UnsafeStoragePathError("runtime engine directory is unsafe")
    generation_root = engine_root / "generations" / generation
    if not _unredirected_directory(generation_root):
        raise UnsafeStoragePathError("active runtime generation is unsafe")
    manifest = generation_root / "manifest.json"
    if not _unredirected_regular_file(manifest):
        raise UnsafeStoragePathError("active runtime manifest is unsafe")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != entry["manifest_sha256"]:
        raise ValueError("active runtime manifest hash mismatch")
    try:
        manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("active runtime manifest is unreadable") from error
    if not isinstance(manifest_document, dict) or (
        manifest_document.get("engine_id") != definition.engine_id
        or manifest_document.get("generation") != generation
    ):
        raise ValueError("active runtime manifest identity mismatch")
    environment = generation_root / "venv"
    if not _unredirected_directory(environment):
        raise UnsafeStoragePathError("active runtime environment is unsafe")
    executable = environment / ("Scripts" if os.name == "nt" else "bin") / definition.executable
    if os.name == "nt" and not executable.exists():
        executable = executable.with_suffix(".exe")
    if not _verified_installed_worker(executable, definition):
        raise ValueError("active runtime Worker provenance is invalid")
    return executable


def _source_descriptor(
    root: Path, project: Path, definition: _WorkerDefinition
) -> AdapterDescriptor:
    return AdapterDescriptor(
        engine_id=definition.engine_id,
        priority=definition.priority,
        launch=WorkerLaunchSpec(
            command=(
                "uv",
                "run",
                "--frozen",
                "--project",
                str(project.resolve(strict=True)),
                definition.executable,
            ),
            cwd=root,
        ),
    )


def _installed_descriptor(definition: _WorkerDefinition, executable: Path) -> AdapterDescriptor:
    return AdapterDescriptor(
        engine_id=definition.engine_id,
        priority=definition.priority,
        launch=WorkerLaunchSpec(command=(str(executable),), cwd=executable.parent.parent),
    )


def _verified_local_project(project: Path, definition: _WorkerDefinition) -> bool:
    if not _unredirected_directory(project):
        return False
    pyproject = project / "pyproject.toml"
    lockfile = project / "uv.lock"
    if not _unredirected_regular_file(pyproject) or not _unredirected_regular_file(lockfile):
        return False
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project_metadata = document["project"]
        scripts = project_metadata["scripts"]
    except KeyError, TypeError, UnicodeError, OSError, tomllib.TOMLDecodeError:
        return False
    return bool(
        project_metadata.get("name") == definition.distribution
        and scripts.get(definition.executable) == definition.entry_point
    )


def _discover_verified_installed_worker(definition: _WorkerDefinition) -> Path | None:
    """Return the first PATH worker with verified uv environment provenance.

    An executable name is not an installation descriptor. The candidate must be a
    direct child of an unredirected virtual environment and its environment must
    contain the uv-installed Worker distribution, exact console entry point, and
    the generated protocol and Worker SDK distributions. This keeps Core free of
    adapter imports while preventing an ambient same-named executable from being
    treated as an installed adapter.
    """

    seen: set[Path] = set()
    for directory_name in os.get_exec_path():
        directory = Path(directory_name or os.curdir).absolute()
        for candidate in _path_candidates(directory, definition.executable):
            if candidate in seen:
                continue
            seen.add(candidate)
            if _verified_installed_worker(candidate, definition):
                return candidate
    return None


def _path_candidates(directory: Path, executable_name: str) -> tuple[Path, ...]:
    names = [executable_name]
    if os.name == "nt":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        names.extend(
            f"{executable_name}{extension.lower()}" for extension in extensions if extension
        )
    return tuple(directory / name for name in dict.fromkeys(names))


def _verified_installed_worker(candidate: Path, definition: _WorkerDefinition) -> bool:
    if not _unredirected_regular_file(candidate) or not os.access(candidate, os.X_OK):
        return False
    if candidate.parent.name not in {"bin", "Scripts"}:
        return False

    environment_root = candidate.parent.parent
    if not _unredirected_directory(environment_root):
        return False
    if not _unredirected_regular_file(environment_root / "pyvenv.cfg"):
        return False
    site_packages = _site_packages(environment_root)
    if not site_packages:
        return False

    distributions = _distributions(site_packages)
    worker_distribution = distributions.get(_normalized_distribution_name(definition.distribution))
    if worker_distribution is None or not _is_uv_artifact(worker_distribution):
        return False
    if not any(
        entry_point.group == "console_scripts"
        and entry_point.name == definition.executable
        and entry_point.value == definition.entry_point
        for entry_point in worker_distribution.entry_points
    ):
        return False
    if not all(
        (dependency := distributions.get(_normalized_distribution_name(name))) is not None
        and _is_uv_artifact(dependency)
        for name in _REQUIRED_WORKER_DISTRIBUTIONS
    ):
        return False
    for dependency_name, expected_version in definition.dependency_versions:
        dependency = distributions.get(_normalized_distribution_name(dependency_name))
        if (
            dependency is None
            or dependency.version != expected_version
            or not _is_uv_artifact(dependency)
        ):
            return False
    return _matches_environment_interpreter(candidate, environment_root)


def _site_packages(environment_root: Path) -> tuple[Path, ...]:
    candidates = [environment_root / "Lib" / "site-packages"]
    lib_directory = environment_root / "lib"
    if lib_directory.is_dir() and not lib_directory.is_symlink():
        candidates.extend(lib_directory.glob("python*/site-packages"))
    return tuple(candidate for candidate in candidates if _unredirected_directory(candidate))


def _distributions(site_packages: tuple[Path, ...]) -> dict[str, metadata.Distribution]:
    return {
        normalized: distribution
        for distribution in metadata.distributions(path=[str(path) for path in site_packages])
        if (normalized := _normalized_distribution_name(distribution.metadata.get("Name", "")))
    }


def _is_uv_artifact(distribution: metadata.Distribution) -> bool:
    installer = distribution.read_text("INSTALLER")
    if installer is None or installer.strip() != "uv":
        return False
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is None:
        return False
    try:
        document: Any = json.loads(direct_url)
    except json.JSONDecodeError, TypeError:
        return False
    return (
        isinstance(document, dict)
        and isinstance(document.get("url"), str)
        and isinstance(document.get("archive_info"), dict)
    )


def _matches_environment_interpreter(candidate: Path, environment_root: Path) -> bool:
    if os.name == "nt":
        return True
    try:
        expected = environment_root / "bin" / "python"
        expected_resolved = expected.resolve(strict=True)
        lines = candidate.read_text(encoding="utf-8").splitlines()
        if (
            len(lines) >= 2
            and lines[0] == "#!/bin/sh"
            and lines[1].startswith("'''exec' ")
            and (f"'{expected}'" in lines[1] or f"'{expected_resolved}'" in lines[1])
        ):
            return True
        if lines[0].startswith("#!"):
            interpreter = Path(lines[0].removeprefix("#!").strip().split(maxsplit=1)[0])
            return interpreter.resolve(strict=True) == expected_resolved
        return False
    except OSError, IndexError, UnicodeError, ValueError:
        return False


def _unredirected_regular_file(path: Path) -> bool:
    try:
        metadata_result = path.lstat()
        return (
            not path.is_symlink()
            and stat.S_ISREG(metadata_result.st_mode)
            and path.resolve(strict=True) == path
        )
    except OSError:
        return False


def _unredirected_directory(path: Path) -> bool:
    try:
        metadata_result = path.lstat()
        return (
            not path.is_symlink()
            and stat.S_ISDIR(metadata_result.st_mode)
            and path.resolve(strict=True) == path
        )
    except OSError:
        return False


def _normalized_distribution_name(name: str) -> str:
    return "-".join(part for part in name.casefold().replace("_", "-").split("-") if part)
