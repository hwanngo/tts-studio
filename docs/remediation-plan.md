# Audit remediation plan

**Goal:** Resolve every repository-addressable finding without changing the Core/Worker
architectural boundaries.

**Architecture:** The work is divided into security and request lifecycle, Web behavior, Worker
supervision, supply-chain metadata, CI/release assurance, and documentation. Each behavior change
gets a focused regression test before implementation; generated contracts are refreshed whenever a
public schema changes.

**Technology:** Python 3.14, FastAPI, SQLite, gRPC/protobuf, React, TypeScript, Vitest, Playwright,
uv, pnpm, GitHub Actions.

## Constraints

- FastAPI remains the sole public server and SQLite writer.
- Workers remain isolated and communicate only by the versioned protobuf/gRPC protocol.
- Native clients without an `Origin` header remain supported.
- Remote browser access requires a configured bearer token; no token is persisted by the Web UI.
- Platform-neutral CI work runs once; platform checks and all jobs have bounded execution time.
- No external model download, credential, or hardware dependency is required for pull requests.

## Tasks

### Task 1: HTTP trust boundary and authenticated browser session

**Files:** `src/tts_studio/server/app.py`, `src/tts_studio/server/errors.py`, route tests,
`web/src/lib/api.ts`, the application shell, Studio and History media rendering, Web tests, and
`docs/http-api.md`.

1. Write failing route tests for rejected arbitrary Host headers, rejected cross-origin mutations,
   accepted same-origin mutations, and token-authenticated browser API calls.
2. Add a Host/origin middleware configured from the listener host and port. Accept absent Origin for
   CLI/native clients; require the exact public origin for browser mutations.
3. Add a memory-only browser token session. Attach its bearer value to every JSON, PCM, event, media,
   and download request; create/revoke object URLs for authenticated audio and downloads.
4. Write Web tests for token prompt, authenticated request headers, media object URLs, and session
   clearing after authentication failure. Update the HTTP authentication contract.

### Task 2: Generation validation, privacy, and retryable job lifecycle

**Files:** generation route/service/registry/domain and migrations, public route tests, generation
integration tests, generated OpenAPI artifacts, `docs/http-api.md`, and `docs/operations.md`.

1. Write failing tests that reject more than 10,000 characters before persistence and reject an
   oversized request body.
2. Define one Core text-size constant and enforce it at HTTP validation and service entry points.
3. Write failing tests proving a non-retained terminal job no longer exposes or stores text while a
   retained job still does.
4. Add a migration and registry transition that redacts non-retained text atomically at terminal
   completion, preserve response compatibility with an empty text field, and document the behavior.
5. Define an explicit retry endpoint/action for retryable terminal jobs only; test idempotence,
   artifact/reference ownership, and unsupported terminal states.

### Task 3: Worker containment, cancellation, and capacity

**Files:** supervisor/process lifecycle modules, generation service, supervisor and integration
tests, and `docs/operations.md`.

1. Write failing tests that prove separate ready replicas can synthesize concurrently and that an
   uncooperative cancellation quarantines then terminates its Worker.
2. Replace the process-wide generation gate with supervisor lease admission plus narrow lifecycle
   coordination.
3. Add platform process containment, using a Windows Job Object and POSIX process groups, and expose
   one hard-stop operation used after cancellation grace expires.
4. Test descendant cleanup, failed termination, quarantine/replacement, and retry handoff. Document
   the revised bounded-cancellation and retry guarantees.

### Task 4: Web model/voice capability behavior and accessibility

**Files:** Studio and Voices pages, API helpers, translations, styles, Web tests, and `docs/http-api.md`.

1. Write failing tests for both preset/saved-voice response orders and a saved-voice-only model.
2. Fetch voices as one merged result, preserve a valid selected voice, and deterministically select
   the first available merged voice.
3. Gate Generate, Cancel, Preview, cues, and prosody from advertised Worker capabilities, including
   localized disabled reasons.
4. Update `<html lang>` at initialization and on locale changes; make compact action hit areas at
   least 44 CSS pixels. Test language and capability behavior.

### Task 5: Local-first assets and user documentation

**Files:** `web/index.html`, local CSS/font assets or font stack, `README.md`, `LICENSE`, project
metadata, and documentation tests where present.

1. Remove external Google font requests and use bundled or system typography without network access.
2. Replace display-name CLI examples with commands that list and pass actual model and voice IDs.
3. Add the MIT license text and SPDX-compatible project metadata. Verify package metadata includes it.

### Task 6: Internal release-set compatibility and type debt

**Files:** all package `pyproject.toml` files, version sources/build configuration, package metadata
tests, typed source modules, `scripts/mypy-baseline.txt`, and `docs/development.md`.

1. Write metadata tests that install only built artifacts and reject unsupported mixed internal
   package versions.
2. Version Core, protocol, Worker SDK, and Workers as a compatible release set and constrain each
   internal dependency to that set.
3. Fix every existing baseline diagnostic or explicitly type only the lightweight Worker source with
   narrow third-party exclusions; remove the baseline acceptance mechanism once diagnostics are zero.
4. Run strict mypy and package-install smoke tests from clean temporary environments.

### Task 7: Lean deterministic CI and browser/Worker coverage

**Files:** `.github/workflows/ci.yml`, browser test configuration, development documentation, and
CI-specific helper scripts/tests.

1. Split Linux-only static, lint, format, type, protocol, OpenAPI, coverage, and Web build checks
   from the OS release-smoke matrix.
2. Run deterministic Python tests and clean installed-artifact CLI smoke on Linux, macOS, and Windows;
   run every locked Worker suite on Linux; run Chromium Playwright with traces/screenshots on failure.
3. Add concurrency cancellation, restrict duplicate push triggering to default-branch/release paths,
   and retain explicit job timeouts.
4. Pin every third-party Action to a reviewed full commit SHA and add an update mechanism.
5. Add coverage collection with a non-regression baseline/threshold and make CI's formatting check
   run `ruff format --check`.

### Task 8: Security scanning and controlled hardware gate

**Files:** CodeQL/secret scanning workflow configuration, a scheduled self-hosted hardware workflow,
repository settings documentation, and operations/development docs.

1. Add CodeQL analysis for Python and JavaScript/TypeScript and a repository-local secret scan to the
   bounded Linux security job.
2. Add a scheduled/manual hardware workflow that only targets a labelled self-hosted runner and uses
   the existing explicit cache marker; pull requests never invoke it.
3. Document the GitHub settings required to enable platform secret scanning and code scanning when
   the repository plan supports them, including expected alert triage ownership.

### Task 9: Documentation consistency and release evidence

**Files:** architecture, operations, development, HTTP API, documentation index, audit, and README.

1. Align replica implementation status and remove the nonexistent roadmap link.
2. Update all changed security, retention, authentication, retry, CI, and release contracts.
3. Replace resolved audit entries with verification evidence; leave only externally controlled GitHub
   settings as explicit operational prerequisites.

### Task 10: Full verification and release handoff

**Files:** affected source/tests/docs only.

1. Run protocol/OpenAPI checks, all Python/Worker/Web tests, type checks, formatting/lint, coverage,
   package metadata/install smoke, lock verification, and a local build.
2. Review the complete diff for architectural-invariant and documentation consistency.
3. Commit the remediation in reviewable logical commits and report any GitHub settings that still
   require repository-owner action.
