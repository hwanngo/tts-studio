# Worker containment and capacity remediation verification

Date: 2026-09-14. Host: macOS ARM64, Python 3.14.7. Scope: remediation plan Task 3.

## Implemented behavior

- Generation creation, previews, voice discovery, synthesis, and alignment use supervisor
  admission instead of a process-wide generation lock. Independent healthy replicas can execute
  simultaneously; excess work waits on the supervisor's capacity condition.
- Immediate acquisition remains available to existing lifecycle callers. Admission excludes
  quarantined/unhealthy replicas while allowing healthy siblings to serve or queue work.
- Generation and alignment keep their lease through failure cleanup and unload. Removing the old
  global gate does not allow another operation to reuse a replica before cleanup finishes.
- Interrupted synthesis sends gRPC cancellation and permits 500 ms for authenticated unload to
  confirm native quiescence. This deliberately does not interpret gRPC cancellation completion or
  a READY Health response as proof that a native SDK call has stopped.
- If confirmation fails, the supervisor quarantines the exact owned replica before forced
  process containment. Verified POSIX process-group identity and descendant cleanup are retained;
  hard-stop sends SIGKILL and bounds reaping. Failed termination retains ownership and quarantine
  and prevents replacement. A later stop can retry cleanup.
- Stream closure is shared across concurrent callers and shields its cleanup from cancellation.
  Lease capacity is not released merely because another caller already initiated closure.
- Watchers retain ownership while failure cleanup is in progress. Planned stop can therefore
  clean the same replica when it interrupts that watcher. Repeated hard-stop on an already
  terminated Worker cannot cancel or quarantine a replacement that occupies the same replica ID.
- Native unload failures after a transient retained synthesis failure can be cleaned by verified
  process termination, preserving the original retryable Worker error. Explicit retry creates a
  fresh attempt, waits for cleanup and replacement capacity, and does not replay a prior stream.

FastAPI remains the sole public server and SQLite writer. No protobuf, HTTP schema, adapter
capability assumptions, provider credentials, or model-removal approval gates changed.

## Windows boundary

The Windows adapter owns a non-inheritable unnamed Job with kill-on-close and no breakaway flags.
A trusted Core-Python standard-library bootstrap blocks on a private pipe. Core assigns that
bootstrap to the Job before allowing it to spawn the configured isolated Worker command. Job
assignment failure prevents engine code from starting. Cleanup terminates the Job and checks its
active-process count before closing its handle; uncertain cleanup retains the handle for retry.

This gate avoids assigning an already-running engine after it has had a chance to spawn an
uncontained descendant. It also avoids undocumented thread-resume APIs or new engine dependencies
in Core. Windows does not use durable POSIX-style orphan records.

The portable gate tests use real processes on the host. Cleanup abstraction tests verify that
pending descendants retain the Job handle and can be retried. A native Windows integration test
launches the gate, Worker, and helper, then verifies Job termination. That test is explicitly
skipped on macOS. Native Windows execution has **not** been claimed or verified in this run.

The implementation follows Microsoft's documented
[Job Object membership, termination, and kill-on-close model](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

## RED/GREEN evidence

The following failures were observed before their corresponding implementation changes:

| Regression | RED evidence | GREEN evidence |
| --- | --- | --- |
| Two ready replicas execute concurrently | Second generation creation timed out on `_replica_gate` while the first replica was held | Both real-gRPC generations reach GENERATING before release; third request waits and later completes |
| Uncooperative cancellation | Worker `returncode` remained `None` after stream closure | Worker exits before reuse and a replacement synthesizes successfully |
| Repeated close waits for native cleanup | Second `aclose()` finished while the first unload was blocked | Both callers wait for the shared cleanup task |
| Failed hard-stop preserves ownership | `stop_all()` discarded the still-live quarantined Worker after termination was denied | Ownership remains available for a later successful stop |
| Planned stop during watcher cleanup | Readiness/ownership files remained after stopping while failure cleanup was blocked | Planned stop finds the owned replica and completes cleanup |
| Healthy sibling capacity survives another replica's failure | Waiting acquisition raised `worker 'fake' is not running` despite a healthy leased sibling | Waiter acquires that sibling after release |
| Stale hard-stop cannot affect replacement | Repeating hard-stop on the original Worker cancelled replacement supervision and failed ownership verification | Repeated call is harmless and replacement health remains ready |
| Retry after native cleanup failure | A real-gRPC fixture returned a retryable error but remained alive after refusing unload | Containment removes the Worker/helper, preserves retryability, and the explicit successor completes once |

The new Windows module's initial test collection failed because the module did not yet exist;
after implementation, the portable gate tests passed and the native test skipped on this host.
The descendant-exit abstraction test additionally exercises retained Job ownership on timeout.

The real-gRPC stuck fixture extends only the test fake adapter in its isolated `uv` project. It
starts a cancellation-ignoring thread and a SIGTERM-ignoring helper, continues reporting READY,
and refuses unload while stuck. It validates that containment actually removes live processes
and does not depend on a mocked process exit or on the Health response.

## Final commands and results

```console
uv run pytest -q tests/workers/test_supervisor.py tests/workers/test_generation_pool.py tests/workers/test_windows_containment.py tests/generation tests/integration/test_generation.py tests/integration/test_alignment.py tests/integration/test_reference_cloning.py tests/server/test_generation_routes.py tests/models/test_download_service.py
```

Result: **347 passed, 4 skipped**, 74.65 seconds. Skips comprise the native Windows Job test and
three existing host-specific generation filesystem cases. Includes authenticated gRPC generation,
cancellation, retry, replica crashes/replacement, alignment, reference handling, public generation
routes, and model lifecycle/removal protection.

```console
uv run pytest -q tests/packaging/test_build_distribution.py tests/packaging/test_wheel.py
uv run python scripts/build_distribution.py
uv run python scripts/check_protocol.py
uv run ruff check src/tts_studio/workers src/tts_studio/generation/service.py tests/workers/test_supervisor.py tests/workers/test_generation_pool.py tests/workers/test_windows_containment.py tests/fixtures/stuck_synthesis_worker.py tests/integration/test_generation.py
git diff --check
```

Packaging: **3 passed, 1 skipped**. The installed-wheel smoke completed Core isolation/import,
server, and Web assertions, then skipped its optional VieNeu environment because `vieneu==3.6.3`
was absent from the offline `uv` cache. Release build, protocol drift check, focused Ruff, and
diff whitespace checks passed. The built Core wheel contains the new Windows containment module.

`uv run mypy --platform linux src/tts_studio/workers/windows_job.py src/tts_studio/workers/supervisor.py --follow-imports=silent`
passed with no diagnostics. The full configured mypy targets retain exactly the existing **26**
diagnostics after normalizing source line numbers, with no added or removed diagnostic. Task 3
does not update the tracked baseline; integration must refresh its changed line locations in
the plan's type-baseline task.

## Remaining verification limits

- Native Windows Job Object execution still requires the Windows runner. Portable abstraction
  coverage is not a substitute for a successful native platform run.
- No real VieNeu SDK/model execution was attempted; the live stuck-call fixture is deterministic
  and test-only. The optional installed VieNeu dependency closure was unavailable offline.
- POSIX signaling continues to fail closed if ownership/process-group identity cannot be
  verified. A kernel or permission failure to terminate remains visible cleanup failure; no
  claim is made that an unkillable process has stopped.
