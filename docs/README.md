# TTS Studio documentation

This directory contains the tracked project contract: architecture, public interfaces, operational
rules, development workflow, and decisions. The implementation includes local and remote
OpenAI-compatible provider management, detached Core control, service lifecycle support,
distribution tooling, and the bilingual Web UI.

## Start here

1. [Architecture](architecture.md) — system shape, ownership, seams, and invariants
2. The focused contract for the area being changed

## Focused references

- [Engine protocol](engine-protocol.md): versioned protobuf/gRPC Worker contract and lifecycle
- [HTTP interface](http-api.md): native and OpenAI-compatible public behavior
- [Model management](model-management.md): compatibility, revisions, downloads, and cache
- [Operations](operations.md): data directory, processes, startup, networking, and security
- [Development](development.md): repository layout, dependency tools, testing, and distribution
- [ADRs](adr/): costly architecture decisions and their rationale

Canonical domain terms live in [CONTEXT.md](../CONTEXT.md).

The roadmap is a status and sequencing document, not an executable plan. Implementation details
belong in the focused contract documents and in the tests that exercise the public seams.
