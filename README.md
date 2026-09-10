# TTS Studio

TTS Studio is a local-first, cross-platform text-to-speech application. A FastAPI Core owns the
public HTTP interface, durable state, orchestration, and Web UI. Each TTS engine runs in its own
isolated Worker process so incompatible model dependencies never share an environment.

The first engine is [VieNeu-TTS v3 Turbo](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo),
optimized for Apple Silicon through its ONNX CPU backend while retaining Windows and Linux support.

## Project status

The application includes model management, deterministic fake generation, Core-owned WAV artifacts,
reference-recording cloning, HTTP/CLI behavior, detached control, per-user login services, Worker
runtime upgrade/rollback, and a Studio UI in English (US) and Vietnamese (Vietnam).

See the [documentation index](docs/README.md) and [architecture](docs/architecture.md).

## Quick start

Install the development dependencies, build the embedded Web UI, and start the Core:

```console
uv sync
pnpm install
uv run python scripts/build_distribution.py
uv run tts serve
```

Open <http://127.0.0.1:7860/>. Runtime data is created under the resolved repository-local
`.tts-studio/` directory unless `--data-dir` or `TTS_STUDIO_DATA_DIR` overrides it. Non-loopback
serving requires a configured bearer token; see [HTTP API security](docs/http-api.md).

## Available and planned commands

The model-management and generation surfaces are implemented in the Core and CLI. The broader
experience is being delivered incrementally:

```console
tts serve
tts serve --background
tts status
tts logs
tts stop
tts service install
tts service status
tts runtime status
tts models validate pnnbao-ump/VieNeu-TTS-v3-Turbo
tts models list
tts models download pnnbao-ump/VieNeu-TTS-v3-Turbo
tts voices list
tts speak "Xin chào" --model vieneu --voice "Adam" --output speech.wav
tts speak --model vieneu --voice "Adam" --file article.txt --output speech.wav
```

## Development

Python dependencies and environments are managed with `uv`; frontend dependencies are managed with
`pnpm`. Core and Worker packages use Python 3.14.

```console
uv sync
uv run pytest
pnpm install
pnpm --dir web test -- --run
pnpm --dir web check
pnpm --dir web build
pnpm --dir web api:check
```

See [docs/development.md](docs/development.md) for the full test layers, protocol checks,
distribution checks, and packaging workflow.
