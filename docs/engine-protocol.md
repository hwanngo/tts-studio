# Engine protocol

## Purpose

The engine protocol is the stable seam between the Core and every Engine Adapter. Its source of truth will be a versioned protobuf schema. Communication uses gRPC over ephemeral loopback TCP ports with a per-launch credential in request metadata.

## Contract

The initial schema must express:

| RPC | Responsibility |
| --- | --- |
| `Describe` | Protocol version, adapter identity, capabilities, audio formats, runtime variants, and concurrency limits |
| `Health` | Readiness, loaded models, resource diagnostics, and safe warnings |
| `ValidateModel` | Repository compatibility, required artifacts, available variants, and diagnostic evidence |
| `DownloadModel` | Stream progress events and return a verified adapter manifest |
| `LoadModel` | Lazily load an activated Model Installation with variant and runtime settings |
| `UnloadModel` | Release model resources safely |
| `ListVoices` | Return authoritative presets and voice constraints |
| `ValidateReference` | Check cloning audio format, duration, and engine constraints |
| Saved Voice preparation | Not exposed by the current protobuf schema; do not infer or invent a Worker RPC |
| `Synthesize` | Stream header metadata, PCM chunks, progress, warnings, and final result |
| `Align` | Return bounded frame-offset alignment for retained audio and a transcript |

The reference-cloning extension is frozen in `packages/protocol/proto/tts_studio/engine/v1/engine.proto`:
`ValidateReference(ValidateReferenceRequest) -> ValidateReferenceResponse` is unary, and
`SynthesizeRequest.voice_source` is a oneof containing the existing `voice_id` field 2 and
`ReferenceAudio reference` field 4. `ReferenceAudio.reference_path` is a Core-owned,
managed-relative path and its optional `transcript` is an in-memory handoff. Validation returns
only safe `ReferenceMetadata` and compatibility evidence; existing synthesis fields 1–3 remain
unchanged. Every request carries per-launch authentication metadata, and reference validation has
a bounded 30-second Worker deadline.

The remote-provider extension adds an additive `ProviderConfig` message and `provider` field to
`SynthesizeRequest`. It carries the validated provider base URL, provider model, and resolved API key
only over the authenticated Core-to-Worker RPC; the key is never persisted or inherited through the
Worker environment. Existing synthesis field numbers and voice-source behavior remain unchanged.

Workers advertise `reference_cloning` only when the adapter can validate references and accept the
reference synthesis branch. Alignment is a separate authenticated unary RPC and is never part of ordinary synthesis. Core
passes a managed-relative read-only WAV snapshot plus the transcript; Workers return bounded
`AlignmentResult` units with UTF-8 source byte ranges, ordered frame offsets, confidence, and an
`estimated` marker. Core validates identity, bounds, and capability metadata before converting
frames to milliseconds for public APIs. Adapters advertise alignment only as a runtime capability;
VieNeu intentionally omits it until a production Vietnamese aligner exists.

The interface should remain small: new engine details belong in
declared capabilities or typed extension data only when more than one real adapter needs them.
Saved Voice preparation is intentionally not a Worker RPC in the current contract. Any future
preparation feature requires an approved wire contract before the protobuf schema or generated
artifacts change. `DownloadModel` emits `DownloadProgress`, `ModelManifest`, or `WorkerError` events;
progress carries byte counters and progress-state information, while checkpoint and raw-byte payloads are not
part of this schema.

`SynthesizeRequest.options` carries optional `speed`, `pitch`, and `volume` values. These are
capability-gated runtime facts: Core forwards a requested value only when the selected Worker
advertises the matching capability name, and a missing capability is a stable unsupported-option
failure rather than an inferred fallback. The `inline_cues` capability has the same runtime-fact
rule. Cues, when an adapter supports them, remain inline in the ordinary text field; the protocol
does not add a separate cue field or assume that every adapter interprets cue syntax.

## Streaming rules

- The first synthesis event describes sample rate, channels, sample representation, and any known duration.
- Audio events contain binary PCM, never base64.
- Sequence numbers detect gaps and enforce ordering.
- A successful stream has exactly one terminal result reporting engine timing and completion
  metadata. Core rejects result-before-header, duplicate results, and any event after a result;
  a structured Worker error can terminate an unsuccessful stream before its header.
- gRPC cancellation and deadlines propagate into engine inference. Adapters wait for a bounded
  cancellation interval before lifecycle cleanup; if native inference ignores cancellation, the
  Worker quarantines that runtime, fails unload closed, and rejects reuse rather than claiming
  quiescence. The isolated Worker remains the containment boundary until an explicit lifecycle
  action terminates it.
- Core-to-client backpressure limits buffered audio.
- Once any audio is exposed to a client, a Worker failure cannot be retried transparently. Core
  Generation terminal errors and cancellations are non-retryable because no native Generation
  retry RPC exists; startup-recovery failures remain explicitly retryable after repair.

Core accepts only 48,000 Hz, mono, signed 16-bit little-endian PCM from the fake Worker. The
Core validates header, sequence, byte alignment, and declared frame totals, then writes and
atomically publishes the managed WAV. Finalization uses the original Core-created PCM and WAV
descriptors, checks staging identities, and verifies PCM hashes, WAV sizes, and validated totals
before publication. A Worker sends PCM over gRPC and never writes a Core Audio
Artifact. `LoadModel` receives the opaque Core-assigned Model Installation ID and managed-relative
cache path; it does not receive or infer a repository ID during generation.

## Worker lifecycle

The Core allocates a loopback port and per-launch secret, starts the Worker, waits for health, checks protocol compatibility, and then admits it to scheduling. A Worker handles one adapter. VieNeu concurrency is implemented with replicas, each accepting one active generation.

Process launch is an explicit internal seam: each launch specification contains the command and a
resolved working directory. Workers inherit only required platform environment variables. The Core
creates the credential as a platform-protected file below managed `run/`; the argument vector
contains only its path. The Worker SDK validates, reads, and unlinks the file before publishing
readiness, while Core cleanup removes it on spawn, startup, handshake, and shutdown failure paths.
POSIX validates owner-only mode bits and current-user ownership. Windows relies on the data
directory's inherited ACL because CPython's POSIX mode bits do not represent that ACL, and opens
the token with a non-inheritable handle. The secret value is never placed in argv or the inherited
environment.

The Core exposes unhealthy Worker state. A per-replica supervisor watches process exit and bounded
authenticated Health checks. Unexpected failure marks the replica unhealthy, records sanitized
startup diagnostics, cleans up its managed process files, and attempts up to three consecutive
replacements with capped exponential backoff. POSIX launches use isolated process groups and unique
managed owner claims so startup reconciliation can terminate a live genuine orphan without signaling
an unrelated PID; unverifiable records fail closed. Windows launches support authenticated direct-child graceful and forced cleanup while Core owns the process handle, but do not provide durable orphan recovery or descendant containment after Core loss. Planned shutdown cancels supervision before cleanup, so it cannot
race a restart. If replacement attempts are exhausted, the replica remains unhealthy and the
failure stays visible until an explicit lifecycle action. Unload is graceful; explicit restart
terminates and recreates the process when graceful shutdown exceeds its deadline.

Model removal and replacement refuse active Generation Job pins before requesting authenticated
`UnloadModel`. The supervisor also refuses unload while the replica is leased, and applies a
10-second unload deadline. A failed unload leaves the previous installation in place.

On Core startup, Generation recovery removes private `.generation-*.pcm` staging files before model
download recovery scans the shared `staging/` directory. This ordering keeps Worker/model staging
cleanup separate from Core-owned generation cleanup and leaves no partial Audio Artifact after an
interrupted job.

## Compatibility

Protocol changes follow explicit versioning rules. Core accepts protocol major `1` and minor versions
`0` through `1`; a different major or unsupported minor is rejected during the authenticated
handshake and on later capability refreshes. CI generates stubs reproducibly and fails on
uncommitted generation drift. Every adapter runs a shared compliance suite covering handshake,
auth, capabilities, model validation, progress ordering, lifecycle, voice reporting, synthesis,
cancellation, deadline handling, and structured errors. SQLite startup refuses to apply an
incomplete migration chain; migrations `002` through `012` must be present and contiguous before
`user_version` advances.

## VieNeu profile

The VieNeu Adapter declares native 48 kHz streamed PCM, WAV finalization, preset Voices, and
`reference_cloning` only when its installed `infer_stream` signature accepts `ref_audio` and
`ref_text`. Reference audio is confined to Core's managed `staging/references/` tree, capped at
8 seconds, and passed as `ref_audio` plus optional `ref_text`; the deprecated `style` parameter
is never sent. Saved Voices and configurable replicas are capability-gated.

VieNeu acquisition uses authenticated `ValidateModel` and `DownloadModel` streams. The Worker owns
repository interpretation and temporary download bytes; Core owns Download Job state, independent
manifest verification, atomic promotion, SQLite activation, cancellation terminalization, and
recovery. Stable acquisition errors include `model_incompatible`, `revision_not_found`,
`download_failed`, `download_cancelled`, `checksum_mismatch`, `insufficient_storage`,
`model_load_failed`, and `model_unload_failed`. Error details exclude URLs, tokens, absolute paths,
model bytes, SDK tracebacks, and raw upstream exception text.

The adapter accepts only `pnnbao-ump/VieNeu-TTS-v3-Turbo`, pins SDK `3.6.3` and codec revision
`ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae`, and reports runtime capabilities from `Describe` rather
than from adapter naming or file presence. `ListVoices` is authoritative at runtime. The one
serialized Worker instance advertises `max_concurrency=1`; Core does not infer streaming,
cancellation, cloning, or precision support when a capability is absent. Core receives PCM and
alone creates the WAV and Audio Artifact.
