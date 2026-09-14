# Final audit-remediation verification

Verified on 2026-09-14 against `065ea94`, with the full branch compared to `d54c001` (`main`).
Environment: macOS ARM64, Python 3.14.7, uv 0.12.13, pnpm 11.25.0. CI pins uv 0.12.10;
that exact uv executable and hosted Linux/macOS/Windows runs were not exercised here.

## Local results

| Gate | Observed result |
| --- | --- |
| Core, protocol package, and Worker SDK tests; exact CI coverage selection | 857 passed, 6 skipped, 28 warnings; 158.00 seconds |
| Core statement coverage | 8,403 / 10,059 = 83.54%; configured 83% floor passed |
| Platform-safe Core selection from CI | 143 passed, 1 native-Windows skip; 3.40 seconds |
| Fake Worker in its locked environment | 27 passed |
| OpenAI-compatible Worker in its locked environment | 8 passed |
| VieNeu Worker in its locked environment | 110 passed |
| Web unit tests | 130 passed in 14 files |
| Web types and locale parity | Passed |
| Chromium with `CI=true`, two workers | 11 passed; 6.7 seconds |
| Ruff formatting and lint | Passed; 220 handwritten Python files already formatted |
| Strict mypy, Linux static platform, no incremental cache | Zero diagnostics across 97 files |
| Protocol and OpenAPI drift; release-set metadata | Passed; generated files unchanged by verification |
| Root and all three Worker `uv lock --check` commands | Passed; no lock changes |
| Frozen pnpm installation | Passed; no lock changes |
| Production Web and distribution build | Passed; six wheels plus Core source archive, including the explicit test adapter |
| Clean installed-artifact CLI/dependency/API/Web smoke | Passed |
| Deeper installed Core/fake-Worker and `uv tool` workflows | 2 passed, 1 partial-test offline VieNeu skip; 17.48 seconds |
| Repository-local secret scan | Passed |
| All three workflows, actionlint v1.7.7 | Passed |
| Three workflow YAML files, Dependabot, actionlint configuration | Parsed successfully |
| Eight unique Action release tags | Each resolved through the upstream GitHub commit API to its checked-in 40-character SHA; each reported a verified signature |
| Local documentation links | All 46 existing local links and heading anchors across 22 tracked documents resolved before this report was added |
| Complete branch whitespace check | `git diff --check main...HEAD` passed |

The coverage command was:

```console
UV_PYTHON=3.14 uv run --frozen pytest -q -rs -m 'not e2e and not hardware and not network' --ignore=tests/packaging/test_wheel.py --ignore=tests/packaging/test_tool_install.py --ignore=tests/integration/test_phase3_generation_artifact.py --cov=tts_studio --cov-report=term:skip-covered --cov-report=xml
```

The production builder ran once with `--include-test-adapters`. The root lock was exported to
`dist/core-requirements.txt`, and `scripts/ci_artifact_smoke.py --dist-dir dist` installed the
result outside the checkout. The three excluded distribution fixtures then ran with
`TTS_STUDIO_TEST_DIST_DIR` pointing at that same artifact set. The coverage run includes the
release-set metadata tests, artifact-only matching-release installs and mixed-release rejection,
secret-scanner tests, and safe hardware-cache fixture tests.

## Explicit skips and limits

The six Core skips are three generation identity-bound-unlink tests unavailable on this macOS
platform, two opt-in real-VieNeu hardware tests, and the native Windows Job Object test. The
platform-safe selection repeats the same native-Windows skip. They are not passing native-platform
or real-model evidence.

The deeper wheel test completed its available Core/HTTP/Web assertions before its optional VieNeu
installation skipped because the offline cache lacked `vieneu==3.6.3`. The separate mandatory
artifact smoke and the two other installed workflows passed. No complete offline VieNeu
third-party dependency installation is claimed.

The provisioned hardware preflight was also invoked directly:

```console
uv run --frozen python scripts/check_vieneu_hardware_cache.py --cache /var/lib/tts-studio/vieneu-cache
```

It exited 1 because that required cache directory is absent locally. This is expected missing
runner provisioning, not a successful hardware gate. Fixture tests cover accepted pinned caches
and rejection of malformed provenance, missing files, and symbolic links. No model download or
hardware inference was performed.

The Core run emitted 28 warnings, including the previously recorded unclosed SQLite-connection
ResourceWarnings. Browser output included the Node color-environment warning. These did not fail
the gates; this report does not describe a warning-free run. Chromium used the existing matching
Playwright cache; the browser downloader was not rerun.

## Full-branch invariant review

The review covered the complete 188-file branch inventory, focused behavioral diffs, dependency
metadata, generated-contract differences, workflows, and current contracts. A Python AST inventory
identified 72 changed files with identical ASTs, 44 with changed ASTs, 12 additions, and three
removals; typing changes also count as AST changes. Formatting-only files were separated from
behavioral changes before reviewing the Core/Worker boundaries.

FastAPI remains the public server and Core remains the SQLite writer. An import-boundary scan found
no engine/adapter imports in Core and no Core or SQLite imports in Worker source. The versioned
protobuf schema and committed protocol runtime/stub files are unchanged across the branch.
Workers retain their separate locked environments, runtime capability admission, and authenticated
gRPC calls. Windows containment's trusted standard-library gate launches the same isolated Worker
command after Job assignment; it does not import engine code into Core.

Generation admission, retry, terminal text redaction, artifact ownership, and cancellation remain
Core application behavior. Migration 013 adds the unique retry-successor relationship and redacts
existing non-retained terminal text. The Web session keeps its token in memory and routes JSON,
events, PCM, media, and downloads through authenticated HTTP. Capability controls consume observed
Worker facts. Managed-data defaults, adapter-owned Hugging Face compatibility, environment-only
provider secrets, and exclusion of VieNeu's deprecated style parameter are preserved.

The focused HTTP, protocol, operations, development, and audit documents describe these changes.
No new interface, invariant, source correction, or lock update was required by the initial local
pass at `065ea94`. The subsequent review correction is recorded below.

## Hardware-gate review correction

A subsequent full-branch review found that the skipped hardware script called `anext()` directly
on a gRPC `UnaryStreamCall`, which is async iterable but is not itself an async iterator. After
using its iterator, local gRPC cancellation raises `asyncio.CancelledError`, which also needed
explicit handling. The script now retains the iterator and verifies local call cancellation;
structured cancellation events and RPC errors with status `CANCELLED` remain supported.

A new no-hardware regression executes the actual embedded cancellation block against an
in-process real gRPC server. It first failed with the reported `TypeError`, then reproduced the
uncaught `CancelledError` after the iterator-only correction. Both failures are now covered
without importing VieNeu or using a cache. A syntax check alone did not detect these API errors.

Review also found that runtime loading requires `cloning/denoiser.onnx` and
`cloning/speaker_encoder.onnx` below `models/vieneu`, but the preflight did not inspect them.
The preflight now requires both safe regular files and an unlinked cloning directory. New tests
first demonstrated acceptance of either missing file and of a linked cloning directory, then
passed after the validator correction. The operations and development contracts now identify
these required assets. These changes affect verification code only; real-model and hosted
execution remain prerequisites and the earlier full-suite measurements retain their stated scope.

Follow-up verification: the hardware-script, cache, opt-in hardware, and VieNeu Core/Worker
contract selection passed with **16 passed, 2 opt-in skips**; the isolated VieNeu Worker suite
passed **110 tests**. Repository-wide Ruff format/lint, strict mypy (97 files), secret scanning,
all three workflow actionlint/YAML checks, and whitespace checks passed. The fixed runner cache
path still fails preflight because it is absent locally. The full Core coverage/build/browser
gate was not rerun for this verification-only correction.

## Release handoff prerequisites

External release prerequisites are successful hosted platform CI, a successful hosted CodeQL run and
SARIF upload, a provisioned controlled self-hosted runner with a successful offline real-model run,
and administrator-enabled code scanning, secret scanning, and push protection where supported.
None was run or changed here. Native Windows containment and Linux-specific unlink behavior still
require the applicable runner. The first hosted Linux coverage run must establish that platform's
result against the same 83% floor. Local Action provenance checks establish pin identity, not hosted
workflow success or a full third-party source audit.
