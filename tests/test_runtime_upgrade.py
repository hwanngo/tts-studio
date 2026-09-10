from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tts_studio.runtime_upgrade import RuntimeUpgradeError, RuntimeUpgradeManager
from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError

ARTIFACT_NAMES = (
    "tts_studio-0.1.0-py3-none-any.whl",
    "tts_studio-0.1.0.tar.gz",
    "tts_studio_protocol-0.1.0-py3-none-any.whl",
    "tts_studio_worker_sdk-0.1.0-py3-none-any.whl",
    "tts_studio_vieneu_worker-0.1.0-py3-none-any.whl",
    "tts_studio_openai_compatible_worker-0.1.0-py3-none-any.whl",
)


def _artifacts(directory: Path) -> None:
    directory.mkdir()
    for name in ARTIFACT_NAMES:
        (directory / name).write_bytes(name.encode())


def _manager(
    tmp_path: Path,
    *,
    health: list[tuple[str, Path]] | None = None,
) -> RuntimeUpgradeManager:
    def check(engine: str, candidate: Path) -> None:
        if health is not None:
            health.append((engine, candidate))

    return RuntimeUpgradeManager(
        StorageLayout.from_root(tmp_path / "data"),
        required_engines=("vieneu", "openai_compatible"),
        command_runner=lambda _command, _cwd: None,
        health_check=check,
    )


def _pointer(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "data" / "workers" / "current.json").read_text())


def test_upgrade_uses_one_shared_pointer_and_records_exact_artifact_hashes(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    health: list[tuple[str, Path]] = []

    result = _manager(tmp_path, health=health).upgrade(release, "2026.09.08")

    assert result.previous_generations == {"openai_compatible": None, "vieneu": None}
    pointer = _pointer(tmp_path)
    assert pointer["generation"] == "2026.09.08"
    assert pointer["previous_generation"] is None
    assert set(pointer["engines"]) == {"openai_compatible", "vieneu"}
    assert not tuple((tmp_path / "data" / "workers").glob("*/current.json"))
    for engine in ("vieneu", "openai_compatible"):
        entry = pointer["engines"][engine]
        assert entry["generation"] == "2026.09.08"
        manifest_path = (
            tmp_path / "data" / "workers" / engine / "generations" / "2026.09.08" / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        assert entry["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        assert {item["name"] for item in manifest["artifacts"]} == set(ARTIFACT_NAMES)
        assert (
            manifest["artifacts"][0]["sha256"]
            == hashlib.sha256((release / ARTIFACT_NAMES[0]).read_bytes()).hexdigest()
        )
    assert {engine for engine, _ in health} == {"openai_compatible", "vieneu"}


def test_status_rejects_divergent_shared_pointer_state(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    manager = _manager(tmp_path)
    manager.upgrade(release, "one")
    pointer_path = tmp_path / "data" / "workers" / "current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["engines"]["vieneu"]["generation"] = "other"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(RuntimeUpgradeError, match="divergent"):
        manager.status()


def test_rollback_rejects_malformed_or_dangling_shared_pointer(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    manager = _manager(tmp_path)
    manager.upgrade(release, "one")
    manager.upgrade(release, "two")
    pointer_path = tmp_path / "data" / "workers" / "current.json"
    pointer_path.unlink()
    pointer_path.symlink_to(tmp_path / "missing-pointer.json")

    with pytest.raises(UnsafeStoragePathError, match="pointer"):
        manager.rollback()


def test_tampered_active_manifest_is_rejected_before_preserving_it(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    manager = _manager(tmp_path)
    manager.upgrade(release, "one")
    manifest_path = (
        tmp_path / "data" / "workers" / "vieneu" / "generations" / "one" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeUpgradeError, match="manifest|artifact"):
        manager.upgrade(release, "two")

    assert _pointer(tmp_path)["generation"] == "one"


def test_upgrade_rejects_symlinked_artifact_root(tmp_path: Path) -> None:
    real_release = tmp_path / "real-release"
    _artifacts(real_release)
    release = tmp_path / "release"
    release.symlink_to(real_release, target_is_directory=True)

    with pytest.raises(RuntimeUpgradeError, match="artifact"):
        _manager(tmp_path).upgrade(release, "one")


def test_default_verification_fails_closed_without_a_real_candidate(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    manager = RuntimeUpgradeManager(
        StorageLayout.from_root(tmp_path / "data"),
        required_engines=("vieneu",),
        command_runner=lambda _command, _cwd: None,
    )

    with pytest.raises(RuntimeUpgradeError, match="verification"):
        manager.upgrade(release, "one")


def test_failed_candidate_health_check_leaves_shared_pointer_untouched(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    manager = _manager(tmp_path)
    manager.upgrade(release, "old")

    def reject_new(_engine: str, candidate: Path) -> None:
        if candidate.name == "new":
            raise RuntimeUpgradeError("candidate unhealthy")

    manager = RuntimeUpgradeManager(
        StorageLayout.from_root(tmp_path / "data"),
        required_engines=("vieneu", "openai_compatible"),
        command_runner=lambda _command, _cwd: None,
        health_check=reject_new,
    )
    with pytest.raises(RuntimeUpgradeError, match="unhealthy"):
        manager.upgrade(release, "new")

    assert _pointer(tmp_path)["generation"] == "old"
    assert not tuple((tmp_path / "data" / "workers").glob("*/current.json"))


def test_rollback_health_checks_all_previous_engines_before_switching(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)
    manager = _manager(tmp_path)
    manager.upgrade(release, "old")
    manager.upgrade(release, "new")

    checked: list[str] = []

    def health(engine: str, candidate: Path) -> None:
        checked.append(f"{engine}:{candidate.name}")
        if candidate.name == "old" and engine == "vieneu":
            raise RuntimeUpgradeError("old generation is unhealthy")

    manager = RuntimeUpgradeManager(
        StorageLayout.from_root(tmp_path / "data"),
        required_engines=("vieneu", "openai_compatible"),
        health_check=health,
        command_runner=lambda _command, _cwd: None,
    )
    with pytest.raises(RuntimeUpgradeError, match="unhealthy"):
        manager.rollback()

    assert checked == ["openai_compatible:old", "vieneu:old"]
    assert _pointer(tmp_path)["generation"] == "new"


def test_upgrade_rejects_unsafe_engine_and_redirected_managed_paths(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _artifacts(release)

    with pytest.raises(UnsafeStoragePathError):
        RuntimeUpgradeManager(
            StorageLayout.from_root(tmp_path / "data"), required_engines=("../x",)
        )

    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    (layout.workers / "vieneu").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(UnsafeStoragePathError):
        RuntimeUpgradeManager(layout, required_engines=("vieneu",)).upgrade(release, "one")
