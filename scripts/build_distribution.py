"""Build the Web UI and embed it in the Python distributions."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_STATIC_RELATIVE = Path("src") / "tts_studio" / "static"
_WEB_DIST_RELATIVE = Path("web") / "dist"
_FAKE_WORKER_RELATIVE = Path("workers") / "fake"
_OPENAI_WORKER_RELATIVE = Path("workers") / "openai_compatible"
_VIENEU_WORKER_RELATIVE = Path("workers") / "vieneu"


def _build_tool(name: str) -> str:
    return "pnpm.cmd" if os.name == "nt" and name == "pnpm" else name


def _resolve_repository_path(repository_root: Path, relative_path: Path) -> Path:
    expected = repository_root / relative_path
    resolved = expected.resolve()
    try:
        resolved.relative_to(repository_root)
    except ValueError as error:
        raise RuntimeError(f"refusing to access path outside repository: {resolved}") from error
    if resolved != expected:
        raise RuntimeError(f"refusing redirected repository path: {expected}")
    return resolved


def build_distribution(
    repository_root: Path,
    output_dir: Path | None = None,
    *,
    include_test_adapters: bool = False,
) -> None:
    """Compile Web assets and build production or test distribution artifacts."""
    root = repository_root.resolve()
    static_root = _resolve_repository_path(root, _STATIC_RELATIVE)
    web_dist = _resolve_repository_path(root, _WEB_DIST_RELATIVE)
    openai_worker = _resolve_repository_path(root, _OPENAI_WORKER_RELATIVE)
    vieneu_worker = _resolve_repository_path(root, _VIENEU_WORKER_RELATIVE)
    fake_worker = (
        _resolve_repository_path(root, _FAKE_WORKER_RELATIVE) if include_test_adapters else None
    )

    subprocess.run(
        [sys.executable, str(root / "scripts" / "sync_release_versions.py"), "--check"],
        cwd=root,
        check=True,
    )
    subprocess.run([_build_tool("pnpm"), "--dir", "web", "build"], cwd=root, check=True)

    static_root = _resolve_repository_path(root, _STATIC_RELATIVE)
    web_dist = _resolve_repository_path(root, _WEB_DIST_RELATIVE)
    if not web_dist.is_dir():
        raise RuntimeError(f"frontend build did not create {web_dist}")

    if static_root.exists():
        shutil.rmtree(static_root)
    shutil.copytree(web_dist, static_root)

    output_arguments: list[str] = []
    if output_dir is not None:
        output_arguments.extend(["--out-dir", str(output_dir.resolve())])
    subprocess.run(
        [
            _build_tool("uv"),
            "build",
            "--package",
            "tts-studio-protocol",
            "--wheel",
            *output_arguments,
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            _build_tool("uv"),
            "build",
            "--package",
            "tts-studio-worker-sdk",
            "--wheel",
            *output_arguments,
        ],
        cwd=root,
        check=True,
    )
    if fake_worker is not None:
        subprocess.run(
            [
                _build_tool("uv"),
                "build",
                "--wheel",
                str(fake_worker),
                *output_arguments,
            ],
            cwd=root,
            check=True,
        )
    subprocess.run(
        [
            _build_tool("uv"),
            "build",
            "--wheel",
            str(vieneu_worker),
            *output_arguments,
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            _build_tool("uv"),
            "build",
            "--wheel",
            str(openai_worker),
            *output_arguments,
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [_build_tool("uv"), "build", "--package", "tts-studio", *output_arguments],
        cwd=root,
        check=True,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="write distributions to this directory")
    parser.add_argument(
        "--include-test-adapters",
        action="store_true",
        help="also build deterministic fake Worker artifacts for test environments",
    )
    arguments = parser.parse_args(argv)
    build_distribution(
        _REPOSITORY_ROOT,
        arguments.out_dir,
        include_test_adapters=arguments.include_test_adapters,
    )


if __name__ == "__main__":
    main()
