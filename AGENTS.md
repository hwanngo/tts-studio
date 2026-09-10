# TTS Studio agent guide

The foundation vertical slice is implemented. Read [the architecture](docs/architecture.md) and the focused contract document before planning or implementing. Preserve the documented approval gates and update tracked documentation when a requested change alters an approved interface or invariant.

## Architectural invariants

- Keep FastAPI as the sole public server and sole SQLite writer.
- Keep every TTS engine adapter in its own worker process and locked `uv` environment.
- Communicate with workers only through the versioned protobuf/gRPC interface described in [docs/engine-protocol.md](docs/engine-protocol.md).
- Treat adapter capabilities as runtime facts; never assume every engine supports streaming, cloning, precision selection, or cancellation.
- Route the Web UI, CLI, native clients, and OpenAI-compatible endpoint through the same core application behavior.
- Keep managed runtime data under the resolved repository-local `.tts-studio/` directory unless the user supplies `--data-dir` or `TTS_STUDIO_DATA_DIR`.
- Accept arbitrary Hugging Face repository IDs for validation, but allow downloading as runnable models only when an installed adapter reports compatibility.
- Keep remote-provider secrets in environment variables. Persist variable names only.
- Exclude VieNeu's deprecated style parameter. Delivery comes from the preset voice, supported inline cues, or reference recording.

## Source map

The intended repository layout is documented in [docs/architecture.md](docs/architecture.md). Read the focused document for the branch being changed:

- Worker RPC or adapter lifecycle: [docs/engine-protocol.md](docs/engine-protocol.md)
- Public routes or compatibility behavior: [docs/http-api.md](docs/http-api.md)
- Downloads, revisions, cache, or compatibility: [docs/model-management.md](docs/model-management.md)
- Background processes, login startup, storage, or security: [docs/operations.md](docs/operations.md)
- Tooling, test layers, or release checks: [docs/development.md](docs/development.md)
- Canonical project terminology: [CONTEXT.md](CONTEXT.md)

## Working rules

Use `uv` for Python dependency and environment operations and `pnpm` for frontend operations. Commit the generated protobuf runtime modules and `.pyi` typing stubs. After changing the schema, run `uv run python scripts/generate_protocol.py`, then `uv run python scripts/check_protocol.py`. Add commands here only after they exist in repository configuration; configuration and `--help` output remain their source of truth.

Exercise behavior through stable interfaces. Core integration tests use the test-only fake Worker over real gRPC; adapter compliance tests exercise the shared protobuf contract; browser and CLI tests exercise public HTTP behavior. Completion requires the relevant focused tests plus distribution-level checks for packaging or lifecycle changes.

Keep documentation synchronized when an interface, invariant, user workflow, or operational contract changes. Store durable plans, design decisions, and verification reports under the tracked `docs/` tree.
