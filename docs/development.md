# Development and testing

## Tooling

Python dependencies, tools, and Worker environments use `uv`. Frontend dependencies and workspace commands use `pnpm`. The React production build is embedded into the Python distribution.

The foundation vertical slice, model-management workflow, generation/history, Saved Voices,
configurable replicas, VieNeu runtime/acquisition, remote providers, UI, CLI, detached/service
lifecycle, and Worker runtime upgrade/rollback are implemented. Cross-platform release smoke runs
frontend tests/types/build, locale parity, Python tests/Ruff/mypy, generated-artifact drift checks,
and distribution checks on Linux x64, macOS ARM64, and Windows x64.
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
UV_PYTHON=3.14 uv run --project workers/vieneu --frozen pytest workers/vieneu/tests -q
UV_PYTHON=3.14 uv run python scripts/check_protocol.py
UV_PYTHON=3.14 uv run python scripts/check_openapi.py
UV_PYTHON=3.14 uv run python scripts/build_distribution.py
UV_PYTHON=3.14 uv run pytest tests/packaging/test_build_distribution.py tests/packaging/test_wheel.py -q
UV_PYTHON=3.14 uv run python scripts/check_mypy_baseline.py
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

## Audit remediation status and backlog

The canonical security and reliability backlog is maintained in
[`docs/operations.md`](operations.md#audit-remediation-backlog). The completed remediation state
now includes managed-directory and identity-bound file hardening, Worker readiness and orphan
handling, bounded crash replacement, bounded direct inputs, provider egress enforcement, bounded
cancellation with quarantine, migration continuity checks, protocol compatibility validation, and
Core-over-real-gRPC alignment coverage. Remaining work is the stronger native-call termination
operation and a native Generation retry operation; ordinary failed/cancelled Generations remain
non-retryable, while startup-recovery failures remain retryable. This section intentionally does
not duplicate the backlog.

The release-smoke workflow runs frontend unit/type/build checks, locale parity, Ruff,
protocol/OpenAPI drift checks, deterministic tests, and distribution build after locked dependency
installation. Its mypy step runs `scripts/check_mypy_baseline.py`, which prints the known
26-diagnostic baseline and fails on any added, removed, or changed diagnostic; no blanket
suppression or `continue-on-error` is used. The baseline covers analyzable Core/protocol/Worker-SDK
trees, while the VieNeu Worker source is excluded because its locked third-party SDK and
numerical/audio dependencies do not ship analyzable stubs. After intentionally fixing or accepting
type debt, maintain the exact baseline with `uv run python scripts/check_mypy_baseline.py --update`,
review the diff, and commit it with the code change. Generated protobuf stubs retain a narrow
per-file Ruff exception for generator-owned `UP040` aliases. Migration 012 and end-to-end
Core-over-gRPC alignment coverage have direct assertions.

The current full-suite baseline is green: the configured
`UV_PYTHON=3.14 uv run --project . --python 3.14 pytest -q --tb=no` gate completes with
`781 passed, 6 skipped`. The skips remain limited to explicitly optional hardware or unavailable
offline dependency closures. Worker startup has a regression test for a readiness file that appears
immediately after an initial open miss, and the installed-wheel smoke installs the protocol wheel
built by that same smoke so a stale same-version cache entry cannot mask protocol drift. The storage,
Worker containment, provider, alignment, migration, protocol, OpenAPI, frontend, packaging-build,
and baseline-aware mypy checks remain separately required.

The real-model gate is separate and never downloads prerequisites:

```console
TTS_STUDIO_RUN_VIENEU_HARDWARE=1 TTS_STUDIO_VIENEU_MODEL_CACHE=/absolute/path/to/vieneu-cache UV_PYTHON=3.14 uv run --python 3.14 pytest tests/integration/test_vieneu_hardware.py -q -s
```

Run the Python, protocol, lint, and type checks with:

```console
uv run pytest -v
uv run ruff check .
uv run python scripts/check_mypy_baseline.py
uv run python scripts/check_protocol.py
uv run python scripts/check_openapi.py
```

The supported runtime is Python 3.14. Run the explicit 3.14 gate when the active interpreter is
not selected automatically:

```console
UV_PYTHON=3.14 uv run --python 3.14 ruff check . --target-version py314
UV_PYTHON=3.14 uv run python scripts/check_mypy_baseline.py
```

The baseline checker runs mypy in the selected Python 3.14 environment, sorts diagnostics for a
deterministic comparison, prints and matches the known diagnostics in
`scripts/mypy-baseline.txt`, and fails when diagnostics drift.

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

The cross-platform release-smoke definition is `.github/workflows/ci.yml`. It
uses Python 3.14, locked `uv`/Worker/Web dependencies, runs protocol/OpenAPI drift checks, excludes
credentialed or hardware-only suites, builds the distribution, and checks runtime/service CLI help
on Linux x64, macOS ARM64, and Windows x64. Migration failure recovery is covered by
`tests/storage/test_migrations.py`.

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

The deterministic contract and relevant Core/Worker suites are run with:

```console
UV_PYTHON=3.14 uv run --python 3.14 pytest tests/references tests/generation tests/server tests/openapi tests/workers tests/integration/test_reference_cloning.py tests/integration/test_generation.py tests/integration/test_vieneu_worker_contract.py -q
UV_PYTHON=3.14 uv run --project workers/fake --frozen pytest workers/fake/tests -q
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
the fake Worker from its packaged wheel in a separate environment. The installed workflow
proves that the Core accepts the Worker's opaque activated Model Installation ID, owns every WAV
artifact path, and recovers private generation staging before model staging cleanup. Background
upgrade/rollback safety, background control, service definitions, and `uv tool install` smoke
coverage are implemented; macOS ARM64, Windows x64, and Linux x64 CI remain.
Apple Silicon smoke benchmarks will record time-to-first-audio and real-time factor as trends, not
brittle hard gates.

Failure-path tests cover schema migration, interrupted download, insufficient disk, corrupt manifests, port collision, stale run records, missing environment variables, Worker timeout, restart limits, and generated login-service definitions.

## Completion standard

A change is complete when its focused tests pass, the relevant stable interface remains compatible or is deliberately versioned, generated artifacts are current, packaging risk is smoke-tested when applicable, and the focused documentation reflects changed user or operator behavior.
