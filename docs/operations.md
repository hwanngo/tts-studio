# Operations

## Data directory

Managed data defaults to `.tts-studio/` below the current working directory. `--data-dir` and `TTS_STUDIO_DATA_DIR` override it. The resolved directory is displayed by the CLI and Web UI.

```text
.tts-studio/
├── database/   # SQLite and migrations
├── models/     # activated model revisions
├── audio/      # retained WAV Audio Artifacts
├── voices/     # saved Reference Recordings or prepared voice data
├── workers/    # provisioned adapter environments and active runtime pointer
├── logs/       # redacted Core and Worker diagnostics
├── run/        # PID, control, and lifecycle records
└── staging/    # downloads and temporary reference recordings
```

The directory is excluded from version control. Every file operation passes through one path-safe storage module. Service installation records the absolute resolved directory so login startup has deterministic storage.
Managed child paths must be real directories: pre-existing symbolic links and non-directory
entries are rejected, resolved containment is rechecked before use, and Worker launch files use
no-follow creation where supported.

## Process modes

- `tts serve`: foreground Core with console logs
- `tts serve --background`: detached Core with a managed PID record and `logs/core.log`
- `tts status`: public health, version, worker state, and data directory for the selected URL
- `tts logs`: recent detached Core diagnostics
- `tts stop`: graceful shutdown with bounded escalation

Per-user login startup is available through `tts service install`, `tts service uninstall`, and
`tts service status`. The commands target user-owned launchd, systemd user-unit, or Windows Task
Scheduler definitions and use the same public `tts serve --background` and `tts stop` lifecycle.
Platform execution smoke remains host-specific.

Worker runtime replacement is managed separately from models and Core data:

```console
tts runtime upgrade --artifact-dir /absolute/path/to/dist --generation release-id
tts runtime rollback
tts runtime status
```

An upgrade validates the complete release set, records artifact hashes, provisions one offline
`uv` environment per engine below `workers/<engine>/generations/`, and verifies the Worker
distribution provenance, console entry point, protocol/SDK packages, and engine dependencies.
Only after all candidates pass does Core activation replace the regular atomic
`workers/current.json` pointer. A failed candidate is removed without changing the active runtime.
Rollback verifies the previous generation before switching the same pointer back. SQLite, model
revisions, Voices, and retained audio are never part of runtime replacement; service definitions
continue to launch the same Core executable and data directory.

An installed login service and a manually started server detect each other through the same run
record and health probe. Foreground and detached Core processes store PID, a local ownership
claim token, and safe launch metadata in `run/core.json`; logs remain under managed `logs/`. Service installation refuses to
activate a second Core while one process owns that record. Health probes normalize IPv4 and IPv6
wildcard listeners to connectable loopback addresses. Port collisions, stale PID records, and
mismatched data directories produce actionable diagnostics rather than starting duplicate Cores.

## Worker operations

Models load lazily. Users can unload a model or restart its Workers. Each VieNeu Worker Replica
serializes one active generation and uses explicit ONNX/CPU `int8` or `fp32` selection with a
pinned offline codec snapshot. The desired replica count is configurable from one through eight;
the supervisor schedules each healthy replica independently. ONNX thread count is independent of
replica count.

The supervisor performs bounded authenticated health checks, drains Workers before planned restarts, and propagates cancellation. An unexpected process exit or failed health check marks the replica unhealthy, records sanitized diagnostics, cleans up its managed launch files, and attempts up to three consecutive replacements with capped exponential backoff. Planned shutdown cancels watchers before cleanup, preventing restart races; exhausted replacements remain unhealthy until an explicit lifecycle action. Failed termination retains the quarantined process and its ownership resources so a subsequent stop can retry cleanup. A replica remains owned during failure cleanup, including when planned shutdown interrupts its watcher.

Each POSIX launch durably records a pending unique owner claim before spawn, starts the Worker in an isolated process group, then finalizes the record with its PID. Startup reconciliation can recover the pending spawn window by locating exactly one group leader carrying that claim. It revalidates the claim and process-group identity immediately before signaling, retains uncertain records, and never signals an unrelated PID or group. Planned and failure cleanup signal the verified group, including helper descendants, and remove the owner record only after group termination is verified.

Windows launches use an unnamed, non-inheritable, kill-on-close Job Object with breakaway disabled. A trusted Core Python standard-library gate blocks on a private pipe until Job assignment succeeds; only then does it spawn the configured isolated Worker command. Assignment failure never starts engine code. Forced and planned process cleanup terminate the Job and verify that its active-process count reaches zero before closing the handle. Failed termination retains the Job handle for retry. Closing Core's last Job handle, including on Core exit, asks Windows to terminate contained descendants. There is no durable Windows orphan-owner record. The portable gate and cleanup-abstraction tests run on every hosted smoke; native Job descendant coverage requires a separately provisioned Windows runner and is skipped on other platforms. This uses the documented [Windows Job Object containment model](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

Sanitized startup/restart diagnostics are exposed through the runtime response and existing health/status surfaces; the supervisor does not publish restart-history events through Models, Jobs, CLI, or SSE.

Generation and alignment operations wait for supervisor lease admission. Separate ready replicas
can work concurrently; excess requests wait for capacity, and an unhealthy replica cannot be
admitted or make its healthy siblings unavailable. A Generation Job holds its replica through
synthesis, artifact finalization, and failure/unload cleanup. The same lease ordering protects
alignment cleanup. Model-removal pins and leased-replica unload refusal still apply.

Interrupted synthesis cancels the gRPC stream and allows at most 500 ms for authenticated
`UnloadModel` to confirm native quiescence. A locally cancelled gRPC call or a READY Health response
alone is not proof of native termination. If unloading does not confirm release, Core quarantines
that replica and invokes the supervisor's hard-stop operation: verified POSIX group SIGKILL or
Windows Job termination. Forced child reaping is bounded to five seconds per wait, and POSIX group
drain checks are separately bounded. A termination/identity failure reports cleanup failure and
prevents reuse and replacement; it never claims that the native call stopped. Successful forced
cleanup permits the existing bounded replacement policy. Repeated stream-close callers wait for
the same cleanup, and a later stop of an already terminated replica cannot affect its replacement.
Cooperative cancellation unloads the model, so later use must load it again.

Optional alignment Jobs for
completed retained generations also pin the Model Installation while queued or running. Core copies
the retained WAV to a private read-only managed snapshot for alignment, revalidates the original
artifact identity before and after the Worker call, and removes only the private snapshot. Alignment
failure never invalidates successful synthesis or deletes the retained artifact; interrupted work is
recovered as retryable alignment failure after Workers are ready.

Core writes PCM to a private
`.generation-*.pcm` file below `staging/`, validates the completed stream, and atomically publishes
retained WAV bytes below `audio/`. Workers do not write Audio Artifacts. Startup recovery removes
generation temporaries and abandoned regular `.restore-*` rollback staging entries below `audio/`
before model-download recovery scans shared staging, marks interrupted loading/generating/finalizing
jobs with `recovery_required`, and leaves no partial artifact. A post-publication rollback-staging
cleanup failure remains visible as `artifact_delete_failed`; the restored public WAV and metadata
are retained for startup cleanup and retry.

Retained-artifact deletion requires a no-follow, descriptor-relative identity check. POSIX hosts
with that primitive can delete the validated entry atomically. CPython on Windows does not expose
an equivalent primitive, so deletion and retention clearing fail closed there: the artifact and
its metadata remain intact and the public API reports the cleanup failure rather than risking a
path-replacement deletion.

Temporary Reference Recordings live only below `staging/references/`. Core stores an opaque ID and
safe metadata in SQLite; the transcript stays in the in-memory generation handoff. A validated
reference is claimed once, removed on success/failure/cancellation, expires after one hour, and is
removed during startup recovery. Missing transcript handoff after restart becomes
`reference_recovery_required`; redirected or unsafe cleanup becomes retryable `cleanup_failed`.

The ordinary VieNeu contract gate is deterministic and does not access Hugging Face or require a
real model. The opt-in hardware gate requires `TTS_STUDIO_RUN_VIENEU_HARDWARE=1` and an explicit
local cache marker; the marker must pin VieNeu commit `8b7e9cffb4b41918cb638b9f62f0a751184d14a6`,
codec commit `ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae`, SDK `3.6.3`, and safe `int8`/`fp32`
files. It verifies both runtime variants, discovered preset voices, authenticated preset synthesis,
first-audio streaming, cancellation, 48 kHz output, unload/reload, offline reload, and—when the
installed SDK advertises the runtime fact—reference validation and reference synthesis. Missing
prerequisites are precise skips, never implicit downloads or a fallback to floating revisions.

## Networking

The default listener accepts loopback IP literals and defaults to `127.0.0.1`. Non-loopback serving
requires `TTS_STUDIO_API_TOKEN_ENV` to name a populated environment variable. The Core then
requires a bearer token on every public API request; the CLI forwards it automatically. The Web UI
prompts after its first authentication failure and keeps the supplied bearer token only in the
current browser runtime; it never stores it in URLs or browser storage. See
[the HTTP authentication contract](http-api.md#network-security).

Worker gRPC ports are ephemeral and loopback-only. Every launch uses an explicit command and
resolved working directory. The child receives only an allowlist of required platform environment
variables, rather than inheriting the Core environment. Its per-launch secret is written as a
private file below managed `run/`, passed by path rather than value in argv, consumed and removed
by the Worker before readiness, and removed by the Core on every startup failure and shutdown
path. POSIX Workers require current-user ownership and reject group or other permission bits.
Windows Workers do not interpret CPython's synthetic POSIX mode bits as an ACL: they use the
inherited Windows access controls of the user-owned data directory, open a non-inheritable handle,
and retain the same regular-file, bounded-size, no-follow-when-available, and cleanup checks. A
custom Windows data directory must therefore grant access only to the account running TTS Studio
and intended administrators.

Remote Provider Profiles store environment-variable names only. Core passes required provider
values explicitly over the authenticated typed Worker request rather than restoring wholesale
parent-environment inheritance.

## GitHub security operations

The repository-owned `.github/workflows/security.yml` defines CodeQL analysis for Python and
JavaScript/TypeScript on a `main` push, its weekly schedule, or a manual dispatch. It deliberately
has no pull-request trigger. A completed hosted run and uploaded results are required before CodeQL
is treated as observed evidence. To preserve the 25-minute pull-request assurance ceiling and avoid
a second runner, `.github/workflows/ci.yml` invokes the repository-local
`scripts/scan_secrets.py` as an initial Linux-assurance step. The local scanner reads only Git-tracked
text files, rejects every symbolic-link path component, has deterministic rules and a size limit in
`security/secret-scan.toml`, and makes no network requests. The security maintainer owns its rules
and must review a proposed exclusion or pattern change; a real credential finding requires rotation
and removal, not a scanner suppression.

Repository administrators must enable GitHub code scanning and secret scanning when the selected
GitHub plan supports them. Enable push protection where available, keep the CodeQL workflow's
SARIF upload permission enabled, and configure default setup only when it does not duplicate this
repository's CodeQL workflow. The platform scanners complement rather than replace the checked-in
workflow: GitHub secret scanning can inspect history and pushes that CI never sees.

Security alerts are owned by the repository security maintainer. Triage new CodeQL or secret
scanning alerts within one business day: validate the finding, revoke/rotate exposed credentials
immediately, create a tracked remediation item for confirmed code issues, and document a precise
false-positive dismissal in GitHub. Treat an alert that cannot be reproduced as open until its
data flow or credential provenance is understood.

The cache-pinned VieNeu hardware definition is `.github/workflows/vieneu-hardware.yml`. It is
scheduled weekly or may be manually dispatched, never runs for pull requests, and requires all four
runner labels: `self-hosted`, `linux`, `x64`, and `tts-studio-vieneu-hardware`. A controlled runner
must be provisioned with the exact `/var/lib/tts-studio/vieneu-cache` directory and regular
`.vieneu-model-cache-marker`, Python 3.14, `uv`, and the offline locked Python dependencies before
the definition can run. It validates marker JSON, exact model/codec/SDK revisions, and every
required regular, non-symbolic-link model, codec, and cloning file before `pytest` starts. Cloning
requires both `cloning/denoiser.onnx` and `cloning/speaker_encoder.onnx` below `models/vieneu`,
because runtime loading opens both even when the gate uses a preset Voice. A successful
recent hosted execution is a release-candidate prerequisite; repair or re-provision the runner/cache
rather than allowing a download, a skipped prerequisite, or a broader trigger. Diagnostic output
redacts credentials, authorization headers, and sensitive upstream payloads.

The first remote provider adapter is `openai_compatible`. It runs in its own locked `uv` project,
accepts typed provider configuration in the authenticated synthesis RPC, calls the provider's
`/audio/speech` endpoint, and accepts only 48 kHz mono signed-16-bit WAV output. Provider SDKs and
Worker SDK helpers are not installed in Core; Core validates profiles with its own narrow copy of the
egress policy, while the isolated Worker revalidates immediately before transport. Provider egress is
fail-closed: non-loopback targets require HTTPS, HTTP is
allowed only for loopback, URL credentials/query/fragment components are rejected, and DNS results
are checked for global addresses immediately before connecting. Redirect targets are validated by
the same policy, so provider credentials are not sent to private, loopback, link-local, reserved,
unresolved, or otherwise disallowed destinations. Provider cancellation waits at most 250 ms for a
resolver or transport operation to drain; an operation still blocked after that deadline quarantines
the Worker, makes Health not ready for supervised replacement, and checks cancellation again before
any credentialed connection can continue.

## Settings and service administration

The Settings API is the authoritative seam for retention policy and safe Core configuration. It
persists retention defaults and age/storage limits, reports current retained counts and bytes, and
supports an explicit confirmation-bearing cleanup operation. Manual cleanup attempts every retained
Audio Artifact regardless of age or storage limits and remains reference-aware: deleted, skipped,
and failed artifacts are reported, and filesystem failures are not hidden.

The API returns only the effective name of the environment variable used for bearer authentication.
It never accepts, persists through the public Settings route, or returns the credential value. The
name remains a read-only startup diagnostic until a safe lifecycle configuration writer exists;
Settings rejects attempts to create an unapplied pending authentication configuration.

Service lifecycle administration is also explicit and confirmation-bearing. The public routes
`/api/v1/service/install`, `/uninstall`, and `/restart` delegate to the same platform lifecycle
adapter used by the CLI; unsupported adapters report a stable unsupported error rather than
silently changing state.

## Retention and privacy

Generation text and final WAV are retained locally by default. A request can opt out. One-off cloning references are temporary and deleted after generation. Saved cloned Voices retain only the managed reference or prepared data required by their Adapter and record consent acknowledgment.

Core bounds generation text at 10,000 characters and native/OpenAI generation JSON bodies at
128 KiB. When retention is disabled, queued and active jobs keep text for durable execution, then
atomically replace it with an empty string when completed, failed, or cancelled. Migration 013
also redacts non-retained jobs that were already terminal. Retained jobs keep their text. This is
logical SQLite redaction, not a guarantee of forensic erasure from old backups or storage media.

The explicit Generation retry endpoint creates one successor per eligible failed attempt and
persists that relationship with a unique SQLite index. Retried jobs receive new IDs and own their
own artifacts; original attempts and their errors remain inspectable. A crash between persisting
the queued successor and scheduling it is handled by ordinary queued-job startup recovery.
Requests for a job with an existing successor return that successor without synthesizing again.
If a transient retained attempt leaves a runtime that cannot unload, successful supervised process
termination preserves the original retryable Worker error. Explicit retry waits for cleanup and
healthy replacement capacity before handing a fresh request to a new lease. No audio stream is
replayed automatically, and failed termination becomes non-retryable `cleanup_failed`.

Voice previews are a separate temporary path. Core validates the short request, synthesizes a WAV,
and returns it directly without creating a Generation Job, Audio Artifact, or History row. The
selected model is unloaded after the preview, and preview bytes are not copied into managed `audio/`
or any other durable store. Preview failure or cleanup failure does not create a retained artifact.

When retention is disabled, Core validates the generation, discards its output, and completes the
Generation Job without an Audio Artifact or History row. No playback or download is available for
that output when retention is disabled. Explicit cancellation propagates to the
Worker and removes the private temporary file before the job becomes terminal. Model removal is
refused while any non-terminal Generation Job pins that Model Installation.

If active-generation cleanup fails, the job reports a non-retryable `cleanup_failed` error instead
of successful cancellation. The generation retry endpoint refuses this state. If startup recovery
finds cleanup still incomplete, it records a retryable recovery failure; correct the filesystem
problem and restart Core to retry recovery cleanup.

Retention settings support age or storage limits and manual clear-history operations. Deletion is reference-aware and reports file-system failures. Single History deletion removes the managed WAV before deleting its SQLite row; a missing managed file returns the existing `artifact_not_found` route failure, and missing or unsafe managed files never silently remove metadata. The Web UI confirms per-row deletion and treats the bulk action as one confirmed sequence of these single-item operations, not a transactional batch: successful items are removed, failed items remain selected and visible, and the partial failure is surfaced for retry.
