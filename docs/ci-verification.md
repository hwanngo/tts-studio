# CI assurance verification

Verified locally on 2026-09-14, macOS ARM64, Python 3.14.7. Hosted Linux and macOS execution, plus
separately provisioned native Windows verification, remain operational prerequisites; these local
results do not establish hosted status.

## Workflow and cost

One Linux job owns lint, formatting, strict types, generated-contract drift, frontend tests and
build, Core coverage, all locked Worker suites, Chromium, and deep installed-distribution checks.
It produces one artifact set. The two hosted platform jobs download that set, run the deterministic
Core selection, and install Core/protocol into a clean environment outside the checkout.
The platform smoke verifies CLI help/status, HTTP health, packaged Web JS/CSS, dependency closure,
and absence of Worker/engine modules from Core.

The Linux job has 25 minutes; the macOS platform smoke has 12. The raw runner timeout ceiling is
49 minutes; this is a ceiling comparison, not measured billing savings. No Node/Web/static/Worker-suite
work is repeated on macOS. PR concurrency cancellation,
default/release-only pushes, short artifact retention, and monthly grouped Action update PRs
bound repeated work. Ordinary feature pushes no longer duplicate PR checks.

## Measured checks

| Check | Local result |
| --- | --- |
| Core coverage, exact CI selection | 846 passed, 6 skipped; 161.86 seconds |
| Core statement coverage | 8,403 / 10,059 = 83.54%; configured floor 83% |
| Coverage gate failure probe | coverage report --fail-under=100 exits 2 |
| Final platform Core selection | 143 passed, 1 native-Windows skip; 3.51 seconds |
| Fake Worker, own locked environment | 27 passed |
| OpenAI-compatible Worker, own locked environment | 8 passed |
| VieNeu Worker, own locked environment | 110 passed |
| Web unit tests | 130 passed in 14 files |
| Web types / locale parity / production build | Passed |
| Chromium, two workers / zero retries | 11 passed; 6.6 seconds |
| Clean artifact CLI/API/assets smoke | Passed |
| Existing installed tests with shared artifact set | 2 passed, 1 optional offline VieNeu closure skip |
| Ruff lint / actual format check | Passed; 215 handwritten Python files formatted |
| Strict mypy | Passed; 97 source files |
| Protocol / OpenAPI drift / internal release metadata | Passed |
| actionlint v1.7.7 / git diff --check | Passed |

The coverage command excludes three old distribution fixtures to avoid repeated builds; all
three are still run after the one release build with TTS_STUDIO_TEST_DIST_DIR. The six Core
skips are existing opt-in hardware/offline-closure/native-Windows conditions. Coverage reports
only Core code in the pytest process; it does not count isolated Workers, installed subprocesses,
or browser execution. The 83% floor rounds down the measured value to leave a small platform and
timing margin. Confirm the floor against the first hosted Linux run rather than claiming the
macOS measurement proves identical Linux coverage.

The deep wheel test completed its Core/HTTP/Web checks but skipped its optional offline VieNeu
installation because the isolated local cache lacked the vieneu==3.6.3 distribution.
The mandatory new platform artifact smoke cannot skip on missing dependencies.
Existing SQLite ResourceWarnings remain visible in the full Core run.

Playwright's Node downloader timed out locally. The exact same official Chromium Headless Shell
153.0.8010.12 (Playwright revision 1243) archive was fetched through curl from the reported
Playwright CDN URL and unpacked to its normal cache; the final test used this matching Chromium,
not a different system browser. The normal CI install command is unchanged. Earlier failing test
runs produced both screenshots and traces, verifying failure-evidence collection.

## Formatting and fixture changes

The initial format check found 114 files requiring changes, including generator-owned output.
The adoption pass reformatted 110 handwritten files. Across the final Python diff, 106 files
have identical Python ASTs before and after, with seven intentionally changed existing test files:

- Three CLI fixtures now pass their real listener host/ephemeral port to Settings, preserving
  the approved Host-validation rule. The absent-SIGKILL test also works on native Windows.
- Three installed-distribution tests optionally consume an existing artifact directory.
- The hardware-script syntax check reads its string literal through Python AST, so harmless
  formatter quote changes do not invalidate the test.

The newly added Python file is scripts/ci_artifact_smoke.py. Production Python changes are
formatting only. Browser fixtures now advertise the capabilities that the UI requires and assert
current localized errors, combobox values, theme labels, and exact job states.

Formatting exclusions are exactly:

- packages/protocol/src/tts_studio_protocol/engine/v1/*_pb2*.py
- packages/protocol/src/tts_studio_protocol/engine/v1/*_pb2*.pyi
- src/tts_studio/generated/api.py

All generated protobuf runtime modules/stubs and both generated OpenAPI clients remain unchanged.
No release version, RPC, HTTP interface, or Core/Worker isolation invariant changed.

## Reviewed Action pins

Each release tag was resolved through GitHub's upstream commit API on 2026-09-14; all seven
commits reported verification.verified=true. Review included the upstream release identity,
commit message, and required workflow inputs. This is provenance verification, not a claim of
an independent full source audit.

| Action release | Full pinned commit |
| --- | --- |
| [checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1) | 3d3c42e5aac5ba805825da76410c181273ba90b1 |
| [setup-python v7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0) | 5fda3b95a4ea91299a34e894583c3862153e4b97 |
| [setup-uv v10.1.0](https://github.com/astral-sh/setup-uv/releases/tag/v10.1.0) | bec219d24cd3e171d82865faccec33120bb574f4 |
| [pnpm action-setup v6.1.0](https://github.com/pnpm/action-setup/releases/tag/v6.1.0) | ea17c68df8912ef543352723c149a84f56e3d413 |
| [setup-node v7.0.0](https://github.com/actions/setup-node/releases/tag/v7.0.0) | 820762786026740c76f36085b0efc47a31fe5020 |
| [upload-artifact v7.0.1](https://github.com/actions/upload-artifact/releases/tag/v7.0.1) | 043fb46d1a93c77aae656e7c1c64a875d1fc6a0a |
| [download-artifact v8.0.1](https://github.com/actions/download-artifact/releases/tag/v8.0.1) | 3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c |

Dependabot proposes grouped monthly updates with a maximum of two open PRs. Updates require
normal review and CI; no automatic merge or privileged update workflow is introduced.
