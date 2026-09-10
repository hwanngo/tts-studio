# Architecture

## System shape

TTS Studio has one public Core and multiple private Workers.

```text
React Web UI ─┐
CLI ──────────┼─ HTTP ─ FastAPI Core ─ gRPC ─ VieNeu Worker replica(s)
Native client ┤          │          └─ gRPC ─ OpenAI-compatible Worker
OpenAI client ┘          ├─ SQLite
                        └─ managed files under .tts-studio/
```

The Core owns the stable public interface, static Web assets, registry, durable jobs, scheduling, history, audio serving, Worker supervision, and all SQLite writes. Clients never contact Workers.

Each Engine Adapter runs as an isolated Worker in a locked `uv` environment. Workers own engine-specific validation, download interpretation, model lifecycle, voice behavior, and synthesis. They advertise capabilities instead of forcing the Core to know engine details. The deterministic fake Worker is a test-only adapter that exercises this seam; normal production discovery exposes only production adapters.

## Intended repository layout

```text
tts-studio/
├── src/tts_studio/          # Core, CLI, persistence, supervision
├── web/                     # React/Vite/Tailwind/shadcn UI
├── proto/                   # canonical Worker protobuf schema
├── workers/
│   ├── vieneu/              # isolated VieNeu package and lock
│   └── openai_compatible/   # generic remote-provider Worker
├── tests/                   # shared, integration, browser, packaging
├── docs/
├── pyproject.toml
├── uv.lock
└── pnpm-workspace.yaml
```

This is a target layout, not the current implementation state.

## Deep modules and seams

- **Core application module:** one interface used by HTTP routes, with persistence, scheduling, and orchestration hidden inside.
- **Engine seam:** the versioned protobuf/gRPC interface. Engine Adapters are replaceable implementations at this seam.
- **Storage module:** resolves all managed paths and transactional file operations from one configured data root.
- **Lifecycle module:** presents one foreground/background/service interface while containing platform adapters for launchd, systemd, and Task Scheduler.
- **Public client seam:** OpenAPI-generated types keep the React UI and CLI aligned with HTTP behavior.

## Primary generation flow

1. A client submits a native or OpenAI-compatible generation request.
2. The Core validates it, resolves model and Voice, creates a Generation Job, and schedules a healthy Worker Replica.
3. The supervisor provisions or starts the Worker if needed, admits it only after authenticated
   protocol/capability validation, and lazily loads the Model Installation. Per-replica health
   supervision replaces unexpected failures with bounded backoff and exposes exhausted failures.
4. The Core calls streamed `Synthesize` over gRPC; cancellation is bounded and an uncooperative
   Worker runtime is quarantined rather than reused.
5. PCM chunks are forwarded with backpressure while the Core builds the managed WAV.
6. Final metadata is persisted as an Audio Artifact unless retention was disabled.
7. Job and model events reach clients through the resumable SSE feed.
8. When requested, Core runs a capability-gated alignment Job against a private read-only WAV
   snapshot and stores timing metadata separately from the successful synthesis and artifact.

## Invariants

- The Core is the only public listener and only SQLite writer.
- Workers are private, capability-driven, authenticated, and replaceable.
- Engine dependencies never enter the Core environment.
- UI, CLI, and compatibility endpoints share Core application behavior.
- Persisted state is reconciled with process and filesystem reality at startup; incomplete
  migration chains fail before advancing SQLite schema state.
- Protocol major/minor compatibility and Worker capability metadata are validated at admission and
  refresh; clients and Core do not infer unsupported engine behavior.
