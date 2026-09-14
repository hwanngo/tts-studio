import os
import stat
from pathlib import Path

import pytest

from tts_studio.storage.layout import StorageLayout


def test_ensure_creates_only_managed_directories(tmp_path: Path) -> None:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    assert {path.name for path in layout.root.iterdir()} == {
        "database",
        "models",
        "audio",
        "voices",
        "workers",
        "logs",
        "run",
        "staging",
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not ACL policy")
def test_ensure_tightens_permissive_managed_directory_modes(tmp_path: Path) -> None:
    root = tmp_path / ".tts-studio"
    root.mkdir(mode=0o777)
    for name in ("database", "models", "audio", "voices", "workers", "logs", "run", "staging"):
        (root / name).mkdir(mode=0o777)
    root.chmod(0o777)

    layout = StorageLayout.from_root(root)
    layout.ensure()

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE((root / name).stat().st_mode) == 0o700
        for name in ("database", "models", "audio", "voices", "workers", "logs", "run", "staging")
    )


def test_construction_cannot_redirect_a_managed_child(tmp_path: Path) -> None:
    root = tmp_path / ".tts-studio"
    with pytest.raises(TypeError):
        StorageLayout(
            root=root,
            database=root / "database",
            models=root / "models",
            audio=tmp_path / "outside",
            voices=root / "voices",
            workers=root / "workers",
            logs=root / "logs",
            run=root / "run",
            staging=root / "staging",
        )


@pytest.mark.parametrize("child_name", ["logs", "run"])
def test_ensure_rejects_a_symlinked_managed_directory(
    tmp_path: Path,
    child_name: str,
) -> None:
    root = tmp_path / ".tts-studio"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / child_name).symlink_to(outside, target_is_directory=True)

    layout = StorageLayout.from_root(root)

    with pytest.raises(RuntimeError, match=rf"managed directory .*{child_name}.*unsafe"):
        layout.ensure()

    assert tuple(outside.iterdir()) == ()


@pytest.mark.parametrize("child_name", ["logs", "run"])
def test_ensure_rejects_a_non_directory_managed_child(
    tmp_path: Path,
    child_name: str,
) -> None:
    root = tmp_path / ".tts-studio"
    root.mkdir()
    (root / child_name).write_text("not a directory", encoding="utf-8")

    layout = StorageLayout.from_root(root)

    with pytest.raises(RuntimeError, match=rf"managed directory .*{child_name}.*unsafe"):
        layout.ensure()


def test_reference_staging_is_a_checked_child_of_staging(tmp_path: Path) -> None:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    assert layout.reference_staging == layout.staging / "references"
    assert layout.reference_staging.is_dir()


def test_reference_staging_preserves_operational_filesystem_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    references = layout.staging / "references"
    references.mkdir()
    original_mkdir = Path.mkdir

    def fail_references(self: Path, *args: object, **kwargs: object) -> None:
        if self == references:
            raise PermissionError("staging unavailable")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_references)

    with pytest.raises(PermissionError, match="staging unavailable"):
        _ = layout.reference_staging
