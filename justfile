set dotenv-load := false

python := env_var_or_default("UV_PYTHON", "3.14")
host := env_var_or_default("TTS_HOST", "127.0.0.1")
port := env_var_or_default("TTS_PORT", "7860")

# Show all available commands.
default:
    @just --list

# Install locked Python and frontend dependencies.
install:
    uv sync
    pnpm install --frozen-lockfile

# Start Core without test-only adapters.
core host=host port=port:
    uv run --python {{python}} tts serve --host {{host}} --port {{port}}

# Start Core with the deterministic fake Worker for local development.
core-test host=host port=port:
    uv run --python {{python}} tts serve --include-test-adapters --host {{host}} --port {{port}}

# Start the frontend development server.
web:
    pnpm --dir web dev

# Run the frontend production build.
web-build:
    pnpm --dir web build

# Run frontend tests.
web-test:
    pnpm --dir web test -- --run

# Run frontend typecheck and repository checks.
web-check:
    pnpm --dir web check

# Run the complete Python test suite, including known repository debt.
test:
    UV_PYTHON={{python}} uv run --python {{python}} pytest -q

# Run stable Core API, alignment, migration, and lifecycle coverage.
test-core:
    UV_PYTHON={{python}} uv run --python {{python}} pytest tests/integration/test_alignment.py tests/server/test_generation_routes.py tests/server/test_openai_routes.py tests/server/test_runtime_api.py tests/storage/test_migrations.py tests/test_runtime.py -q

# Run the Worker lifecycle and adapter contract suites.
test-workers:
    UV_PYTHON={{python}} uv run --python {{python}} pytest tests/workers/test_supervisor.py -q
    uv run --project workers/fake --frozen pytest workers/fake/tests -q
    uv run --project workers/openai_compatible --frozen pytest workers/openai_compatible/tests -q
    uv run --project workers/vieneu --frozen pytest workers/vieneu/tests -q

# Run one Worker project's tests, for example: just worker-test fake.
worker-test worker="fake":
    uv run --project workers/{{worker}} --frozen pytest workers/{{worker}}/tests -q

# Regenerate protobuf runtime modules and typing stubs.
protocol-generate:
    UV_PYTHON={{python}} uv run --python {{python}} python scripts/generate_protocol.py

# Check generated protobuf modules for drift.
protocol-check:
    UV_PYTHON={{python}} uv run --python {{python}} python scripts/check_protocol.py

# Check generated OpenAPI clients for drift.
openapi-check:
    UV_PYTHON={{python}} uv run --python {{python}} python scripts/check_openapi.py

# Run strict mypy across Core, shared packages, and all Worker source.
mypy:
    UV_PYTHON={{python}} uv run --python {{python}} mypy --platform linux

# Run Ruff across the repository.
lint:
    UV_PYTHON={{python}} uv run --python {{python}} ruff check .

# Check formatting without modifying files.
format-check:
    UV_PYTHON={{python}} uv run --python {{python}} ruff format --check .

# Build all distribution artifacts.
build:
    UV_PYTHON={{python}} uv run --python {{python}} python scripts/build_distribution.py

# Run packaging and installed-wheel checks.
package-test:
    UV_PYTHON={{python}} uv run --python {{python}} pytest tests/packaging/test_build_distribution.py tests/packaging/test_wheel.py -q

# Run frontend, generated-artifact, type, and lint checks.
check: protocol-check openapi-check mypy lint web-check web-test web-build
    git diff --check

# Run the fast handoff verification suite for pointing a frontend at Core.
handoff: test-core test-workers check package-test

# Run the full local verification matrix.
verify: test test-workers check package-test
    UV_PYTHON={{python}} uv run --python {{python}} python scripts/build_distribution.py
    git diff --check

# Short aliases.
alias i := install
alias c := core
alias ct := core-test
alias w := web
alias t := test
alias tc := test-core
alias tw := test-workers
alias b := build
alias v := verify
