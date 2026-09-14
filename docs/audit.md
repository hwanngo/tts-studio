# Project audit

## Scope and method

This report audits repository snapshot `e428896` across architecture, security, privacy,
persistence, Worker isolation, public APIs, CLI, Web UI, accessibility, internationalization,
testing, CI cost and coverage, packaging, dependencies, operations, and documentation. Findings are
based on independent code-review passes plus focused repository and GitHub diagnostics.

Severity reflects plausible impact, not implementation effort:

- **Critical:** immediate compromise, unrecoverable data loss, or release-blocking defect.
- **High:** exploitable security exposure or a major supported workflow/release gate is broken.
- **Medium:** material reliability, privacy, compatibility, usability, or assurance gap.
- **Low:** maintainability, documentation, metadata, or defense-in-depth improvement.

## Executive summary

No critical issue was found. The architecture has strong isolation and filesystem defenses, but the
audit found **6 high**, **14 medium**, and **5 low** findings. The first remediation group should be
the loopback HTTP trust boundary, authenticated-browser behavior, Saved Voice selection race, and
missing release tests for standalone Workers and non-macOS artifacts.

The current GitHub CI run for this snapshot passed on Linux, macOS, and Windows. All four Python
lockfiles are coherent, the pnpm lock is current, protocol/OpenAPI drift checks pass, Dependabot has
no open alert, and `pnpm audit` reports no known vulnerability. Those results do not cover the gaps
below.

## High findings

### A-01 — The tokenless loopback API accepts arbitrary Host headers

FastAPI is created without trusted-host/origin enforcement, while tokenless operation is allowed
for a loopback bind (`src/tts_studio/server/app.py:154-158`; authentication is conditional at
`src/tts_studio/server/errors.py:175-197`). A DNS-rebinding hostname can therefore become same-origin
with the local Core and access readable and mutating endpoints; CORS is not a DNS-rebinding defense.

**Remediation:** enforce a strict Host allowlist for loopback operation, reject unexpected browser
origins on mutations, and consider a random local session token even for the default deployment.

### A-02 — The authenticated Web UI cannot use the authenticated API

The Web client has no bearer-token bootstrap or header injection (`web/src/lib/api.ts:340-369`).
Protected media is bound directly to `<audio>` and download links
(`web/src/features/studio/StudioPage.tsx:288`; `web/src/features/history/HistoryPage.tsx:108-110`),
which cannot add an Authorization header. With non-loopback authentication enabled, `/` loads but
page data, playback, downloads, and event streaming receive `401`.

**Remediation:** add an explicit authenticated browser session, preferably a secure same-site cookie
established through a bootstrap flow. If bearer headers remain, fetch protected media into blobs and
replace native `EventSource` with an authenticated transport. Otherwise document and enforce the Web
UI as loopback-only.

### A-03 — Saved Voice discovery has a destructive race and empty-selection bug

Preset and Saved Voice requests run independently (`web/src/features/studio/StudioPage.tsx:149-155`).
Preset completion replaces the entire array, so it can erase Saved Voices that completed first. If
an adapter has Saved Voices but no presets, preset completion clears `voiceId`, and Saved Voice
completion appends without selecting a voice; generation remains disabled
(`web/src/features/studio/StudioPage.tsx:269-283`).

**Remediation:** keep preset and Saved Voice state separate or merge one `Promise.allSettled` result,
then deterministically preserve a valid selection or select the first merged voice. Add both response
orders and the saved-only adapter case to tests.

### A-04 — Linux and Windows do not run Python or installed-artifact tests

The deterministic Python suite is macOS-only (`.github/workflows/ci.yml:105-110`). All matrix jobs
build distributions, but the final CLI commands execute the checkout environment rather than the
new wheels (`.github/workflows/ci.yml:112-120`). Windows/Linux filesystem, subprocess, SQLite,
lifecycle, metadata, entry-point, and installed-Web defects can therefore pass the matrix.

**Remediation:** run the platform-safe Python suite and install the just-built release set into clean
temporary environments on every OS. Keep truly hardware-specific tests separate.

### A-05 — Standalone Worker suites are absent from CI

Root pytest collects only `tests`, protocol tests, and Worker-SDK tests (`pyproject.toml:48-50`). CI
does not run the locked fake, VieNeu, or OpenAI-compatible Worker projects, despite the documented
commands in `docs/development.md:28-38`. This omits provider credential/redirect/SSRF/cancellation
tests such as `workers/openai_compatible/tests/test_http.py:84-239`.

**Remediation:** add frozen per-project test steps for all three Workers and document the
OpenAI-compatible Worker command alongside fake and VieNeu.

### A-06 — Published packages do not constrain internal compatibility

Core, Worker SDK, and both production Workers depend on bare `tts-studio-protocol` and/or
`tts-studio-worker-sdk` names (`pyproject.toml:11-20`;
`packages/worker-sdk/pyproject.toml:10-13`; `workers/vieneu/pyproject.toml:10-15`;
`workers/openai_compatible/pyproject.toml:10-13`). Repository locks do not protect downstream wheel
resolution. An older Core can import a newer generated module—or vice versa—before protocol
negotiation can reject it.

**Remediation:** version the release set coherently and declare tested compatibility ranges or exact
same-release constraints for internal packages. Add a metadata test that installs each supported
combination from only built artifacts.

## Medium findings

### M-01 — Audio-retention opt-out still durably retains full input text

Every job stores `text` in SQLite independently of `retain_artifact`
(`src/tts_studio/generation/registry.py:95-111`; schema at
`src/tts_studio/storage/migrations/004_generation.sql:11-30`) and returns it publicly
(`src/tts_studio/server/routes/generation.py:217-225`). This can surprise users who interpret the
one-off/no-retention UI as a privacy mode.

**Remediation:** either disclose text retention precisely or redact/delete opted-out job text after
terminal completion, with a migration and API/UI tests.

### M-02 — Native generation accepts and persists unbounded text

`GenerationRequest.text` has no maximum (`src/tts_studio/server/routes/generation.py:143-149`), and
Core validation only rejects empty/NUL content (`src/tts_studio/generation/service.py:1828-1830`). A
large request is inserted before the Worker boundary applies its 10,000-character limit
(`packages/worker-sdk/src/tts_studio_worker_sdk/limits.py:3`).

**Remediation:** enforce the shared limit in the HTTP model and Core service before job creation,
and add an overall request-body limit.

### M-03 — One global gate defeats engine/replica concurrency

GenerationService creates one `_replica_gate` (`src/tts_studio/generation/service.py:202`) and holds
it across complete generation and alignment operations (`src/tts_studio/generation/service.py:1045-1049`,
`1113-1118`). This serializes unrelated engines and all configured replicas before the supervisor's
free-replica selection can help.

**Remediation:** rely on supervisor leases for capacity; retain only narrowly scoped per-model
lifecycle locks where load/unload ordering requires them.

### M-04 — UI actions are not consistently capability-gated

Studio gates prosody options but enables Generate from model/voice/busy state and Cancel from job
state (`web/src/features/studio/StudioPage.tsx:196-203`, `283-288`). Voice Preview also lacks a
runtime-capability check (`web/src/features/voices/VoicesPage.tsx:15-21`). Unsupported adapters are
offered controls that Core must reject.

**Remediation:** centralize model-to-Worker capability lookup and give Generate, Cancel, Preview,
prosody, and cues explicit disabled reasons.

### M-05 — Browser end-to-end tests are never run in CI

Playwright is configured and exposed as `test:browser` (`web/package.json:9-10`), but CI runs only
Vitest, TypeScript, locale parity, and build (`.github/workflows/ci.yml:81-103`). Real-browser focus,
routing, audio, responsive layout, and request behavior are not gated.

**Remediation:** add a Chromium job on Linux and retain traces/screenshots on failure.

### M-06 — The primary engine has no automated real-model release gate

The hardware test is deliberately opt-in (`docs/development.md:217-231`; `docs/operations.md:92-99`)
and no workflow runs it. A fixture-green release can still break against the pinned model/SDK/codec.

**Remediation:** run the cache-pinned hardware smoke on a controlled scheduled or release runner and
require a recent successful result for release candidates.

### M-07 — GitHub Actions are pinned to mutable tags

Checkout, Python, uv, pnpm, and Node setup actions use version tags
(`.github/workflows/ci.yml:33-65`). A moved or compromised tag changes executed CI code without a
repository diff.

**Remediation:** pin reviewed full commit SHAs and automate update pull requests.

### M-08 — Known cancellation, retry, and Windows containment gaps remain

The operational contract acknowledges that Windows cannot durably contain descendants after Core
loss and that a native SDK call may continue after user-visible cancellation; native Generation
retry is also absent (`docs/operations.md:68`, `141-150`).

**Remediation:** use Windows Job Objects, add a hard process-termination operation for stuck native
calls, and design an idempotent native Generation retry operation.

### M-09 — Strict mypy currently accepts 26 known errors

The exact-baseline gate prevents drift but still treats the 26 diagnostics in
`scripts/mypy-baseline.txt` as success (`docs/development.md:61-70`). Errors remain in generation,
references, runtime, routes, and distribution code, and VieNeu source is excluded
(`pyproject.toml:60-67`).

**Remediation:** burn down the baseline by module and add zero-error gates for newly cleaned modules;
type-check lightweight Worker code with narrow third-party exclusions.

### M-10 — CI duplicates expensive work and does not cancel superseded runs

Unrestricted `push` plus `pull_request` triggers can duplicate same-repository PR runs, there is no
workflow concurrency group, and frontend/static/build work repeats across three OS jobs
(`.github/workflows/ci.yml:3-30`, `81-115`). This directly increases hosted-runner consumption.

**Remediation:** restrict push to the default branch/tags, add branch/PR concurrency with
`cancel-in-progress`, and move platform-neutral checks to one Linux job before the OS matrix.

### M-11 — GitHub code scanning and secret scanning are disabled

The repository API reports both features disabled. Dependabot and local pattern scans are useful but
do not replace semantic code scanning or historical/push-time secret detection.

**Remediation:** CodeQL now analyzes Python and JavaScript/TypeScript in the pinned,
schedule/manual/default-branch `.github/workflows/security.yml`. The repository-local,
network-free scanner configured by `security/secret-scan.toml` instead runs in the existing Linux
assurance job, retaining pull-request coverage without adding a runner or exceeding its documented
minute ceiling. Repository administrators must also enable GitHub code and secret scanning (plus
push protection where available) when their plan supports them. `docs/operations.md` assigns alert
triage to the security maintainer.

### M-12 — The local-first Web UI makes external Google Fonts requests

`web/index.html:8-12` preconnects to Google and loads font CSS on every page load. This discloses
client network metadata and makes appearance network-dependent.

**Remediation:** bundle/subset the fonts into the distribution or use system fonts.

### M-13 — README speech examples use identifiers the CLI cannot resolve

README passes `--model vieneu` and display-style `--voice "Adam"` (`README.md:47-52`), while `tts
speak` forwards both values as model/voice IDs (`src/tts_studio/cli.py:622-649`). Installed models
use generated IDs, so the documented quick path can return `model_not_found` or `voice_not_found`.

**Remediation:** show how to obtain and pass IDs from model download/list and voice list output, or
add an explicitly specified alias-resolution feature.

### M-14 — Changing locale does not update the document language

The shell is permanently `<html lang="en">` (`web/index.html:2`), while i18n supports `en-US` and
`vi-VN` without updating `document.documentElement.lang` (`web/src/i18n.ts:15-31`). Screen readers
and browser language services therefore misclassify Vietnamese content.

**Remediation:** set `lang` after initialization and on `languageChanged`, with a browser/unit test.

## Low findings

### L-01 — Small buttons undershoot the common touch-target baseline

`.btn-small` is 36 px high (`web/src/styles.css:887-891`) and is used by frequent Voice Preview and
provider actions. Preserve a compact visual while providing at least a 44 px hit area.

### L-02 — Coverage is unmeasured and unenforced

Neither root Python nor Vitest config defines coverage dependencies or thresholds
(`pyproject.toml:25-36`; `web/vite.config.ts:18-22`). Establish a measured baseline and prevent
regression rather than selecting an arbitrary high threshold.

### L-03 — The CI “formatting and lint” step does not check formatting

The step runs only `ruff check .` (`.github/workflows/ci.yml:90-91`). Add `ruff format --check .` or
rename the step.

### L-04 — Public release metadata has no license

There is no tracked `LICENSE` file or `license` project metadata (`pyproject.toml:5-20`). Choose and
declare terms before public distribution.

### L-05 — Documentation contains status contradictions

Development says configurable replicas are implemented (`docs/development.md:7-11`), while
operations says VieNeu replicas remain deferred (`docs/operations.md:63-66`). The documentation
index also refers to a roadmap that is not present (`docs/README.md:23-24`). Align status language
with the supported runtime and remove or restore the roadmap reference.

## Strengths and clean checks

- FastAPI remains the sole public server and SQLite writer; Workers stay isolated behind
  authenticated loopback gRPC and a versioned protocol.
- Managed path, identity, symlink, atomic-promotion, artifact integrity, and migration-continuity
  defenses are unusually thorough.
- Provider egress rejects unsafe schemes, credentials, query/fragment targets, private/reserved DNS
  results, and cross-origin authorization forwarding.
- Worker tokens are per-launch, passed by protected file, compared in constant time, and removed at
  startup; parent environment inheritance is allowlisted.
- PCM event order, format, size, backpressure, hashes, and publication are validated by Core.
- The API has correlation-aware stable error envelopes; OpenAI input rejects unknown fields and
  bounds input and encoded responses.
- The UI has visible focus, route focus management, reduced-motion support, localized error mapping,
  confirmation for destructive History actions, and partial-failure preservation.
- All Python lockfiles passed `uv lock --check`; pnpm's frozen lock is coherent.
- Protocol and OpenAPI generated artifacts passed drift checks.
- `pnpm audit` and GitHub Dependabot reported zero known/open dependency advisories.
- The mypy baseline matched exactly, so type debt did not silently increase.
- Latest CI for this snapshot passed: Linux 69 seconds, Windows 117 seconds, macOS 257 seconds.
- No tracked virtual environment, runtime data, build output, obvious credential file, or recognized
  secret pattern was found.

## Recommended remediation order

1. Close the default HTTP trust boundary (A-01) and decide the authenticated-browser contract
   (A-02).
2. Fix Saved Voice merging/selection (A-03), bound native text (M-02), and clarify or change opted-out
   text retention (M-01).
3. Restore release assurance: Worker suites (A-05), Python and installed-wheel smoke on every OS
   (A-04), browser E2E (M-05), and CI concurrency/cost controls (M-10).
4. Version internal packages as one compatible release set (A-06).
5. Remove global synthesis serialization (M-03) without weakening model lifecycle safety.
6. Address the remaining medium operational, accessibility, privacy, and type-debt items, then the
   low documentation/metadata items.

## Approved remediation design

The remediation preserves the Core as the sole public HTTP server and SQLite writer and retains the
versioned, private gRPC Worker boundary. Browser authentication will be an explicit in-memory bearer
session: static assets remain readable, but the Web UI prompts for a token when Core requires one,
adds it to API requests, fetches protected media into object URLs, and consumes server events through
an authenticated streaming request. Loopback operation will reject untrusted Host headers and
cross-origin browser mutations without disrupting native clients that have no Origin header.

Generation requests will share one early text-size limit across public entry points. Requests that
opt out of retention will have their text removed after reaching a terminal state, while retained
history remains unchanged. Scheduling will use supervisor capacity instead of a global generation
gate. Native cancellation will fail closed by terminating an uncooperative Worker process, with a
new explicit retry path only for retryable terminal jobs.

The release workflow will run platform-neutral checks once on Linux and keep platform-specific
smoke coverage in a bounded matrix. It will run deterministic Core and Worker tests, install built
artifacts into clean environments, run Chromium browser coverage, cancel superseded runs, and use
immutable action revisions. A scheduled, self-hosted hardware workflow will be supplied for the
real-model gate. Code scanning and secret detection will be represented by repository workflows and
documented repository settings; availability remains dependent on the repository's GitHub plan.
