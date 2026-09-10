# Keep the fake Worker as a test-only adapter

## Decision

Retain the fake Worker as a locked, real-gRPC Engine Adapter for deterministic integration,
packaging, and protocol-compliance tests. Exclude it from default Core adapter discovery and
default release artifacts.

Test workflows opt in explicitly with `discover_adapters(..., include_test_adapters=True)` or
`scripts/build_distribution.py --include-test-adapters`.

## Rationale

The fake Worker exercises the same authenticated protobuf/gRPC seam as VieNeu without requiring a
model download, native runtime, hardware cache, or network access. Removing it would make Core
tests depend on VieNeu availability and would weaken deterministic coverage of lifecycle,
download, generation, cancellation, recovery, and packaging behavior.

It is not a product engine. Presenting its deterministic fixtures through normal discovery would
make a test adapter appear as a user-facing runtime and would unnecessarily ship it in ordinary
release artifacts.

## Consequences

- Production Core discovery returns only installed production adapters, currently VieNeu.
- Test code explicitly requests the fake adapter or injects its descriptor.
- Normal distribution builds contain Core, protocol, Worker SDK, and production Worker artifacts.
- Packaging and distribution smoke tests request the fake Worker bundle explicitly.
- The fake Worker remains versioned against the shared protocol and continues to run in its own
  locked environment.
