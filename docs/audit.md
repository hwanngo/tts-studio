# Audit closure

This document records the closure state of the repository audit. It distinguishes controls that
are present and verifiable in this repository from execution and settings that only a repository
administrator or a provisioned runner can establish. The public contracts are the focused
documents linked from the [documentation index](README.md); it does not duplicate those contracts.

## Repository-verifiable resolutions

| Finding group | Resolution | Verification evidence |
| --- | --- | --- |
| HTTP trust boundary and browser authentication | Loopback requests require an accepted numeric `Host`; browser mutations with an `Origin` must be same-origin. Non-loopback API access requires a bearer token. The Web UI keeps that token only in memory, attaches it to API, event, PCM, media, and download requests, and clears it after an authentication failure. | [HTTP interface](http-api.md#network-security); `tests/server/test_authentication.py`; `web/src/lib/api.test.ts`; `web/src/app/App.test.tsx` |
| Generation input, retention, and retry | Native and compatible generation input is bounded before persistence. Terminal jobs without retention redact their text transactionally. Retry creates one durable successor only for eligible retained failures with a reusable source. | [HTTP interface](http-api.md#principles); [Operations](operations.md#retention-and-privacy); `tests/generation/test_service.py`; `tests/server/test_generation_routes.py`; `tests/storage/test_migrations.py`; `tests/integration/test_phase3_generation_artifact.py` |
| Replica capacity and Worker containment | The supervisor admits work by healthy replica lease, supports a desired count of one through eight, quarantines an uncooperative Worker, and uses verified POSIX process groups or Windows Job Objects for forced cleanup. | [Architecture](architecture.md#primary-generation-flow); [Operations](operations.md#worker-operations); `tests/integration/test_generation.py`; `tests/workers/test_supervisor.py`; `tests/workers/test_generation_pool.py`; `tests/workers/test_windows_containment.py` |
| Capability-aware interface and accessibility | Web controls use observed runtime capabilities, preserve merged preset and Saved Voice selections, update document language with the selected locale, and provide compact controls with a 44 px hit target. | [HTTP interface](http-api.md#principles); `web/src/features/studio/StudioPage.test.tsx`; `web/src/features/voices/VoicesPage.test.tsx`; `web/src/i18n.test.ts`; `web/src/styles.css` |
| Release-set compatibility and local assets | Core, protocol, Worker SDK, and Workers use one exact internal release set. Package metadata declares MIT terms, and Web assets use local/system fonts rather than remote font requests. | [Development](development.md#internal-release-compatibility); `tests/packaging/test_release_set.py`; `tests/packaging/test_project_assets.py`; `scripts/sync_release_versions.py` |
| Deterministic CI definition | The checked-in CI definition uses immutable Action revisions, cancellation of superseded refs, one Linux assurance build, shared release artifacts, platform smoke definitions, Worker suites, browser checks, formatting, and a Core coverage floor. | [Development](development.md#ci-cost-coverage-and-dependency-updates); `.github/workflows/ci.yml`; `docs/ci-verification.md` |
| Repository-local secret scanning | The deterministic scanner examines tracked text only, rejects symbolic-link paths, makes no network request, and is configured by tracked rules. | [Operations](operations.md#github-security-operations); `scripts/scan_secrets.py`; `security/secret-scan.toml`; `.github/workflows/ci.yml` |

The focused verification records retain local command results and their stated limits:

- [Generation verification](generation-remediation-verification.md)
- [Worker containment verification](worker-containment-remediation-verification.md)
- [Release-set and typing verification](release-set-typing-verification.md)
- [CI assurance verification](ci-verification.md)
- [Final full-branch verification](final-remediation-verification.md)

## Operational prerequisites

The following are intentionally not marked complete by repository contents alone.

1. A hosted execution of `.github/workflows/ci.yml` must complete on Linux, macOS, and Windows
   before treating the configured platform smoke coverage as observed evidence.
2. A hosted execution of `.github/workflows/security.yml` must complete and upload its CodeQL
   results before treating CodeQL analysis as observed evidence.
3. A controlled self-hosted runner with the required labels, offline cache, marker, Python 3.14,
   and `uv` must be provisioned; a successful execution of
   `.github/workflows/vieneu-hardware.yml` is required before a release candidate relies on the
   real-model gate.
4. Repository administrators must enable GitHub code scanning and secret scanning, and push
   protection where the selected GitHub plan provides it. They must also retain the CodeQL SARIF
   upload permission and follow the alert-triage procedure in
   [Operations](operations.md#github-security-operations).

These prerequisites are external service or runner state. They do not weaken the repository-local
contracts above, and they must be recorded with the relevant run or settings evidence when met.
