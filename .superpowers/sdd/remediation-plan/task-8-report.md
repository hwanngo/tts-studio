# Task 8 — Security scanning and controlled hardware gate

## Delivered

- Added `.github/workflows/security.yml`, a 12-minute Ubuntu job that runs
  CodeQL for Python and JavaScript/TypeScript and the checked-in secret scan.
  Its checkout and both CodeQL actions are pinned to full commit SHAs, and its
  permissions are limited to `contents: read` and CodeQL's required
  `security-events: write` SARIF upload.
- Added the deterministic, network-free `scripts/scan_secrets.py` and
  `security/secret-scan.toml`. It scans only Git-tracked text files, validates
  its configuration, limits file size, reports locations without echoing
  matched values, and contains only high-confidence credential patterns.
- Added `.github/workflows/vieneu-hardware.yml`. It is schedule/manual only,
  requires the exact `self-hosted`, `linux`, `x64`, and
  `tts-studio-vieneu-hardware` labels, and never has a pull-request trigger.
  The runner must already provide Python 3.14, `uv`, offline dependencies, and
  `/var/lib/tts-studio/vieneu-cache/.vieneu-model-cache-marker`; the workflow
  rejects missing or symbolic-link prerequisites and uses `uv --offline`.
- Added `.github/actionlint.yaml` so the custom runner label is checked rather
  than ignored.
- Documented plan-dependent GitHub code/secret scanning activation, push
  protection, security-maintainer ownership and one-business-day triage in
  `docs/operations.md`, with developer-facing workflow/scanner guidance in
  `docs/development.md`. Updated the corresponding audit remediation entry.

## Validation

Completed locally in the remediation worktree:

```console
actionlint v1.7.7
python3 -m py_compile scripts/scan_secrets.py
python3 scripts/scan_secrets.py
uv run --frozen ruff check scripts/scan_secrets.py
uv run --frozen ruff format --check scripts/scan_secrets.py
git diff --check
```

`actionlint` passed all repository workflows. YAML parsing passed for the
actionlint configuration and both new workflows. The secret scan passed, and a
separate in-process configuration check verified representative positive
matches for every rule plus the source-code false-positive guard for an
unquoted attribute expression. All action references under `.github/workflows`
were checked to use full 40-character commit SHAs.

Hosted CodeQL uploads and the self-hosted hardware smoke intentionally remain
for GitHub execution; no workflow dispatch or hardware/model download occurred
while completing this task.
