from pathlib import Path

import pytest

from tts_studio.distribution import validate_release_artifacts


def _write_artifacts(directory: Path, *, include_test_adapters: bool = False) -> None:
    names = [
        "tts_studio-0.1.0-py3-none-any.whl",
        "tts_studio-0.1.0.tar.gz",
        "tts_studio_protocol-0.1.0-py3-none-any.whl",
        "tts_studio_worker_sdk-0.1.0-py3-none-any.whl",
        "tts_studio_vieneu_worker-0.1.0-py3-none-any.whl",
        "tts_studio_openai_compatible_worker-0.1.0-py3-none-any.whl",
    ]
    if include_test_adapters:
        names.append("tts_studio_fake_worker-0.1.0-py3-none-any.whl")
    for name in names:
        (directory / name).write_bytes(b"artifact")


def test_release_artifacts_require_one_complete_set(tmp_path: Path) -> None:
    _write_artifacts(tmp_path, include_test_adapters=True)

    artifacts = validate_release_artifacts(tmp_path, include_test_adapters=True)

    assert len(artifacts) == 7
    assert all(path.is_file() and not path.is_symlink() for path in artifacts)


def test_release_artifacts_reject_missing_worker(tmp_path: Path) -> None:
    _write_artifacts(tmp_path)
    (tmp_path / "tts_studio_openai_compatible_worker-0.1.0-py3-none-any.whl").unlink()

    with pytest.raises(ValueError, match="openai_compatible_worker"):
        validate_release_artifacts(tmp_path)


def test_release_artifacts_reject_duplicate_or_redirected_files(tmp_path: Path) -> None:
    _write_artifacts(tmp_path)
    (tmp_path / "tts_studio_openai_compatible_worker-0.2.0-py3-none-any.whl").write_bytes(
        b"duplicate"
    )

    with pytest.raises(ValueError, match="exactly one"):
        validate_release_artifacts(tmp_path)


def test_release_artifacts_reject_symlinked_file(tmp_path: Path) -> None:
    _write_artifacts(tmp_path)
    artifact = tmp_path / "tts_studio_openai_compatible_worker-0.1.0-py3-none-any.whl"
    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"outside")
    artifact.unlink()
    artifact.symlink_to(outside)

    with pytest.raises(ValueError, match="regular file"):
        validate_release_artifacts(tmp_path)


def test_release_artifacts_reject_symlinked_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    _write_artifacts(real_root)
    redirected = tmp_path / "redirected"
    redirected.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="unredirected directory"):
        validate_release_artifacts(redirected)
