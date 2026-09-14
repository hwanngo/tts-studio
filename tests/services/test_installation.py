import os
from pathlib import Path

import pytest

from tts_studio.services import (
    ServicePaths,
    ServicePlatform,
    uninstall_definition,
    write_definition,
)


def test_service_paths_resolve_user_owned_platform_targets(tmp_path: Path) -> None:
    assert ServicePaths.resolve(ServicePlatform.DARWIN, "tts-studio", tmp_path).definition == (
        tmp_path / "Library/LaunchAgents/tts-studio.plist"
    )
    assert ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path).definition == (
        tmp_path / ".config/systemd/user/tts-studio.service"
    )
    assert ServicePaths.resolve(ServicePlatform.WINDOWS, "tts-studio", tmp_path).definition is None


def test_service_paths_require_absolute_home_and_reject_unsafe_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", Path("relative"))
    with pytest.raises(ValueError, match="name"):
        ServicePaths.resolve(ServicePlatform.LINUX, "../escape", tmp_path)
    for name in ("", "has/slash", "has\\slash", "has space", "has:colon", "has\nnewline"):
        with pytest.raises(ValueError, match="name"):
            ServicePaths.resolve(ServicePlatform.LINUX, name, tmp_path)


def test_write_definition_creates_parent_and_replaces_regular_file_atomically(
    tmp_path: Path,
) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)

    write_definition(paths, "first")
    assert paths.definition is not None
    assert paths.definition.read_text() == "first"
    write_definition(paths, "second")
    assert paths.definition.read_text() == "second"
    assert not list(paths.definition.parent.glob(".tts-studio-*"))


def test_write_definition_rejects_symlink_target(tmp_path: Path) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    assert paths.definition is not None
    paths.definition.parent.mkdir(parents=True)
    destination = tmp_path / "outside"
    paths.definition.symlink_to(destination)

    with pytest.raises(ValueError, match="symlink"):
        write_definition(paths, "unsafe")
    assert not destination.exists()


def test_write_definition_rejects_symlinked_parent_without_redirecting(tmp_path: Path) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    assert paths.definition is not None
    paths.definition.parent.parent.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    paths.definition.parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        write_definition(paths, "unsafe")
    assert not (redirected / "tts-studio.service").exists()


@pytest.mark.parametrize("target_kind", ["directory", "fifo"])
def test_write_definition_rejects_non_regular_target(tmp_path: Path, target_kind: str) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    assert paths.definition is not None
    paths.definition.parent.mkdir(parents=True)
    if target_kind == "directory":
        paths.definition.mkdir()
    else:
        os.mkfifo(paths.definition)

    with pytest.raises(ValueError, match="regular file"):
        write_definition(paths, "unsafe")


def test_windows_write_is_a_noop_and_uninstall_is_idempotent(tmp_path: Path) -> None:
    paths = ServicePaths.resolve(ServicePlatform.WINDOWS, "tts-studio", tmp_path)

    assert write_definition(paths, "ignored") is None
    assert uninstall_definition(paths) is False
    assert uninstall_definition(paths) is False


def test_uninstall_removes_regular_definition_and_is_idempotent(tmp_path: Path) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    write_definition(paths, "definition")

    assert uninstall_definition(paths) is True
    assert paths.definition is not None and not paths.definition.exists()
    assert uninstall_definition(paths) is False


def test_uninstall_rejects_symlink_target(tmp_path: Path) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    assert paths.definition is not None
    paths.definition.parent.mkdir(parents=True)
    paths.definition.symlink_to(tmp_path / "outside")

    with pytest.raises(ValueError, match="symlink"):
        uninstall_definition(paths)


def test_uninstall_rejects_symlinked_parent_without_redirecting(tmp_path: Path) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    assert paths.definition is not None
    paths.definition.parent.parent.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    paths.definition.parent.symlink_to(redirected, target_is_directory=True)
    outside = redirected / "tts-studio.service"
    outside.write_text("must remain")

    with pytest.raises(ValueError, match="symlink"):
        uninstall_definition(paths)
    assert outside.read_text() == "must remain"


def test_uninstall_rejects_symlinked_home_without_redirecting(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    symlinked_home = tmp_path / "home"
    symlinked_home.symlink_to(real_home, target_is_directory=True)
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", symlinked_home)

    with pytest.raises(ValueError, match="symlink"):
        uninstall_definition(paths)


@pytest.mark.parametrize("target_kind", ["directory", "fifo"])
def test_uninstall_rejects_non_regular_target(tmp_path: Path, target_kind: str) -> None:
    paths = ServicePaths.resolve(ServicePlatform.LINUX, "tts-studio", tmp_path)
    assert paths.definition is not None
    paths.definition.parent.mkdir(parents=True)
    if target_kind == "directory":
        paths.definition.mkdir()
    else:
        os.mkfifo(paths.definition)

    with pytest.raises(ValueError, match="regular file"):
        uninstall_definition(paths)
