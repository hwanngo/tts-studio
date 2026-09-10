"""Run mypy and fail when diagnostics drift from the checked-in baseline."""

from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "mypy-baseline.txt"
MYPY_TARGETS = (
    "src",
    "packages/protocol/src",
    "packages/worker-sdk/src",
)
MYPY_PLATFORM = "linux"


def normalize_output(output: str) -> str:
    """Keep stable diagnostics while dropping mypy's variable summary line."""
    lines = [line.rstrip().replace("\\", "/") for line in output.splitlines()]
    diagnostics = sorted(
        line
        for line in lines
        if line and not line.startswith(("Found ", "Success: "))
    )
    return "\n".join(diagnostics) + ("\n" if diagnostics else "")


def run_mypy() -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "mypy", "--platform", MYPY_PLATFORM, *MYPY_TARGETS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, normalize_output(completed.stdout + completed.stderr)


def update_baseline() -> int:
    _returncode, output = run_mypy()
    BASELINE.write_text(output, encoding="utf-8")
    print(f"Wrote {BASELINE} with {len(output.splitlines())} diagnostics.")
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--update":
        return update_baseline()
    if len(sys.argv) != 1:
        print("usage: check_mypy_baseline.py [--update]", file=sys.stderr)
        return 2

    returncode, current = run_mypy()
    expected = BASELINE.read_text(encoding="utf-8") if BASELINE.exists() else ""
    print(current, end="")
    if current != expected:
        print("mypy diagnostics differ from the checked-in baseline:", file=sys.stderr)
        diff = difflib.unified_diff(
            expected.splitlines(),
            current.splitlines(),
            fromfile=str(BASELINE),
            tofile="current mypy output",
            lineterm="",
        )
        print("\n".join(diff), file=sys.stderr)
        return 1
    print(f"mypy diagnostics match baseline ({len(current.splitlines())} lines).")
    return 0 if returncode in (0, 1) else returncode


if __name__ == "__main__":
    raise SystemExit(main())
