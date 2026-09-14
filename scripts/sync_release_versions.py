"""Synchronize package metadata from the root project's authoritative release version."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = (
    "pyproject.toml",
    "packages/protocol/pyproject.toml",
    "packages/worker-sdk/pyproject.toml",
    "workers/fake/pyproject.toml",
    "workers/openai_compatible/pyproject.toml",
    "workers/vieneu/pyproject.toml",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="reject drift without writing files")
    arguments = parser.parse_args()
    release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    drift = False
    for relative in PROJECTS:
        path = ROOT / relative
        original = path.read_text(encoding="utf-8")
        expected = re.sub(
            r'^version = "[^"]+"$',
            f'version = "{release}"',
            original,
            count=1,
            flags=re.MULTILINE,
        )
        expected = re.sub(
            r'"(tts-studio-(?:protocol|worker-sdk))(?:[<>=!~][^"\n]*)?",',
            lambda match: f'"{match[1]}=={release}",',
            expected,
        )
        if expected != original:
            drift = True
            if arguments.check:
                print(f"Release metadata differs from root version {release}: {relative}")
            else:
                path.write_text(expected, encoding="utf-8")
                print(f"Updated {relative} to release {release}")
    return 1 if arguments.check and drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
