from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_REMOTE_URL = re.compile(r"(?:https?:)?//[^\s\"'()]+")


def _remote_font_urls(document: str) -> tuple[str, ...]:
    """Return network URLs used by the HTML shell or a stylesheet.

    Relative asset paths and data URLs are self-contained and deliberately do
    not match; protocol-relative URLs remain network requests and do match.
    """
    return tuple(dict.fromkeys(_REMOTE_URL.findall(document)))


def test_web_assets_do_not_request_remote_fonts() -> None:
    shell = (_REPOSITORY_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (_REPOSITORY_ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert _remote_font_urls(shell) == ()
    assert _remote_font_urls(styles) == ()
    assert "ui-sans-serif" in styles
    assert "ui-monospace" in styles


@pytest.mark.parametrize(
    ("stylesheet", "expected"),
    [
        ('@import url("https://fonts.example.test/brand.css");', ("https://fonts.example.test/brand.css",)),
        ('@font-face { src: url(//cdn.example.test/brand.woff2); }', ("//cdn.example.test/brand.woff2",)),
        ('@font-face { src: url("data:font/woff2;base64,AAAA"); }', ()),
        ('@font-face { src: url("/assets/brand.woff2"); }', ()),
    ],
)
def test_remote_font_url_detection_rejects_any_host(
    stylesheet: str, expected: tuple[str, ...]
) -> None:
    assert _remote_font_urls(stylesheet) == expected


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
