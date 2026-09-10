"""Fail when either checked-in OpenAPI client is out of date."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_openapi_client import generate  # type: ignore[import-not-found]


def main(repository_root: Path | None = None) -> int:
    root = repository_root or Path(__file__).resolve().parents[1]
    checked_in_typescript = root / "web" / "src" / "generated" / "api.ts"
    checked_in_python = root / "src" / "tts_studio" / "generated" / "api.py"
    with tempfile.TemporaryDirectory(prefix="tts-studio-openapi-check-") as directory:
        expected_typescript_path = Path(directory) / "api.ts"
        expected_python_path = Path(directory) / "api.py"
        expected_typescript = generate(expected_typescript_path, expected_python_path)
        expected_python = expected_python_path.read_text(encoding="utf-8")

    drift = []
    if checked_in_typescript.read_text(encoding="utf-8") != expected_typescript:
        drift.append("web/src/generated/api.ts")
    if checked_in_python.read_text(encoding="utf-8") != expected_python:
        drift.append("src/tts_studio/generated/api.py")
    if not drift:
        return 0
    print(f"generated OpenAPI client drift: {', '.join(drift)}")
    print("run `uv run python scripts/generate_openapi_client.py` and commit the result")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
