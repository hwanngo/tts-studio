# Model management

## Compatibility first

Users may enter any Hugging Face repository ID, but a repository becomes downloadable as a runnable Model Installation only when an installed Engine Adapter validates it. The fake adapter provides deterministic compatible and incompatible fixtures for this workflow; VieNeu repository acquisition is handled by its installed adapter.

The UI must say `No compatible adapter` for unsupported repositories. It must not imply that an arbitrary TTS repository can run merely because files can be fetched. VieNeu acquisition is enabled only for `pnnbao-ump/VieNeu-TTS-v3-Turbo`; the requested or default model revision resolves to an immutable commit and the adapter-owned codec is pinned to `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX@ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae`.

## Identity and revisions

A Model Installation is identified by its repository and the one active resolved commit. A request without a revision resolves the default branch once to an immutable commit. V1 permits one installed revision per repository. Update checks and replacements are explicit.

The registry retains:

- Repository ID and requested revision
- Resolved immutable commit
- Compatible Engine Adapter and validation evidence
- Runtime variant such as INT8 or FP32
- Adapter-produced manifest and checksums where available
- Downloaded byte size and resolved cache path
- Desired and observed loaded state
- Replica association and last structured error

## Transactional download

1. Create a durable Download Job.
2. Ask candidate Engine Adapters to validate compatibility.
3. Allocate a path-safe staging directory.
4. Let the selected Adapter stream download progress and build its manifest.
5. Verify required files, manifest, size, and available storage.
6. Open the Worker-owned staging tree through descriptor-relative/no-follow reads where supported,
   copy only regular files into a Core-created private promotion directory under `models/`, and
   validate the copied snapshot against the verified manifest. Platforms without a safe no-follow
   primitive fail closed.
7. Atomically rename only the Core-created promotion directory into `models/<model_id>`.
8. Retire the previous revision only after successful activation and unload.

Cancellation leaves no active partial installation. Startup recovery handles abandoned staging directories according to recorded state. Removal refuses or coordinates unload before deleting files.

The Core persists each state transition in order: `queued`, `validating`, `downloading`,
`verifying`, `activating`, then `completed`. `cancelled` and `failed` are terminal. Adapter manifests
do not authorize activation by themselves: Core independently checks repository/revision/variant
identity, the complete staged file set, required files, byte counts, and SHA-256 values. Worker-
controlled staging is never renamed into `models/`; the final Core-promotion-to-model rename is
atomic within the managed data root.

Immediately before activation, Core independently checks filesystem free space for the verified
byte size and applies the configured managed-model byte limit, accounting for the revision being
replaced. Capacity failures remain durable on the Download Job as retryable `insufficient_storage`
errors; adapter-provided capacity claims are not trusted as the Core check.

Replacement and removal are serialized inside Core. A replacement keeps the previous directory and
registry row until the new staging tree verifies, then unloads the previous installation before
activation. A failed database commit rolls the new directory back. Removal first unloads, renames
the model into managed retirement staging, commits metadata deletion, and then discards the retired
tree; a metadata failure restores the directory. Startup recovery fails abandoned active jobs with
`recovery_required`, removes their managed partial data, restores interrupted retirements when the
registry still references them, and removes unreferenced Core promotion directories. A
retirement-shaped name is never enough to authorize restoration: recovery requires a real,
unredirected, non-reparse directory whose filesystem identity remains stable across promotion.

## Durable progress events

Validation results and Download Job checkpoints are appended to a bounded SQLite event log with
globally increasing IDs. The event types are `model.validation`, `download.queued`,
`download.validating`, `download.progress`, `download.activating`, `model.activated`,
`download.cancelled`, and `download.failed`. A recovered interrupted job also produces the terminal
failure event.

Clients resume the SSE feed with `Last-Event-ID`; replay is exclusive so an activation is not
delivered twice after a normal reconnect. A cursor outside retained history produces
`stream.reset`, which tells the client to refresh the current model and Download Job snapshots.
Known totals include a percentage, while unknown totals omit both fields. Slow clients consume
bounded database batches and cannot build an unbounded in-memory event queue.

## VieNeu acquisition contract

The VieNeu Worker stages a complete snapshot below `staging/<download-id>/` with
`backbone/int8/`, `backbone/fp32/`, `codec/`, and `cloning/`. Both ONNX graph variants,
configuration/tokenizer, codec files, and approved cloning assets are required regular files;
symlinks, traversal, redirected paths, duplicate entries, and extra staged files fail closed. The
manifest records every relative path, byte count, and SHA-256. Core independently verifies the
repository, immutable revision, selected `int8`/`fp32` variant, exact file set, checksums, and
capacity before copying into a private promotion directory and atomically publishing
`models/<id>`. Worker-owned staging is never renamed into the active model directory.

After activation, loading, runtime Voice discovery, preset synthesis, and reference operations use
only the managed snapshot and local Worker environment. No network access is required after
activation. Acquisition is the only operation allowed to resolve repository metadata or download
bytes, and ordinary tests never invoke it against Hugging Face.

## Runtime variants

The fake adapter exposes deterministic `INT8` and `FP32` fixture variants. VieNeu maps
`int8` to `onnx_int8` and `fp32` to `onnx_update`, passes `backend="onnx"`, and emits 48 kHz mono
S16LE. Switching variants unloads the Worker before reload. The UI shows the download/storage
implication and current observed runtime variant.

## Artifact verification

The artifact smoke explicitly builds the root, protocol, Worker SDK, fake-adapter, and
VieNeu Worker wheels for test isolation, installs Core and the fake Worker into separate temporary
environments, and confirms that Core imports the generated client without importing Worker packages.
The fake adapter is a deterministic test fixture, not a production-discovered engine. Installed
Worker discovery verifies the uv-managed virtual environment, package metadata, console entry point,
and protocol/Worker SDK artifacts, so an earlier same-named executable on `PATH` is ignored. The
smoke validates `fixtures/compatible` and `fixtures/incompatible`, downloads and activates the
compatible fixture, confirms the managed model directory contains its expected regular files,
resumes its durable SSE events with `Last-Event-ID`, and confirms the managed directory is absent
after removal.

## UI requirements

Model screens show compatibility, repository, resolved revision, variant, byte progress, total size when known, cache location, loaded state, replica status, update availability, and actionable errors. Progress uses determinate bytes when the source exposes a total and an explicit indeterminate state otherwise.
