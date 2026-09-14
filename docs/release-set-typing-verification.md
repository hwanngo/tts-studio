# Release-set compatibility and strict typing verification

Date: 2026-09-14. Scope: release-set compatibility and strict typing.

## Result

Core, generated protocol, Worker SDK, fake Worker, OpenAI-compatible Worker, and VieNeu Worker
remain one `0.1.0` release set. All eight internal runtime dependency edges now require exactly
that release, as does Core's development-only Worker SDK dependency. No arbitrary package version
bump or protobuf schema change was introduced.

The root `pyproject.toml` supplies the authoritative version. The new
`scripts/sync_release_versions.py` synchronizes the derived static versions and internal dependency
constraints; `--check` rejects drift. CI and the distribution builder run that check. A command
test changes only the root version in a temporary source fixture, verifies rejection, synchronizes
the metadata, and builds a Worker SDK wheel with the expected version and protocol constraint.
Core's `__version__` reads installed distribution metadata, removing a duplicate source constant.
The normal `uv_build` backend and independent Worker project environments remain intact.

Strict mypy reports **zero diagnostics across all 97 owned source files**. The old acceptance
script, 26-diagnostic baseline, and baseline-specific tests were deleted. CI and `just mypy` now
run mypy directly; any diagnostic fails the command. Historical audit/verification documents that
describe the old baseline remain historical records, not executable acceptance gates.

## TDD evidence

The package-install test first installed the existing Core wheel with a separately built synthetic
`0.0.0` protocol wheel. The installer accepted this unsupported mixture, causing the new test to
fail. After exact internal constraints were added, the installer rejected mixed wheels at all eight
dependency edges. Tests require a diagnostic naming the consumer and its exact internal dependency
constraint, so an unrelated missing dependency does not count as successful rejection.

The typed OpenAI-compatible Worker was missing `Align`. A real-gRPC test first observed
`UNIMPLEMENTED` for an unauthenticated request. The implementation now authenticates the call,
returns `UNAUTHENTICATED` without a token, and returns a non-retryable `alignment_unavailable`
response for authenticated calls. The adapter does not advertise alignment capability.

The original type-check gate was independently run before changes: 26 errors across the prior
78-file scope. Corrections include concrete datetime/SQLite/reference metadata types, optional
platform module types, typed artifact streams, separately named PCM payload and subscriber values,
proved non-None reference paths/services, and the supported Python 3.14 queue shutdown exception.
All existing baseline diagnostics were resolved rather than recaptured or suppressed.

Expanding the gate to Worker sources exposed additional annotations and SDK-boundary issues.
Worker RPC requests, contexts, and responses now carry generated types; blocking repository calls
preserve their return types; download progress uses explicit protobuf fields; HTTP request JSON
and returned WAV bytes have distinct types; and the numerical boundary returns concrete bytes.

## Deliberate type boundaries

No owned source directory is excluded. Only third-party `numpy`, `soundfile`, `huggingface_hub`,
and `vieneu` module trees are skipped when following imports. These libraries remain exclusively
in VieNeu's locked runtime environment and are not added to Core for type checking.

Two new line-scoped exceptions document third-party limitations: VieNeu's runtime-selected SDK
base class has no static type, and the installed types-grpcio stub declares done callbacks as a
nominal class instead of a structural callback protocol. The exception codes are restricted to
those individual expressions. Neither exception accepts any of the original 26 Core diagnostics.
The strict check also runs in a clean temporary environment with mypy and built Core/protocol/SDK
wheels; it does not depend on editable internal package installations or an incremental cache.

## Verification

Commands were run with Python 3.14.7 on macOS ARM64.

| Check | Result |
| --- | --- |
| `uv run --python 3.14 mypy --platform linux --no-incremental` | Zero diagnostics, 97 files |
| `uv run --python 3.14 pytest tests/packaging/test_release_set.py -q` | 14 passed, including isolated mypy and artifact-only installs |
| Focused references/generation/server/Workers/packaging/runtime suite below | 501 passed, 4 skipped |
| `uv run --project workers/fake --frozen pytest workers/fake/tests -q` | 27 passed |
| `uv run --project workers/vieneu --frozen pytest workers/vieneu/tests -q` | 110 passed |
| `uv run --python 3.14 pytest tests/packaging/test_wheel.py -q -rs` | Available checks completed; final VieNeu offline dependency gate skipped |
| `uv run --python 3.14 ruff check .` | Passed |
| Protocol and OpenAPI drift checks | Passed; generated files unchanged |
| Root `uv lock --check` and all three Worker `uv lock` commands | Passed; no lockfile changes required |
| `uv run python scripts/sync_release_versions.py --check` | Passed |
| `git diff --check` | Passed |

The focused suite command was:

```console
uv run --python 3.14 pytest tests/references tests/generation tests/server tests/workers tests/packaging/test_release_set.py tests/packaging/test_build_distribution.py tests/test_runtime.py tests/test_runtime_upgrade.py -q
```

It ran before the last release-command regression was added; the final 14-case release-set suite
was then rerun independently. The existing wheel smoke invokes the production distribution builder,
so the Web build and all release artifacts were exercised. Its available Core and fake Worker
install/import/server checks passed before the final explicit skip for the missing cached
`vieneu==3.6.3` dependency. No model download, third-party network fetch, hardware inference, or
native Windows/Linux verification is claimed.

## Remaining limits

Full VieNeu installation from the release artifacts still requires a complete third-party wheel
cache. The package metadata and both VieNeu mixed-release rejection cases are covered without that
cache. Matching release installs verify Core and fake/OpenAI Workers in separate environments,
run `uv pip check`, inspect installed distribution versions, and import their actual application or
service modules using isolated Python execution.

The package release version is independent of protobuf major/minor negotiation and adapter runtime
capabilities. Core remains the sole public server/SQLite writer, and no engine dependency enters
the Core wheel's runtime dependency graph. Complete cross-platform release verification remains an
operational prerequisite until hosted platform runs are observed.
