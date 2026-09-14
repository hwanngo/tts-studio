# Task 7 report

Implemented lean deterministic CI and measured coverage. Durable details, exact Action pins,
verification evidence, formatting exclusions, and limitations are in
[docs/ci-verification.md](../../../docs/ci-verification.md).

- Linux alone runs static/types/protocol/OpenAPI/format checks, 846-test Core coverage, all three
  locked Worker suites, 11 Chromium browser tests, Web build, and deep installed fixtures.
- Linux/macOS/Windows each run the same bounded Core selection and a clean installed-artifact
  CLI/API/Web smoke from the shared artifact set. Matrix commands are valid PowerShell/Bash.
- Concurrency, restricted pushes, 25/12-minute timeouts, SHA pins, and monthly grouped Dependabot
  updates reduce duplicate hosted work. Raw timeout ceiling changes from 90 to 61 runner minutes.
- Measured Core coverage is 83.54%, guarded at 83%. A 100% threshold probe correctly exits 2.
- The actual ruff format --check . gate passes. 106 changed Python files are AST-equivalent;
  seven existing test files contain intentional fixture/reuse/parser changes. Generated files
  remain byte-for-byte unchanged.
- Core 846 pass/6 skip; final platform subset 143 pass/1 native-Windows skip; Workers 27/8/110 pass;
  Web 130 pass; Chromium 11 pass; clean artifact smoke passes; reused installed tests 2 pass with
  one optional offline VieNeu closure skip. Ruff/mypy/drift/actionlint/diff checks pass.

No subagents were dispatched. Native Windows and hosted Linux checks await the normal CI run;
no GitHub workflow was dispatched. Existing Task 1–6 source behavior was preserved.
