from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_mypy_baseline", Path(__file__).parents[2] / "scripts" / "check_mypy_baseline.py"
)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def test_normalize_output_removes_variable_summary() -> None:
    assert checker.normalize_output("a diagnostic\nFound 1 errors in 1 file\n") == "a diagnostic\n"


def test_normalize_output_sorts_diagnostics_for_stable_comparison() -> None:
    output = "src/z.py:1: error: z\nsrc/a.py:1: error: a\nFound 2 errors in 2 files\n"

    assert checker.normalize_output(output) == (
        "src/a.py:1: error: a\nsrc/z.py:1: error: z\n"
    )


def test_main_accepts_matching_baseline(monkeypatch, tmp_path: Path, capsys) -> None:
    baseline = tmp_path / "mypy-baseline.txt"
    baseline.write_text("src/example.py:1: error: baseline\n", encoding="utf-8")
    monkeypatch.setattr(checker, "BASELINE", baseline)
    monkeypatch.setattr(checker, "run_mypy", lambda: (1, baseline.read_text(encoding="utf-8")))
    monkeypatch.setattr(checker.sys, "argv", ["check_mypy_baseline.py"])

    assert checker.main() == 0
    assert "match baseline" in capsys.readouterr().out


def test_remediation_egress_typing_is_not_accepted_as_baseline_debt() -> None:
    baseline = checker.BASELINE.read_text(encoding="utf-8")

    assert "tts_studio_worker_sdk/egress.py" not in baseline


def test_main_rejects_diagnostic_drift(monkeypatch, tmp_path: Path, capsys) -> None:
    baseline = tmp_path / "mypy-baseline.txt"
    baseline.write_text("src/example.py:1: error: baseline\n", encoding="utf-8")
    monkeypatch.setattr(checker, "BASELINE", baseline)
    monkeypatch.setattr(
        checker,
        "run_mypy",
        lambda: (1, "src/example.py:1: error: changed\nsrc/new.py:2: error: new\n"),
    )
    monkeypatch.setattr(checker.sys, "argv", ["check_mypy_baseline.py"])

    assert checker.main() == 1
    captured = capsys.readouterr()
    assert "diagnostics differ" in captured.err
    assert "+src/new.py:2: error: new" in captured.err
