# Development and testing

## Tooling

Python dependencies, tools, and Worker environments use `uv`. Frontend dependencies and workspace commands use `pnpm`. The React production build is embedded into the Python distribution.

The foundation vertical slice, model-management workflow, generation/history, Saved Voices,
configurable replicas, VieNeu runtime/acquisition, remote providers, UI, CLI, detached/service
lifecycle, and Worker runtime upgrade/rollback are implemented. The checked-in CI definition
performs platform-neutral assurance once on Linux, then defines the same built-artifact and bounded
Core test selection for Linux x64 and macOS ARM64. Native Windows verification is a separately
provisioned operational check rather than part of hosted CI.
Exact commands belong here only after their configuration exists; `pyproject.toml`, package
scripts, and command help remain the executable source of truth.

## Verification

Run the current frontend checks and production build from the repository root:

```console
pnpm --dir web test -- --run
pnpm --dir web exec tsc --noEmit
pnpm --dir web build
```

The deterministic acquisition, authenticated Core/Worker, packaging, protocol, and
quality gate is:

```console
UV_PYTHON=3.14 uv run --python 3.14 pytest tests/models tests/storage tests/workers tests/generation tests/server tests/openapi tests/integration/test_model_management.py tests/integration/test_generation.py tests/integration/test_reference_cloning.py tests/integration/test_vieneu_preset_generation.py tests/integration/test_vieneu_worker_contract.py -q
UV_PYTHON=3.14 uv run --project workers/fake --frozen pytest workers/fake/tests -q
UV_PYTHON=3.14 uv run --project workers/openai_compatible --frozen pytest workers/openai_compatible/tests -q
UV_PYTHON=3.14 uv run --project workers/vieneu --frozen pytest workers/vieneu/tests -q
UV_PYTHON=3.14 uv run python scripts/check_protocol.py
UV_PYTHON=3.14 uv run python scripts/check_openapi.py
UV_PYTHON=3.14 uv run python scripts/build_distribution.py
UV_PYTHON=3.14 uv run pytest tests/packaging/test_build_distribution.py tests/packaging/test_wheel.py -q
UV_PYTHON=3.14 uv run mypy --platform linux
UV_PYTHON=3.14 uv run ruff check src/tts_studio/models src/tts_studio/workers src/tts_studio/generation src/tts_studio/server workers/vieneu/src workers/vieneu/tests tests/models tests/workers tests/generation tests/server tests/integration/test_vieneu_preset_generation.py
git diff --check
```

Alignment verification is included in the Core protocol, Worker, route, and regression suites.
The alignment contract uses the fake Worker for deterministic frame offsets and explicitly verifies
that VieNeu reports `alignment_unavailable`; no production Vietnamese aligner or model download is
part of this gate. JSON speech tests also verify bounded base64 WAV responses and artifact hash
integrity, while the default binary WAV path remains unaligned. The dedicated end-to-end
Core-over-real-gRPC alignment integration test is `tests/integration/test_alignment.py` and is run
with `uv run pytest tests/integration/test_alignment.py -q`.

## Assurance status

Implemented controls include managed-directory and identity-bound file hardening, Worker readiness
and orphan handling, bounded crash replacement, provider egress enforcement, cancellation
quarantine, migration continuity checks, protocol compatibility validation, input limits, atomic
terminal privacy redaction, idempotent Generation retry, and forced native-call termination through
supervised POSIX groups or Windows Job Objects. Hosted execution, runner provisioning, and GitHub
settings remain external release prerequisites; portable coverage does not replace a native Windows
execution.

The Linux assurance definition runs frontend unit/type/build checks, locale parity, Ruff lint and
`ruff format --check`, protocol/OpenAPI drift checks, deterministic Core coverage, all three
locked Worker suites, Chromium browser tests, and one release build. Its mypy step runs
`uv run mypy --platform linux` in strict mode and fails on any
diagnostic. All 97 source files in Core, protocol, Worker SDK, and the fake, OpenAI-compatible,
and VieNeu Workers are checked; the previous 26-error baseline and acceptance script are removed.
The only external import exclusions are VieNeu's `numpy`, `soundfile`, `huggingface_hub`, and
`vieneu` trees, which remain isolated in its locked Worker environment. Adapter source is never
excluded. Two line-scoped exceptions document third-party typing limitations: the runtime-selected
VieNeu SDK base class and types-grpcio's nominal done-callback class. Generated protobuf stubs retain a narrow
per-file Ruff exception for generator-owned `UP040` aliases. Migration 012 and end-to-end
Core-over-gRPC alignment coverage have direct assertions.

The earlier full-suite verification recorded the configured
`UV_PYTHON=3.14 uv run --project . --python 3.14 pytest -q --tb=no` gate completing with
`781 passed, 6 skipped`. The skips were limited to explicitly optional hardware or unavailable
offline dependency closures. Worker startup has a regression test for a readiness file that appears
immediately after an initial open miss, and the installed-wheel smoke installs the protocol wheel
built by that same smoke so a stale same-version cache entry cannot mask protocol drift. The storage,
Worker containment, provider, alignment, migration, protocol, OpenAPI, frontend, packaging-build,
and strict mypy checks remain separately required.

The real-model gate is separate and never downloads prerequisites:

```console
TTS_STUDIO_RUN_VIENEU_HARDWARE=1 TTS_STUDIO_VIENEU_MODEL_CACHE=/absolute/path/to/vieneu-cache UV_PYTHON=3.14 uv run --python 3.14 pytest tests/integration/test_vieneu_hardware.py -q -s
```

Run the Python, protocol, lint, and type checks with:

```console
uv run pytest -v
uv run ruff format --check .
uv run ruff check .
uv run mypy --platform linux
uv run python scripts/check_protocol.py
uv run python scripts/check_openapi.py
```

The supported runtime is Python 3.14. Run the explicit 3.14 gate when the active interpreter is
not selected automatically:

```console
UV_PYTHON=3.14 uv run --python 3.14 ruff check . --target-version py314
UV_PYTHON=3.14 uv run mypy --platform linux
```

Mypy targets are configured in `pyproject.toml`, so a bare `uv run mypy` checks all owned Python
source. CI fixes the static platform to Linux for deterministic cross-platform diagnostics;
platform runtime behavior remains covered by the distribution and lifecycle checks.

## Internal release compatibility

Core, protocol, Worker SDK, and all three Workers form one release set, currently `0.1.0`.
The root `pyproject.toml` version is authoritative. Every internal runtime dependency, plus Core's
development-only Worker SDK dependency, is constrained with `==` to that release. Worker
capabilities and protobuf major/minor negotiation remain separate runtime contracts; matching
package versions do not imply that an adapter supports any particular capability.

To change the release, edit only the root project version, synchronize the derived static package
metadata, and regenerate every affected lock using uv:

```console
uv run --no-sync python scripts/sync_release_versions.py
uv lock
uv lock --project workers/fake
uv lock --project workers/openai_compatible
uv lock --project workers/vieneu
uv run python scripts/sync_release_versions.py --check
uv run pytest tests/packaging/test_release_set.py -q
```

Static package metadata keeps standalone wheels and source builds compatible with the standard
`uv_build` backend. The production distribution builder and CI reject version drift before a
release build. Core reports its installed distribution version instead of a second source constant.
The release-set tests build every internal wheel and install the internal release set exclusively
from those artifacts in clean temporary environments outside the checkout. The strict type check
resolves its pinned public `mypy` tool normally, so it does not rely on a runner-specific package
cache. The tests verify Core/Worker isolation and `uv pip check`, and require the installer to
reject each of the eight internal dependency edges when supplied a different-release
protocol or Worker SDK wheel. The synthetic `0.0.0` fixture is not a published release.
VieNeu's full third-party installation remains the separately cache-aware installed-wheel gate.

The OpenAI-compatible Worker explicitly authenticates `Align` and returns
`alignment_unavailable`; it does not advertise alignment capability.
Focused results and known installation limits are recorded in the
[release-set and typing verification report](release-set-typing-verification.md).

Build the normal release artifacts—the root source archive and wheel plus the protocol, Worker
SDK, and VieNeu Worker wheels—with the production Web assets embedded:

```console
uv run python scripts/build_distribution.py
```

The builder compiles `web/`, refreshes the ignored `src/tts_studio/static/` staging directory,
and writes six normal release artifacts under `dist/`: the root source archive and wheel,
protocol wheel, Worker SDK wheel, OpenAI-compatible Worker wheel, and VieNeu Worker wheel. Test
artifact workflows add the fake Worker explicitly:

```console
uv run python scripts/build_distribution.py --include-test-adapters
```

The distribution smokes pass `--include-test-adapters` to the builder and
`tts serve --include-test-adapters` to the installed Core. They install Core and the fake Worker
into separate temporary environments, run `uv pip check`, verify generated-client and dependency
isolation, and exercise the public model and generation workflows. This opt-in is reserved for
deterministic test environments; normal Core startup and release artifacts exclude the fake adapter.
The installed distribution smoke
uses the same isolated layout with Python 3.14, then exercises runtime Voice discovery, Core-owned
generation, valid WAV metadata and SHA-256 delivery, CLI output, the embedded Studio bundle,
retention opt-out, cancellation cleanup, startup recovery, and model-in-use protection. Installed
Worker discovery validates uv environment and distribution provenance instead of trusting an ambient
same-named executable; both smokes check activated model files and their removal. End users
installing the root wheel do not need Node.js. The `uv tool install` smoke verifies the installed
Core and service command help when the offline dependency cache is complete; missing cached
dependencies produce an explicit skip. The latest local installed-wheel run completed the Core
isolation, import, server, and Web assertions, then skipped only the optional VieNeu environment
because `sea-g2p` was unavailable in the offline cache; the separate `uv tool install` smoke passed.

Runtime replacement is covered by `tests/test_runtime_upgrade.py` and the runtime CLI tests. It
uses one shared atomic generation pointer, strict artifact/provenance checks, and injected
command/health hooks for deterministic tests. The real release path uses offline `uv` installation
and candidate Worker verification; run it with a complete artifact directory and the required
cached third-party wheels.

The CI definition is `.github/workflows/ci.yml`. Its two platform smokes depend on Linux
assurance and download the same release artifact set; they do not install Node or rebuild Web
assets. Each runs platform-safe configuration, settings, client, migration, generation registry,
PCM, CLI, and Worker containment tests. Native Windows Job Object coverage is not a hosted CI gate.
`scripts/ci_artifact_smoke.py` installs the built Core and protocol wheels in a temporary environment
outside the checkout, using an export of the root lock for third-party dependencies. It verifies
dependency isolation, `uv pip check`, installed runtime/service help, real HTTP status, and the
embedded Web page and its JS/CSS. Missing dependencies fail this mandatory smoke instead of
skipping it. Windows paths and process cleanup live in the Python helper; the matrix's test command
is a YAML folded scalar valid in both PowerShell and Bash.

The three older installed-distribution fixtures accept `TTS_STUDIO_TEST_DIST_DIR` to reuse an
existing complete artifact set. The Linux assurance definition runs all three with this variable after the one build,
preserving the deep installed Core/fake-Worker generation, cancellation, recovery, and `uv tool`
checks. Without the variable, local invocations still build their own artifacts. The optional
offline VieNeu closure check can still skip when its third-party packages are not cached.

## CI cost, coverage, and dependency updates

The CI definition triggers for pull requests and for pushes only to `main`, `release/**` branches,
and `v*` tags. Ordinary feature-branch pushes do not duplicate PR runs. A concurrency group cancels superseded runs of
the same PR/ref. Linux assurance has a 25-minute timeout; each artifact smoke has 12 minutes.
The maximum runner allocation drops from three 30-minute jobs to 25 + 3 × 12 = 61 raw runner
minutes, and the expensive macOS/Windows runners no longer repeat lint, type, Web, browser, or
Worker checks. Actual billed savings depend on queueing, cache hits, and runner billing weights.

Core line coverage uses locked `pytest-cov` and includes every `tts_studio` module. It intentionally
does not claim Worker-process or browser coverage. The initial measured macOS/Python 3.14 baseline
is **83.54%** (8,403 / 10,059 statements), with 846 passing tests and six explicit skips. The
configured non-regression floor is **83%**, rounded down to a whole percent to allow a small
platform/timing margin; the first Linux hosted run must confirm this baseline. Raise the floor
alongside demonstrated increases; changing the source scope or lowering the floor requires an
explained review. The exact coverage invocation is:

```console
uv run --frozen pytest -q -m "not e2e and not hardware and not network" --ignore=tests/packaging/test_wheel.py --ignore=tests/packaging/test_tool_install.py --ignore=tests/integration/test_phase3_generation_artifact.py --cov=tts_studio --cov-report=term:skip-covered --cov-report=xml
```

Those three exclusions prevent repeated production Web builds during coverage collection; all
three fixtures run separately against built artifacts. Coverage XML is retained for seven days.
Ruff formatting covers handwritten Python throughout the repository. Only generated
`packages/protocol/src/tts_studio_protocol/engine/v1/*_pb2*.py`, the corresponding `*.pyi`, and
`src/tts_studio/generated/api.py` are excluded from formatting; their drift checks remain required.

Chromium verification uses the locked Playwright package and deterministic public-HTTP fixtures:

```console
pnpm --dir web exec playwright install --with-deps chromium
pnpm --dir web test:browser
```

CI uses two browser workers, rejects focused `.only` tests, does not reuse an existing server,
and retains traces and screenshots on failure plus the HTML report for seven days. Tests cover
generation, cancellation, runtime capability fixtures, history, Settings, theme/focus, and
destructive-action pending states. These browser tests mock HTTP responses; real Core behavior is
covered by Core integration and installed-artifact tests.

Every Action is pinned to a full commit SHA with a release comment. The pins were resolved from
the upstream release tags and GitHub's verified commit metadata on 2026-09-14; see
[CI verification](ci-verification.md) for the exact table. `.github/dependabot.yml` groups monthly
GitHub Actions updates into reviewable PRs with at most two open update PRs. There is no auto-merge
or privileged update workflow. Before merging an update, review the upstream release/change diff,
verify the tag's resolved SHA and commit provenance, preserve SHA pinning and minimal permissions,
and require normal CI. This follows GitHub's guidance on
[full-length commit pinning](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)
and [Dependabot Actions updates](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/keeping-your-actions-up-to-date-with-dependabot).

Validate workflow syntax locally when Go is available:

```console
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 .github/workflows/ci.yml
```

## Preview, options, and History verification

The current voice-preview and History increment is covered by the public route tests, generation-service
capability tests, Studio/Voices/History component tests, and the distribution build. The required
repository gate is:

```console
uv run python scripts/generate_protocol.py
uv run python scripts/check_protocol.py
uv run python scripts/check_openapi.py
uv run pytest tests/server tests/integration tests/workers -q
uv run --project workers/vieneu --frozen pytest workers/vieneu/tests -q
pnpm --dir web check
pnpm --dir web test -- --run
pnpm --dir web build
uv run python scripts/build_distribution.py
```

Browser verification should inspect `/voices`, `/`, and `/history` in light and dark themes. Check
preview loading, successful audio replay, preview errors, generation-control disabled states when
runtime capabilities are absent, inline-cue guidance, one-item deletion, confirmed multi-item
deletion, partial-failure messaging, responsive layout, no horizontal overflow, and preserved sidebar
navigation. A live preview may be unavailable in a fixture-only or restricted browser environment;
record that limitation separately from mocked component-test coverage.

## Module discipline

Keep modules deep and test them through their interfaces:

- Core application behavior is independent of FastAPI route functions.
- Persistence and managed-file behavior sit behind narrow interfaces and use injected roots/connections.
- Worker implementations depend on generated protocol types, not Core internals.
- Platform lifecycle adapters satisfy one foreground/background/service interface.
- React and CLI clients consume the generated public HTTP types.

Generated protobuf and OpenAPI artifacts require deterministic generation and a CI drift check.
After changing a public HTTP schema, run
`uv run python scripts/generate_openapi_client.py`; it refreshes both
`web/src/generated/api.ts` and `src/tts_studio/generated/api.py`. The React client and Python CLI
consume those generated types respectively, and `scripts/check_openapi.py` rejects drift in either
checked-in artifact. The focused client/API contract checks are:

```console
uv run pytest tests/client -q
pnpm --dir web test -- --run src/lib/api.test.ts
pnpm --dir web api:check
```

## Test layers

### Pure

State machines, registry compatibility, revision identity, scheduling, retention, path safety, error mapping, OpenAI translation, and platform service-file rendering run without network or models.

### Engine contract

Every Adapter runs the shared compliance suite over real gRPC: authenticated handshake, version negotiation, capabilities, health, validation, progress ordering, load/unload, voices, synthesis event order, cancellation, deadlines, and structured errors.

### Core integration

FastAPI tests use deterministic fake Workers over real gRPC. They cover native HTTP, OpenAI compatibility, SSE resumption, binary audio, persistence, authentication, crash recovery, and backpressure.

### VieNeu hardware

Routine CI uses the no-network contract fixture. The real-model gate is opt-in and cache-aware:

```console
TTS_STUDIO_RUN_VIENEU_HARDWARE=1 \
TTS_STUDIO_VIENEU_MODEL_CACHE=/absolute/path/to/vieneu-cache \
UV_PYTHON=3.14 uv run --python 3.14 pytest tests/integration/test_vieneu_hardware.py -q -s
```

The cache must contain `.vieneu-model-cache-marker` plus exact model and pinned codec provenance.
Missing prerequisites skip safely without downloads. When enabled, the gate covers both `int8`
and `fp32`, runtime preset discovery, streaming cancellation, 48 kHz output, unload/reload, and
offline reload. When the installed SDK exposes the verified reference signature, it also validates
and synthesizes from a local reference in both variants; no model or codec download is permitted.

The hardware-gate definition is a distinct scheduled/manual workflow, not a pull-request check. It
accepts only the required `tts-studio-vieneu-hardware` self-hosted Linux x64 runner and validates
the existing `/var/lib/tts-studio/vieneu-cache/.vieneu-model-cache-marker`, its JSON provenance,
and every required model/codec/cloning file before using the same offline command. Cloning includes
`models/vieneu/cloning/denoiser.onnx` and `models/vieneu/cloning/speaker_encoder.onnx`, both required
by runtime loading. The hardware script's cancellation block also runs over real in-process gRPC
in the deterministic script regression, without importing VieNeu or using model files.
Do not change that path,
the required runner labels, or the offline mode to make a developer machine convenient; provision
the controlled runner instead. A hosted runner and successful execution are operational prerequisites,
not repository-verifiable completion.

### Security scanning

Run the repository-local, deterministic secret scan before changing its rules or CI wiring:

```console
python3 scripts/scan_secrets.py
```

The scanner evaluates only tracked text files with the high-confidence patterns and byte limit in
`security/secret-scan.toml`; it does not download a scanner, submit source, or scan untracked
developer data. Keep rules narrow. For a genuine finding, rotate and remove the credential. A
false positive needs security-maintainer review and a targeted configuration change rather than a
blanket exclusion.

The Linux assurance definition invokes this check for pull requests without allocating another
runner or extending the documented 25-minute assurance ceiling. `.github/workflows/security.yml`
defines CodeQL analysis for Python and JavaScript/TypeScript only for a `main` push, weekly
schedule, or manual dispatch. A successful hosted run and uploaded results remain an operational
prerequisite. GitHub code scanning and secret scanning, including push protection where available,
remain repository-admin settings that depend on the GitHub plan; see the operational alert ownership and triage procedure in
[`docs/operations.md`](operations.md#github-security-operations).

The deterministic contract and relevant Core/Worker suites are run with:

```console
UV_PYTHON=3.14 uv run --python 3.14 pytest tests/references tests/generation tests/server tests/openapi tests/workers tests/integration/test_reference_cloning.py tests/integration/test_generation.py tests/integration/test_vieneu_worker_contract.py -q
UV_PYTHON=3.14 uv run --project workers/fake --frozen pytest workers/fake/tests -q
UV_PYTHON=3.14 uv run --project workers/openai_compatible --frozen pytest workers/openai_compatible/tests -q
UV_PYTHON=3.14 uv run --project workers/vieneu --frozen pytest workers/vieneu/tests -q
```

### User interfaces

React Testing Library covers critical accessible interactions. Browser tests cover first run, compatible and incompatible repositories, byte progress, generation, cancellation, history, missing provider variables, keyboard focus, and responsive layouts. CLI tests start an isolated real Core and use only public HTTP behavior.

### Distribution and platforms

Current packaging tests explicitly build the root, protocol, Worker SDK, fake-adapter, and VieNeu
Worker artifacts for test isolation, install Core and Workers into separate isolated environments
from the artifact set, verify embedded assets,
dependency closure, Worker generated RPC presence, and Core-to-Worker import isolation, and exercise
foreground server lifecycle. The artifact smokes also provision
the fake Worker from its packaged wheel in a separate environment. The installed artifact smoke
proves that the Core accepts the Worker's opaque activated Model Installation ID, owns every WAV
artifact path, and recovers private generation staging before model staging cleanup. Background
upgrade/rollback safety, background control, service definitions, and `uv tool install` smoke
coverage are implemented. The hosted platform smoke definitions target macOS ARM64 and Linux x64;
native Windows verification remains an operational prerequisite.
Apple Silicon smoke benchmarks will record time-to-first-audio and real-time factor as trends, not
brittle hard gates.

Failure-path tests cover schema migration, interrupted download, insufficient disk, corrupt manifests, port collision, stale run records, missing environment variables, Worker timeout, restart limits, and generated login-service definitions.

## Completion standard

A change is complete when its focused tests pass, the relevant stable interface remains compatible or is deliberately versioned, generated artifacts are current, packaging risk is smoke-tested when applicable, and the focused documentation reflects changed user or operator behavior.
