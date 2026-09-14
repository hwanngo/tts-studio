# Task 8 — Security scanning and controlled hardware gate

## Delivered

- Added `.github/workflows/security.yml`, a 12-minute Ubuntu CodeQL job for
  Python and JavaScript/TypeScript. Its checkout and both CodeQL actions are
  pinned to full commit SHAs, and its permissions are limited to `contents:
  read` and CodeQL's required `security-events: write` SARIF upload.
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
validation_bin=$(mktemp -d)/bin && GOBIN="$validation_bin" go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 && "$validation_bin/actionlint"
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

## Review follow-up

- Replaced the invalid annotated-tag object used for CodeQL with the upstream
  verified `github/codeql-action` v3.38.0 commit
  `faaca9a8f6edddba5725ffe5adefdab6669a2eca`. GitHub's commit API reported a
  valid verified signature, and the `init/action.yml` and `analyze/action.yml`
  manifests at that exact commit were inspected. The unsupported multi-language
  `build-mode` input was removed.
- Kept the normal pull-request assurance budget intact: `security.yml` now
  runs only on a `main` push, weekly schedule, or manual dispatch. The local
  secret scan moved to the existing Linux assurance job, so PRs get the scan
  without a second hosted runner or a higher 25-minute ceiling.
- Added `scripts/check_vieneu_hardware_cache.py`; the hardware workflow calls
  it before `pytest`. It fails on invalid marker JSON/provenance, symlinks, or
  any missing required model/codec file, so a prerequisite skip cannot make
  the scheduled/manual gate green.
- Added focused tests for cache preflight failures, quoted JSON `api_key`
  values, source-expression false-positive avoidance, and tracked symlink
  rejection. The scanner now rejects a symlink in any component of a tracked
  path rather than following it into untracked data.

## P2 follow-up

- Hardened the hardware-cache preflight equivalently: it now checks every path
  component from the cache root through the marker, model directories, and
  required files before calling `is_dir()` or `is_file()`. It therefore rejects
  a linked `cache/models` directory and a linked nested `backbone` directory
  without traversing either target.
- Added both intermediate-directory regressions while retaining the complete
  regular-directory/file acceptance test. Focused cache-preflight validation
  passes with `8 passed`.
