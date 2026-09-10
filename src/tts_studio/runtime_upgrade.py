"""Transactional installation and rollback of locked Worker environments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from uuid import uuid4

from tts_studio.distribution import validate_release_artifacts
from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor

CommandRunner = Callable[[tuple[str, ...], Path], None]
HealthCheck = Callable[[str, Path], bool | None]
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_PREFIXES = {
    "tts_studio-",
    "tts_studio_protocol-",
    "tts_studio_worker_sdk-",
    "tts_studio_vieneu_worker-",
    "tts_studio_openai_compatible_worker-",
    "tts_studio_fake_worker-",
}


@dataclass(frozen=True)
class _EngineSpec:
    distribution: str
    executable: str
    entry_point: str
    dependencies: tuple[tuple[str, str], ...]
    wheel_prefix: str


_ENGINE_SPECS = {
    "vieneu": _EngineSpec(
        "tts-studio-vieneu-worker",
        "tts-studio-vieneu-worker",
        "tts_studio_vieneu_worker.main:main",
        (("vieneu", "3.6.3"), ("soundfile", "0.14.0")),
        "tts_studio_vieneu_worker-",
    ),
    "openai_compatible": _EngineSpec(
        "tts-studio-openai-compatible-worker",
        "tts-studio-openai-compatible-worker",
        "tts_studio_openai_worker.main:main",
        (),
        "tts_studio_openai_compatible_worker-",
    ),
    "fake": _EngineSpec(
        "tts-studio-fake-worker",
        "tts-studio-fake-worker",
        "tts_studio_fake_worker.main:main",
        (),
        "tts_studio_fake_worker-",
    ),
}


class RuntimeUpgradeError(RuntimeError):
    """Raised when a Worker environment transaction cannot be completed safely."""


@dataclass(frozen=True)
class UpgradeResult:
    generation_id: str
    previous_generations: dict[str, str | None]


@dataclass(frozen=True)
class RuntimeStatus:
    active_generations: dict[str, str | None]
    previous_generations: dict[str, str | None]

    @property
    def state(self) -> str:
        active = tuple(self.active_generations.values())
        if not any(active):
            return "not_installed"
        if all(active) and len(set(active)) == 1:
            return "active"
        return "incomplete"


class RuntimeUpgradeManager:
    """Manage independently versioned, user-owned Worker environments."""

    def __init__(
        self,
        layout: StorageLayout,
        *,
        required_engines: Sequence[str],
        command_runner: CommandRunner | None = None,
        health_check: HealthCheck | None = None,
        python_executable: str = "3.14",
    ) -> None:
        engines = tuple(required_engines)
        if not engines:
            raise ValueError("at least one Worker engine is required")
        for engine in engines:
            _validate_id(engine, "engine")
            if engine not in _ENGINE_SPECS:
                raise ValueError(f"no release artifact mapping exists for engine {engine!r}")
        if not isinstance(python_executable, str) or not python_executable:
            raise ValueError("python executable must be non-empty")
        self._layout = layout
        self._engines = tuple(sorted(set(engines)))
        self._command_runner = command_runner or _run_command
        self._health_check = health_check or self._verify_candidate
        self._python_executable = python_executable

    def _verify_candidate(self, engine: str, candidate: Path) -> None:
        _verify_candidate_structure(engine, candidate)
        try:
            asyncio.run(self._probe_candidate(engine, candidate))
        except RuntimeUpgradeError:
            raise
        except Exception as error:
            raise RuntimeUpgradeError(
                f"candidate Worker gRPC verification failed for {engine}"
            ) from error

    async def _probe_candidate(self, engine: str, candidate: Path) -> None:
        spec = _ENGINE_SPECS[engine]
        environment = _checked_directory(candidate / "venv", "candidate virtual environment")
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        executable = scripts / spec.executable
        if os.name == "nt" and not executable.exists():
            executable = executable.with_suffix(".exe")
        launch = WorkerLaunchSpec(command=(str(executable),), cwd=environment)
        supervisor = WorkerSupervisor(self._layout, startup_timeout=10.0)
        try:
            await supervisor.start(engine, launch)
            status = await supervisor.health(engine)
            if not status.ready:
                raise RuntimeUpgradeError(
                    f"candidate Worker health check failed for {engine}: {status.message}"
                )
        finally:
            await supervisor.stop_all()

    def upgrade(
        self,
        release_directory: Path,
        generation_id: str,
        *,
        expected_sha256: Mapping[str, str] | None = None,
        include_test_adapters: bool = False,
    ) -> UpgradeResult:
        """Stage, verify, and atomically activate one complete release."""
        _validate_id(generation_id, "generation")
        _checked_artifact_root(release_directory)
        artifacts = validate_release_artifacts(
            release_directory, include_test_adapters=include_test_adapters
        )
        digests = _hash_artifacts(artifacts, expected_sha256)
        self._layout.ensure()
        current = _read_pointer(self._layout, self._engines)
        previous = {engine: current.generation if current else None for engine in self._engines}
        staged: list[Path] = []
        try:
            for engine in self._engines:
                staged.append(
                    self._stage_engine(engine, generation_id, release_directory, artifacts, digests)
                )
            for engine, candidate in zip(self._engines, staged, strict=True):
                _check_health(self._health_check, engine, candidate)
            entries = {
                engine: {
                    "generation": generation_id,
                    "manifest_sha256": _manifest_sha256(candidate),
                }
                for engine, candidate in zip(self._engines, staged, strict=True)
            }
            _activate_pointer(
                self._layout, generation_id, entries, current.generation if current else None
            )
        except Exception as error:
            for candidate in staged:
                _remove_owned_generation(candidate)
            if isinstance(error, (RuntimeUpgradeError, UnsafeStoragePathError, ValueError)):
                raise
            raise RuntimeUpgradeError("Worker environment upgrade failed") from error
        return UpgradeResult(generation_id=generation_id, previous_generations=previous)

    def rollback(self) -> UpgradeResult:
        """Verify every previous generation, then atomically make it active."""
        self._layout.ensure()
        current = _read_pointer(self._layout, self._engines)
        if current is None or current.previous_generation is None:
            raise RuntimeUpgradeError("no verified previous Worker generation is available")
        target = current.previous_generation
        entries: dict[str, dict[str, str]] = {}
        candidates: list[Path] = []
        for engine in self._engines:
            candidate = _generation_path(self._layout, engine, target)
            manifest = _read_manifest(candidate, engine, target)
            candidates.append(candidate)
            entries[engine] = {
                "generation": target,
                "manifest_sha256": _manifest_sha256(candidate),
            }
            if manifest["generation"] != target:
                raise RuntimeUpgradeError("previous generation manifest is inconsistent")
        for engine, candidate in zip(self._engines, candidates, strict=True):
            _check_health(self._health_check, engine, candidate)
        assert current.generation is not None
        _activate_pointer(self._layout, target, entries, current.generation)
        return UpgradeResult(
            generation_id=target,
            previous_generations={engine: current.generation for engine in self._engines},
        )

    def status(self) -> RuntimeStatus:
        """Return the validated shared pointer as the legacy per-engine view."""
        self._layout.ensure()
        pointer = _read_pointer(self._layout, self._engines)
        if pointer is None:
            empty: dict[str, str | None] = {engine: None for engine in self._engines}
            return RuntimeStatus(active_generations=empty, previous_generations=empty.copy())
        active: dict[str, str | None] = {engine: pointer.generation for engine in self._engines}
        previous: dict[str, str | None] = {
            engine: pointer.previous_generation for engine in self._engines
        }
        return RuntimeStatus(active_generations=active, previous_generations=previous)

    def _stage_engine(
        self,
        engine: str,
        generation: str,
        release_directory: Path,
        artifacts: tuple[Path, ...],
        digests: dict[str, str],
    ) -> Path:
        engine_root = _engine_root(self._layout, engine, create=True)
        generations = _ensure_directory(engine_root / "generations", "generation directory")
        final = generations / generation
        if final.exists() or final.is_symlink():
            raise RuntimeUpgradeError(f"Worker generation already exists: {engine}/{generation}")
        final.mkdir()
        try:
            artifact_root = _ensure_directory(final / "artifacts", "candidate artifact directory")
            for artifact in artifacts:
                _checked_artifact_file(artifact)
                shutil.copyfile(artifact, artifact_root / artifact.name)
            _write_json(
                final / "manifest.json",
                {
                    "engine_id": engine,
                    "generation": generation,
                    "artifacts": [
                        {"name": artifact.name, "sha256": digests[artifact.name]}
                        for artifact in artifacts
                    ],
                },
            )
            venv = final / "venv"
            self._command_runner(
                ("uv", "venv", "--python", self._python_executable, "venv"), final
            )
            selected = tuple(
                artifact
                for artifact in artifacts
                if artifact.suffix == ".whl"
                and (
                    artifact.name.startswith("tts_studio_protocol-")
                    or artifact.name.startswith("tts_studio_worker_sdk-")
                    or artifact.name.startswith(_ENGINE_SPECS[engine].wheel_prefix)
                )
            )
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            self._command_runner(
                (
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--offline",
                    "--find-links",
                    str(release_directory.expanduser().resolve()),
                    *(str(artifact) for artifact in selected),
                ),
                final,
            )
            return final
        except Exception:
            _remove_owned_generation(final)
            raise


@dataclass(frozen=True)
class _Pointer:
    generation: str
    previous_generation: str | None


def _read_pointer(layout: StorageLayout, engines: tuple[str, ...]) -> _Pointer | None:
    path = layout.checked_directory("workers") / "current.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise UnsafeStoragePathError("shared runtime pointer must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeUpgradeError("shared runtime pointer is malformed") from error
    if not isinstance(value, dict):
        raise RuntimeUpgradeError("shared runtime pointer is malformed")
    generation = value.get("generation")
    previous = value.get("previous_generation")
    mapping = value.get("engines")
    if not isinstance(generation, str) or not isinstance(mapping, dict):
        raise RuntimeUpgradeError("shared runtime pointer is malformed")
    _validate_id(generation, "generation")
    if previous is not None:
        if not isinstance(previous, str):
            raise RuntimeUpgradeError("shared runtime pointer has an invalid previous generation")
        _validate_id(previous, "generation")
    if set(mapping) != set(engines):
        raise RuntimeUpgradeError("shared runtime pointer has divergent engines")
    for engine in engines:
        entry = mapping[engine]
        if (
            not isinstance(entry, dict)
            or entry.get("generation") != generation
            or not isinstance(entry.get("manifest_sha256"), str)
            or not _SHA256.fullmatch(entry["manifest_sha256"])
        ):
            raise RuntimeUpgradeError("shared runtime pointer has divergent engines")
        candidate = _generation_path(layout, engine, generation)
        _read_manifest(candidate, engine, generation, entry["manifest_sha256"])
        if previous is not None:
            _read_manifest(_generation_path(layout, engine, previous), engine, previous)
    return _Pointer(generation=generation, previous_generation=previous)


def _activate_pointer(
    layout: StorageLayout,
    generation: str,
    engines: dict[str, dict[str, str]],
    previous_generation: str | None,
) -> None:
    pointer = {
        "engines": engines,
        "generation": generation,
        "previous_generation": previous_generation,
    }
    _write_json(layout.checked_directory("workers") / "current.json", pointer)


def _generation_path(layout: StorageLayout, engine: str, generation: str) -> Path:
    _validate_id(generation, "generation")
    root = _engine_root(layout, engine, create=False)
    generations = _checked_directory(root / "generations", "generation directory")
    return _checked_directory(generations / generation, "Worker generation")


def _engine_root(layout: StorageLayout, engine: str, *, create: bool) -> Path:
    root = layout.managed_child("workers", engine)
    if root.is_symlink():
        raise UnsafeStoragePathError("Worker engine directory is redirected")
    if create and not root.exists():
        root.mkdir()
    return _checked_directory(root, "Worker engine directory")


def _validate_id(value: str, kind: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise UnsafeStoragePathError(f"{kind} identifier is unsafe")


def _ensure_directory(path: Path, description: str) -> Path:
    if path.is_symlink():
        raise UnsafeStoragePathError(f"{description} is redirected")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError as error:
        raise UnsafeStoragePathError(f"{description} is not a directory") from error
    return _checked_directory(path, description)


def _checked_directory(path: Path, description: str) -> Path:
    try:
        metadata_value = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise UnsafeStoragePathError(f"{description} is missing or unsafe") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata_value.st_mode) or resolved != path:
        raise UnsafeStoragePathError(f"{description} must be an unredirected directory")
    return path


def _checked_artifact_root(path: Path) -> Path:
    try:
        metadata_value = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeUpgradeError("artifact directory is missing or unsafe") from error
    candidate = path.absolute()
    if path.is_symlink() or not stat.S_ISDIR(metadata_value.st_mode) or resolved != candidate:
        raise RuntimeUpgradeError("artifact directory must be an unredirected directory")
    return candidate


def _checked_artifact_file(path: Path) -> None:
    try:
        metadata_value = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeUpgradeError(f"artifact is missing or unsafe: {path.name}") from error
    if path.is_symlink() or not stat.S_ISREG(metadata_value.st_mode) or resolved != path:
        raise RuntimeUpgradeError(f"artifact must be a regular file: {path.name}")


def _hash_artifacts(
    artifacts: tuple[Path, ...], expected: Mapping[str, str] | None
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for artifact in artifacts:
        _checked_artifact_file(artifact)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        digests[artifact.name] = digest
        if expected is not None and expected.get(artifact.name) != digest:
            raise RuntimeUpgradeError(f"artifact SHA-256 provenance mismatch: {artifact.name}")
    if expected is not None and set(expected) != set(digests):
        raise RuntimeUpgradeError("artifact SHA-256 provenance set does not match release")
    return digests


def _read_manifest(
    path: Path, engine: str, generation: str, expected_sha256: str | None = None
) -> dict[str, object]:
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise UnsafeStoragePathError("Worker manifest must be a regular file")
    if (
        expected_sha256 is not None
        and hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_sha256
    ):
        raise RuntimeUpgradeError("Worker manifest hash was tampered")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeUpgradeError("Worker manifest is unreadable") from error
    if (
        not isinstance(document, dict)
        or document.get("engine_id") != engine
        or document.get("generation") != generation
        or not isinstance(document.get("artifacts"), list)
    ):
        raise RuntimeUpgradeError("Worker manifest identity is invalid")
    artifact_root = path / "artifacts"
    _checked_directory(artifact_root, "candidate artifact directory")
    names: set[str] = set()
    for item in document["artifacts"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise RuntimeUpgradeError("Worker manifest artifact entry is invalid")
        name = item["name"]
        if name in names or not any(name.startswith(prefix) for prefix in _ARTIFACT_PREFIXES):
            raise RuntimeUpgradeError("Worker manifest artifact set is invalid")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RuntimeUpgradeError("Worker manifest artifact hash is invalid")
        artifact = artifact_root / name
        _checked_artifact_file(artifact)
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise RuntimeUpgradeError("Worker artifact hash was tampered")
        names.add(name)
    required_prefixes = {
        "tts_studio_protocol-",
        "tts_studio_worker_sdk-",
        "tts_studio_vieneu_worker-",
        "tts_studio_openai_compatible_worker-",
    }
    root_wheels = {
        name for name in names if name.startswith("tts_studio-") and name.endswith(".whl")
    }
    root_sdists = {
        name for name in names if name.startswith("tts_studio-") and name.endswith(".tar.gz")
    }
    fake_count = sum(name.startswith("tts_studio_fake_worker-") for name in names)
    if (
        len(names) not in {6, 7}
        or len(root_wheels) != 1
        or len(root_sdists) != 1
        or fake_count > 1
        or not all(
            sum(name.startswith(prefix) for name in names) == 1
            for prefix in required_prefixes
        )
    ):
        raise RuntimeUpgradeError("Worker manifest artifact set is incomplete")
    return document


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()


def _check_health(check: HealthCheck, engine: str, candidate: Path) -> None:
    try:
        result = check(engine, candidate)
    except RuntimeUpgradeError:
        raise
    except Exception as error:
        raise RuntimeUpgradeError(f"{engine} candidate verification failed") from error
    if result is False:
        raise RuntimeUpgradeError(f"{engine} candidate verification failed")


def _verify_candidate_structure(engine: str, candidate: Path) -> None:
    """Fail closed unless the candidate is a uv-provenanced runnable environment."""
    try:
        spec = _ENGINE_SPECS[engine]
        environment = _checked_directory(candidate / "venv", "candidate virtual environment")
        _checked_artifact_file(environment / "pyvenv.cfg")
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        _checked_directory(scripts, "candidate executable directory")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        _checked_artifact_file(python)
        executable = scripts / spec.executable
        _checked_artifact_file(executable)
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise RuntimeUpgradeError("candidate console script is not executable")
        site_packages = _site_packages(environment)
        distributions = _distributions(site_packages)
        worker = distributions.get(_normalize(spec.distribution))
        if worker is None or not _uv_provenance(worker):
            raise RuntimeUpgradeError("candidate Worker distribution lacks uv provenance")
        if not any(
            entry.group == "console_scripts"
            and entry.name == spec.executable
            and entry.value == spec.entry_point
            for entry in worker.entry_points
        ):
            raise RuntimeUpgradeError("candidate Worker console script is invalid")
        for required in ("tts-studio-protocol", "tts-studio-worker-sdk"):
            distribution = distributions.get(_normalize(required))
            if distribution is None or not _uv_provenance(distribution):
                raise RuntimeUpgradeError("candidate protocol/SDK lacks uv provenance")
        for name, version in spec.dependencies:
            dependency = distributions.get(_normalize(name))
            if (
                dependency is None
                or dependency.version != version
                or not _uv_provenance(dependency)
            ):
                raise RuntimeUpgradeError(f"candidate dependency {name} is not verified")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise RuntimeUpgradeError(f"candidate verification failed for {engine}") from error


def _site_packages(environment: Path) -> tuple[Path, ...]:
    candidates = [environment / "Lib" / "site-packages"]
    lib = environment / "lib"
    if lib.is_dir() and not lib.is_symlink():
        candidates.extend(lib.glob("python*/site-packages"))
    return tuple(path for path in candidates if path.is_dir() and not path.is_symlink())


def _distributions(paths: tuple[Path, ...]) -> dict[str, metadata.Distribution]:
    return {
        _normalize(distribution.metadata.get("Name", "")): distribution
        for distribution in metadata.distributions(path=[str(path) for path in paths])
        if distribution.metadata.get("Name")
    }


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _uv_provenance(distribution: metadata.Distribution) -> bool:
    if distribution.read_text("INSTALLER") != "uv\n":
        return False
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is None:
        return False
    try:
        document = json.loads(direct_url)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(document, dict)
        and isinstance(document.get("url"), str)
        and isinstance(document.get("archive_info"), dict)
    )


def _write_json(path: Path, document: object) -> None:
    _atomic_bytes(
        path, (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def _atomic_bytes(path: Path, content: bytes) -> None:
    parent = _checked_directory(path.parent, "managed JSON parent")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UnsafeStoragePathError(f"managed JSON path is not a regular file: {path}")
    temporary = parent / f".tmp-{uuid4().hex}.json"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        temporary.unlink(missing_ok=True)


def _remove_owned_generation(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        raise UnsafeStoragePathError("staged Worker generation is not a directory")
    shutil.rmtree(path)


def _run_command(command: tuple[str, ...], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


__all__ = [
    "RuntimeStatus",
    "RuntimeUpgradeError",
    "RuntimeUpgradeManager",
    "UpgradeResult",
]
