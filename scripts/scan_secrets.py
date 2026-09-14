"""Scan tracked text files for repository-configured, high-confidence secrets."""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_CONFIG = Path("security/secret-scan.toml")


@dataclass(frozen=True)
class Rule:
    identifier: str
    description: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Configuration:
    max_file_bytes: int
    excluded_paths: tuple[str, ...]
    rules: tuple[Rule, ...]


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _load_configuration(path: Path) -> Configuration:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read configuration {path}: {error}") from error

    scan = document.get("scan")
    if not isinstance(scan, dict):
        raise TypeError("configuration must contain a [scan] table")
    max_file_bytes = scan.get("max_file_bytes")
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise ValueError("scan.max_file_bytes must be a positive integer")
    excluded_paths_raw = scan.get("excluded_paths", [])
    if not isinstance(excluded_paths_raw, list) or not all(
        isinstance(path, str) and path for path in excluded_paths_raw
    ):
        raise ValueError("scan.excluded_paths must be an array of non-empty strings")

    raw_rules = document.get("rule")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("configuration must contain at least one [[rule]] table")
    rules: list[Rule] = []
    identifiers: set[str] = set()
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise TypeError(f"rule {index} must be a table")
        identifier = _require_string(raw_rule.get("id"), f"rule {index}.id")
        description = _require_string(raw_rule.get("description"), f"rule {identifier}.description")
        expression = _require_string(raw_rule.get("pattern"), f"rule {identifier}.pattern")
        if identifier in identifiers:
            raise ValueError(f"rule id {identifier!r} is duplicated")
        identifiers.add(identifier)
        try:
            pattern = re.compile(expression)
        except re.error as error:
            raise ValueError(f"rule {identifier!r} has an invalid pattern: {error}") from error
        rules.append(Rule(identifier, description, pattern))
    return Configuration(max_file_bytes, tuple(excluded_paths_raw), tuple(rules))


def _tracked_paths() -> Iterable[PurePosixPath]:
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    for encoded_path in result.stdout.split(b"\0"):
        if encoded_path:
            yield PurePosixPath(encoded_path.decode("utf-8", errors="surrogateescape"))


def _is_excluded(path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path.as_posix(), pattern) for pattern in patterns)


def _matching_lines(text: str, rule: Rule) -> Iterable[int]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if rule.pattern.search(line):
            yield line_number


def _has_symbolic_link_component(path: Path) -> bool:
    current = Path()
    for component in path.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


def _scan(configuration: Configuration) -> list[tuple[PurePosixPath, int, Rule]]:
    findings: list[tuple[PurePosixPath, int, Rule]] = []
    for relative_path in _tracked_paths():
        if _is_excluded(relative_path, configuration.excluded_paths):
            continue
        path = Path(relative_path)
        try:
            if _has_symbolic_link_component(path):
                raise RuntimeError(f"refusing to scan symbolic link: {relative_path}")
            if not path.is_file() or path.stat().st_size > configuration.max_file_bytes:
                continue
            content = path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"cannot scan {relative_path}: {error}") from error
        if b"\0" in content:
            continue
        text = content.decode("utf-8", errors="replace")
        for rule in configuration.rules:
            findings.extend((relative_path, line, rule) for line in _matching_lines(text, rule))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    arguments = parser.parse_args()
    try:
        configuration = _load_configuration(arguments.config)
        findings = _scan(configuration)
    except (RuntimeError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"secret scan configuration error: {error}", file=sys.stderr)
        return 2
    if not findings:
        print("secret scan passed: no configured high-confidence secret patterns found")
        return 0
    for path, line, rule in findings:
        print(f"{path}:{line}: {rule.identifier}: {rule.description}", file=sys.stderr)
    print(
        "secret scan failed; rotate any real credential before adding a narrowly reviewed rule",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
