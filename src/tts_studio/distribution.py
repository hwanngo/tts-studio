"""Validation helpers for complete, user-independent release artifact sets."""

from __future__ import annotations

import stat
from pathlib import Path


def validate_release_artifacts(
    directory: Path, *, include_test_adapters: bool = False
) -> tuple[Path, ...]:
    """Return one complete release artifact set or raise before an upgrade can proceed."""

    root = directory.expanduser()
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"artifact directory does not exist: {root}") from error
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or resolved != root:
        raise ValueError(f"artifact directory must be an unredirected directory: {root}")
    if not root.is_dir():
        raise ValueError(f"artifact directory does not exist: {root}")
    required: tuple[tuple[str, str], ...] = (
        ("tts_studio-", ".whl"),
        ("tts_studio-", ".tar.gz"),
        ("tts_studio_protocol-", ".whl"),
        ("tts_studio_worker_sdk-", ".whl"),
        ("tts_studio_vieneu_worker-", ".whl"),
        ("tts_studio_openai_compatible_worker-", ".whl"),
    )
    if include_test_adapters:
        required += (("tts_studio_fake_worker-", ".whl"),)

    artifacts: list[Path] = []
    for prefix, suffix in required:
        matches = sorted(root.glob(f"{prefix}*{suffix}"))
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one release artifact for {prefix}*{suffix}, found {len(matches)}"
            )
        artifact = matches[0]
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"release artifact must be a regular file: {artifact}")
        artifacts.append(artifact)
    return tuple(artifacts)
