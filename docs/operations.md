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

Models load lazily. Users can unload a model or restart its Workers. The VieNeu Worker
uses one serialized runtime replica, explicit ONNX/CPU `int8` or `fp32` selection, and a pinned
offline codec snapshot; configurable replicas remain deferred. ONNX thread count is independent
of replica count.

The supervisor performs bounded authenticated health checks, drains Workers before planned restarts, and propagates cancellation. An unexpected process exit or failed health check marks the replica unhealthy, records sanitized diagnostics, cleans up its managed launch files, and attempts up to three consecutive replacements with capped exponential backoff. Planned shutdown cancels watchers before cleanup, preventing restart races; exhausted replacements remain unhealthy until an explicit lifecycle action. Each POSIX launch durably records a pending unique owner claim before spawn, starts the Worker in an isolated process group, then finalizes the record with its PID. Startup reconciliation can recover the pending spawn window by locating exactly one group leader carrying that claim. It revalidates the claim and process-group identity immediately before signaling, retains uncertain records, and never signals an unrelated PID or group. Planned and failure cleanup signal the verified group, including helper descendants, and remove the owner record only after the original group members terminate. Windows launches retain authenticated direct-child lifecycle while Core owns the process handle, including graceful and forced reaping, but do not provide durable orphan recovery or descendant containment after Core loss. Sanitized startup/restart diagnostics are exposed through the runtime response and existing health/status surfaces; the supervisor does not publish restart-history events through Models, Jobs, CLI, or SSE.

Generation Jobs hold the Worker lease only for the active synthesis. Optional alignment Jobs for
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
requires a bearer token on every public API request; the CLI forwards it automatically. Remote
browser token UX remains out of scope for this slice.

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
Diagnostic output redacts credentials, authorization headers, and sensitive upstream payloads.

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

## Audit remediation backlog

The remaining security and reliability backlog is intentionally maintained here as the canonical
backlog. Completed remediation includes owner-only managed-directory and identity-bound file
operations, readiness-file hardening, crash/orphan supervision with bounded replacement, bounded
direct Worker/reference inputs, migration-gap detection, protocol compatibility checks, provider
egress policy, and cancellation quarantine. Remaining work is limited to a stronger operation that
can forcibly terminate a native SDK call and a native Generation retry operation. The implemented
cancellation contract waits a bounded interval, quarantines a Worker/runtime that ignores
cancellation, and fails unload/reuse closed; it does not pretend the native call stopped. Until a
native Generation retry operation exists, ordinary failed and cancelled Generation Jobs are
non-retryable, while startup-recovery failures remain retryable. Other documents link here instead
of maintaining a second backlog.

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
of successful cancellation because no native Generation retry operation exists. If startup recovery
finds cleanup still incomplete, it records a retryable recovery failure; correct the filesystem
problem and restart Core to retry recovery cleanup.

Retention settings support age or storage limits and manual clear-history operations. Deletion is reference-aware and reports file-system failures. Single History deletion removes the managed WAV before deleting its SQLite row; a missing managed file returns the existing `artifact_not_found` route failure, and missing or unsafe managed files never silently remove metadata. The Web UI confirms per-row deletion and treats the bulk action as one confirmed sequence of these single-item operations, not a transactional batch: successful items are removed, failed items remain selected and visible, and the partial failure is surfaced for retry.
