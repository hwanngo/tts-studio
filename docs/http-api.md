# HTTP interface

## Principles

FastAPI exposes the sole public network interface. The CLI, Web UI, native clients, and OpenAI-compatible clients share the same Core application behavior. OpenAPI is authoritative for public request and response types.

## Native interface

The `/api/v1` namespace covers the implemented foundation and evolves through compatible releases:

- Server health, version, resolved data directory, and lifecycle status
- Engine installations, Worker Replicas, capabilities, and restart/unload operations
- Model validation, download/update/removal, load state, and progress
- Preset, saved cloned, and one-off Reference Recording workflows
- Generation creation, inspection, cancellation, retry, and binary streaming
- Audio Artifact playback, download, retention, and deletion
- Provider Profile configuration and validation
- Settings and resumable event delivery

`POST /api/v1/generations` supports asynchronous tracked work and interactive streaming. `GET /api/v1/events` uses SSE with event IDs so clients can resume after reconnecting. Audio is delivered as a binary stream or managed file.

Alignment is an opt-in post-synthesis operation for completed generations that retained a WAV
Audio Artifact. `POST /api/v1/generations/{job_id}/alignment` queues one alignment Job and is
idempotent; `GET /api/v1/generations/{job_id}/alignment` returns `queued`, `running`, `completed`,
or `failed` state. Completed units expose UTF-8 source byte ranges and public `start_ms`/`end_ms`
converted from Worker frame offsets, plus confidence and an `estimated` marker. Alignment is
capability-gated at runtime and does not mutate the Generation Job or Audio Artifact. VieNeu
currently returns `alignment_unavailable` because no production Vietnamese aligner is installed.

`GET/POST/DELETE /api/v1/saved-voices` manages Core-owned Saved Voice profiles. A Saved Voice is
created from a validated Reference Recording and remains pinned to its Model Installation. Deletion
fails closed with retryable `503 reference_cleanup_failed` when identity-bound removal is unavailable
or cannot be proven; the registered row and managed bytes remain intact for retry.

`GET /api/v1/history` lists retained Audio Artifacts and `DELETE /api/v1/history/{artifact_id}`
deletes one retained artifact by removing its managed WAV before its History row. If the managed WAV
is missing, redirected, or otherwise unsafe, deletion fails without removing SQLite metadata; a
missing WAV uses the existing `404 artifact_not_found` route response so clients keep the row visible.
Deletion requires an OS primitive that atomically verifies the open file identity while removing its
directory entry. Hosts without that primitive fail closed with `503 artifact_delete_failed` and retain
both the WAV and SQLite row rather than falling back to a pathname-only unlink. Storage failures while
rolling back a file-first deletion use the same stable failure, while operational database errors remain
internal errors after a successful file restore. Artifact audio downloads validate into an immutable
snapshot before serving; `GET` and `HEAD` expose `Accept-Ranges: bytes` and `Content-Length`, and a
single valid byte range returns `206` with `Content-Range`. Unsatisfiable, malformed, or multi-range
requests return `416` with `Content-Range: bytes */<size>`. The complete snapshot is hashed before
any `GET`, range, or `HEAD` response is sent; this intentionally trades full-file I/O for the
invariant that no bytes from a same-size-tampered artifact are served.
The public API intentionally has no all-or-nothing bulk-delete route. The Web UI requires explicit
confirmation for both per-row deletion and Delete selected. The bulk action confirms once, then
issues the same single-artifact delete operation for each selected row; successful rows disappear,
while failed rows remain visible and selected and the UI reports the partial failure so the user can
retry or inspect it.

### Settings, runtime, and service administration

`GET/PATCH /api/v1/settings` exposes and updates only safe settings fields. Retention defaults and
limits are editable. The effective API token environment-variable name is a read-only startup
diagnostic; secret token values are never accepted or returned. Because this slice has no safe
startup-configuration writer, `restart_required` remains false and `PATCH` rejects token-variable,
host, and port fields rather than persisting configuration the running Core cannot apply.
`POST /api/v1/settings/retention/clear` requires `{ "confirm": true }`, attempts every retained
Audio Artifact through Core's reference-aware deletion path regardless of configured policy limits,
and reports deleted, skipped, and failed artifacts with safe issues.

`GET /api/v1/runtime` reports version, resolved data directory, generation state, worker capability
facts, aggregate Worker health, sanitized startup/restart diagnostics, and storage/database
accessibility. Unexpected Worker exit or failed health checks are supervised with bounded replacement
attempts and capped exponential backoff; exhausted replicas remain unhealthy until an explicit
lifecycle action. `GET /api/v1/service` reports lifecycle status. Install, uninstall, and restart
use explicit confirmation-bearing `POST` routes and return an operation result; unsupported
lifecycle operations return `service_unsupported`.
`PATCH /api/v1/models/{model_id}/replicas` changes the desired Worker replica count from 1 to 8.
`GET /api/v1/generations/{job_id}/pcm` streams transient validated mono S16LE PCM with explicit
sample-rate, channel-count, and encoding headers; PCM is never durable. Generation Jobs currently
have no native retry operation, so failed and cancelled job records are never marked retryable;
clients must create a new job explicitly, and consumed Reference Recordings are not reusable.

`POST /api/v1/voices/preview` accepts `{ "model_id": "...", "voice_id": "...", "text": "..." }`
and returns a directly playable validated WAV (`audio/wav`). Preview text is non-empty, NUL-free,
and capped at 500 characters. The route loads the selected model, verifies runtime streaming and
preset-voice capabilities, confirms the voice is currently reported by the Worker, synthesizes the
short sample, and unloads the model. It creates no Generation Job, Audio Artifact, or History row;
the returned bytes are temporary and are not retained by Core. Model, voice, capability, and Worker
failures use the same stable error envelope as native generation.

Native generation accepts optional `speed`, `pitch`, and `volume` JSON numbers. Core validates finite
values in the ranges speed `0.25..4.0`, pitch `-1.0..1.0`, and volume `0.0..2.0`, then checks each
requested option against the selected Worker's runtime capability facts before queueing and again
after model load. An explicitly requested unsupported option fails with retryable
`capability_unsupported` when request admission fails before a Generation Job is created; if a
queued job later fails capability checks, its stored terminal error is non-retryable because no
Generation retry operation exists. Inline cues remain part of the
text payload rather than a separate request field: clients may display guidance only when the
Worker advertises the runtime `inline_cues` capability, and must not assume cue support.

The one-off Reference Recording seam is `POST /api/v1/references` (multipart `model_id`, one
`file`, and optional `transcript`), `DELETE /api/v1/references/{reference_id}`, and
`POST /api/v1/generations` with exactly one of `voice_id` or `reference_id`. Core returns only an
opaque ID, safe metadata, evidence, state, and expiry. A reference is pinned to its Model
Installation, claimed once, and its ID is redacted after terminal cleanup.

Uploads are limited to 20 MiB before decoding. VieNeu accepts finite WAV/FLAC audio with one or
two channels, positive sample rate, duration no greater than eight seconds, and an optional UTF-8,
NUL-free transcript of at most 2,000 characters. Raw audio, samples, embeddings, absolute paths,
and transcript text never appear in public responses, SQLite, events, or logs.

The model lifecycle routes are:

- `POST /api/v1/models/validate`
- `GET /api/v1/models` and `GET /api/v1/models/{model_id}`
- `POST /api/v1/downloads`, `GET /api/v1/downloads`, and
  `GET /api/v1/downloads/{download_id}`
- `POST /api/v1/downloads/{download_id}/cancel`
- `POST /api/v1/models/{model_id}/remove`
- `GET /api/v1/events`

Creating a download returns `202 Accepted` with its durable queued state. Clients inspect the same
Download Job through the list/detail routes. Removal returns `204 No Content`; it refuses a model
with active generations and always crosses the Core-owned unload lifecycle seam before moving or
deleting managed files.

### Resumable model events

`GET /api/v1/events` returns `text/event-stream` events with globally monotonic integer IDs. Send
the last rendered ID in `Last-Event-ID` to resume exclusively after it. The optional
`download_id` query parameter scopes the feed to one Download Job; a scoped feed closes after
`model.activated`, `download.cancelled`, or `download.failed`.

Core retains a bounded durable event window in SQLite. When a supplied cursor predates that window
or is ahead of the durable cursor, the feed emits one `stream.reset` event and closes. Clients then
refresh `/api/v1/models` and `/api/v1/downloads` before reconnecting with the reset event ID.
Progress data includes `bytes_downloaded`; `total_bytes` and `percentage` are present only when an
adapter reports a total. Event payloads are checked before persistence and never include secrets,
Worker tracebacks, or absolute machine paths. Streaming reads small durable batches rather than
using an unbounded per-client queue.

The artifact-only smoke exercises validation, download activation, exclusive SSE resume,
model listing, and removal through an installed Core while the fake adapter runs from a separate
environment. It places an unprovenanced same-named Worker earlier on `PATH` to prove discovery
selects the verified Worker environment, and checks the managed model files exist after activation
and are absent after removal.

## OpenAI compatibility

`POST /v1/audio/speech` accepts the local compatible subset (`model`, `input`, `voice`, and
`response_format=wav`) and maps it to a Generation Job. It returns Core-owned WAV bytes. The same
route accepts `model=provider:<provider_id>` for the initial remote-provider slice.

The local route also accepts explicit `response_format=json`. It waits for retained synthesis and
alignment, then returns bounded JSON containing alignment units and base64-encoded WAV bytes. The
JSON response is always `audio_format=wav`, `media_type=audio/wav`, and `encoding=base64`; MP3 and
unknown request fields remain unsupported. The default WAV response remains an unaligned binary
`audio/wav` stream for compatibility.

Remote provider profiles are managed through `GET/POST /api/v1/providers`,
`GET/PATCH/DELETE /api/v1/providers/{provider_id}`, and
`POST /api/v1/providers/{provider_id}/validate`. Profiles persist only safe configuration and an
API-key environment-variable name. Provider targets are checked fail-closed: non-loopback URLs
require HTTPS, loopback HTTP is the only HTTP exception, URL credentials/query/fragment components
are rejected, and DNS results must resolve to permitted global addresses. The same policy is
applied immediately before the credentialed Worker request. A disallowed initial target or redirect
target returns `provider_egress_rejected`; a redirect to an otherwise permitted different origin
returns `provider_redirect_rejected`. `POST /v1/audio/speech` accepts
`model=provider:<provider_id>`; other provider failures use stable safe codes and credentials are
never returned.

Compatibility is explicit:

- OpenAI-compatible input is non-empty and capped at 10,000 characters. Core and the direct
  OpenAI-compatible Worker enforce this bound before synthesis.
- Provider WAV responses are capped at 20 MiB. The JSON speech route bounds encoded audio and the
  complete response at 32 MiB and caps alignment units at 10,000 after synthesis/alignment.
  Oversized responses fail safely rather than being returned to clients.
- Unsupported parameters or values return validation errors.
- A requested voice must resolve for the selected Provider Profile or Model Installation.
- Format conversion occurs only for documented supported formats.
- VieNeu does not accept a style parameter.
- Advanced cloning, model lifecycle, and job operations remain native-interface features.

## Errors

All `/api/*` and `/v1/*` failures use a stable error envelope, including unknown routes, request
validation, authentication-category failures, and internal errors:

```json
{
  "error": {
    "code": "model_incompatible",
    "message": "No installed engine adapter supports this repository.",
    "source": "model_registry",
    "retryable": false,
    "correlation_id": "...",
    "details": {}
  }
}
```

Safe messages may be displayed directly. Tracebacks remain in local redacted logs. Validation errors identify fields. Provider failures distinguish authentication, rate limiting, upstream unavailability, and invalid responses.

Each error response also returns its `correlation_id` in the `X-Correlation-ID` response header.
Validation details contain safe locations, messages, and error types without echoing rejected
input. Internal errors return a generic message; their local traceback is logged with the same
correlation ID.

Reference routes use stable codes `reference_request_invalid`, `reference_invalid`,
`reference_not_found`, `reference_capability_unsupported`, and `reference_cleanup_failed`.
Generation rejects an empty or ambiguous source with `reference_request_invalid`; consumed
reference failures are safe terminal errors and are not retried.

VieNeu uses the same public routes: model validation accepts the target repository and optional
revision, downloads select `int8` or `fp32`, model listings expose only Core-owned activated
installations, and `/api/v1/voices` returns runtime preset IDs, labels, and capabilities. A preset
`POST /api/v1/generations` pins the Model Installation and produces a Core-owned 48 kHz WAV Audio
Artifact through the existing binary route. No public route exposes Worker staging, model bytes,
live PCM, or a client-to-Worker connection. Stable VieNeu errors include `model_incompatible`,
`revision_not_found`, `download_failed`, `download_cancelled`, `checksum_mismatch`,
`insufficient_storage`, `model_load_failed`, `model_unload_failed`, `voice_not_found`,
`voice_list_failed`, `synthesis_failed`, `invalid_audio`, and `synthesis_cancelled`.

## Network security

The default listener accepts loopback IP literals only and defaults to `127.0.0.1` without auth.
Non-loopback serving requires `TTS_STUDIO_API_TOKEN_ENV` to name a populated environment variable.
When configured, the Core requires `Authorization: Bearer <token>` on every `/api/*` and `/v1/*`
request and returns the stable `authentication_failed` envelope for missing or invalid credentials.
The CLI forwards the same token automatically. CORS is closed by default. Secrets and
authorization headers are redacted from logs and never returned through settings endpoints.
