from __future__ import annotations

import tomllib
from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_web_shell_does_not_request_google_fonts() -> None:
    shell = (_REPOSITORY_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (_REPOSITORY_ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in shell
    assert "fonts.gstatic.com" not in shell
    assert "ui-sans-serif" in styles
    assert "ui-monospace" in styles


def test_readme_uses_runtime_model_and_voice_ids() -> None:
    readme = (_REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "tts models list" in readme
    assert "tts voices list --model <model-installation-id>" in readme
    assert "--model <model-installation-id> --voice <voice-id>" in readme
    assert '--model vieneu' not in readme
    assert '"Adam"' not in readme


def test_project_declares_the_mit_license() -> None:
    metadata = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    license_text = (_REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")

    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert license_text.startswith("MIT License\n")
    assert "Permission is hereby granted, free of charge" in license_text
