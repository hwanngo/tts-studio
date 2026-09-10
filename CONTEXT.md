# TTS Studio

Canonical language for TTS Studio's model, engine, voice, job, and artifact concepts.

## Language

**Core**:
The local FastAPI process that owns public interfaces, durable state, orchestration, and the Web UI.
_Avoid_: Backend worker, engine server

**Engine Adapter**:
A distributable integration for one model family or remote-provider protocol, executed by a Worker.
_Avoid_: Plugin, model driver

**Worker**:
An isolated process running one Engine Adapter in its own locked Python environment.
_Avoid_: Core service, backend

**Worker Replica**:
One independently scheduled Worker instance for an Engine Adapter. Each VieNeu replica accepts one active generation.
_Avoid_: Thread, slot

**Model Installation**:
The single locally activated revision and runtime variant of a compatible model repository.
_Avoid_: Model, download

**Voice**:
Either an Engine Adapter's named preset or a saved cloned-voice profile. A one-off reference is not a Voice.
_Avoid_: Speaker, style

**Reference Recording**:
Audio supplied to reproduce delivery and vocal characteristics for a cloned generation or saved Voice.
_Avoid_: Style sample

**Generation Job**:
A tracked request that turns text into an Audio Artifact, possibly delivering audio while it runs.
_Avoid_: Request, synthesis task

**Download Job**:
A tracked, transactional attempt to validate, fetch, and activate a Model Installation.
_Avoid_: Model Installation

**Audio Artifact**:
A managed WAV output associated with a completed Generation Job and its retention state.
_Avoid_: History item, result file

**Streaming PCM**:
Binary signed PCM chunks emitted by a Worker during synthesis; it is an engine-to-Core transport,
not a finalized Audio Artifact.
_Avoid_: Audio Artifact, encoded audio

**WAV finalization**:
Core-owned validation and atomic publication of a managed WAV after all Worker PCM chunks complete.
_Avoid_: Worker file output

**Retention**:
The per-generation decision to keep the finalized Audio Artifact and its history metadata locally.
_Avoid_: SSE event retention

**Provider Profile**:
Non-secret configuration for a remote OpenAI-compatible TTS endpoint, including the environment-variable name holding its credential.
_Avoid_: Cloud model, account
