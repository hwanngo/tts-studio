from __future__ import annotations

import runpy
from pathlib import Path, PurePosixPath

import pytest

scan_secrets = runpy.run_path(Path("scripts/scan_secrets.py"))


def _rule(identifier: str):
    configuration = scan_secrets["_load_configuration"](Path("security/secret-scan.toml"))
    return next(rule for rule in configuration.rules if rule.identifier == identifier)


def test_detects_quoted_json_api_key_literal() -> None:
    rule = _rule("assigned-service-secret")

    assert rule.pattern.search('{"api_key": "provider-secret-' + "a" * 10 + '12345"}')


def test_ignores_unquoted_source_attribute_expression() -> None:
    rule = _rule("assigned-service-secret")

    assert not rule.pattern.search("api_key=request.provider.api_key")


def test_refuses_to_follow_tracked_symbolic_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target_directory = tmp_path / "untracked-directory"
    target_directory.mkdir()
    (target_directory / "secret.json").write_text(
        '{"api_key": "provider-secret-' + "a" * 10 + '12345"}', encoding="utf-8"
    )
    (tmp_path / "tracked-directory").symlink_to(target_directory, target_is_directory=True)
    rule = _rule("assigned-service-secret")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(
        scan_secrets["_scan"].__globals__,
        "_tracked_paths",
        lambda: iter((PurePosixPath("tracked-directory/secret.json"),)),
    )
    configuration = scan_secrets["Configuration"](1024, (), (rule,))

    with pytest.raises(RuntimeError, match="refusing to scan symbolic link"):
        scan_secrets["_scan"](configuration)
