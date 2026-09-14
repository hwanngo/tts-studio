# Generation remediation verification

Date: 2026-09-14
Scope: generation request validation, terminal privacy, and explicit retry.

## Result and contract

- One Core constant in `generation/limits.py` limits native generation text, OpenAI speech input,
  and GenerationService admission to 10,000 Unicode characters. Existing empty-text native error
  envelopes remain compatible. Preview retains its narrower existing 500-character limit.
- Native/OpenAI creation bodies are counted while streaming before JSON parsing and bounded to
  128 KiB. Both ordinary and chunked over-budget bodies return the common
  `413 request_body_too_large` envelope. The bound fits 10,000 escaped Unicode characters.
- Registry terminal transitions redact non-retained text in the same SQL update that sets
  completed/failed/cancelled state. The public string field remains present as `""`.
  Migration 013 backfills old terminal non-retained records while preserving retained and active
  records; no Worker schema change is involved.
- `POST /api/v1/generations/{job_id}/retry` creates one successor for eligible retained FAILED
  jobs with retryable errors and reusable preset/Saved Voice sources. A durable unique
  `retry_of` relation plus service serialization makes repeated/concurrent requests idempotent
  across completion and Core restart. Ordinary creation performs current admission checks again.
- Retry preserves text, model, source, retention, and speed/pitch/volume while assigning a new
  job ID and correlation ID. The original failed attempt stays immutable. A failed successor can
  be retried using its own ID. Nothing retries automatically or replays already-exposed PCM.
- Completed/cancelled/active/permanent failures, redacted jobs, one-off references, incomplete
  cleanup, and artifact-owning attempts are rejected. The registry additionally checks artifact
  ownership independently of the job's artifact pointer inside its retry insertion transaction.
  Unknown jobs return 404; ineligible jobs return 409.
- Worker retryability is retained only for eligible ordinary failed attempts after cleanup.
  Existing shutdown/startup cleanup flags keep their established recovery meaning: repair and
  restart Core. They do not authorize generation retry or reuse of consumed references.
- A queued successor committed before a Core crash is scheduled by ordinary startup recovery;
  retry requests return that same successor.
- Core remains the sole public server/SQLite writer; Workers and protocol are unchanged.

## TDD evidence

Tests were written and executed before implementing the associated behavior.

1. Service RED:
   `uv run pytest tests/generation/test_service.py -k 'oversized_text or terminal_generation_text or retry_creates or retry_refuses or without_native_retry' -q`
   yielded **14 failed, 3 passed**. Oversized text incorrectly persisted; non-retained terminal
   text remained visible; Worker retryability was suppressed; the retry service method was absent.
   Retained-text controls already passed.
2. HTTP RED:
   `uv run pytest tests/server/test_generation_routes.py -k 'text_limit_before or body_limit or retry_route or api_redacts' -q --tb=short`
   yielded **7 failed**. Oversized native text returned 202, oversized bodies reached JSON
   validation instead of 413, retry was 404, and terminal non-retained text remained exposed.
3. Migration RED:
   `uv run pytest tests/storage/test_migrations.py -k upgrade_redacts -q --tb=short`
   yielded **1 failed** because old non-retained terminal text survived upgrade.
4. Initial GREEN: selected service/migration cases **18 passed**; HTTP cases **7 passed**.
5. Additional ownership RED:
   `uv run pytest tests/generation/test_service.py -k unlinked_artifact -q --tb=short`
   yielded **1 failed** because an inconsistent artifact pointer escaped the stable service error.
   The registry already refused creating a successor; translating that refusal fixed the public
   behavior. This case passes in the final full gate.
6. Additional coverage verifies exactly 10,000 astral Unicode characters are accepted, transient
   private failures are not advertised as retryable, options survive retry, capability failures
   prevent successor creation, and queued successor crash recovery produces one retained artifact.

## Final verification

The combined focused gate passed **282 tests**, with **3 skips** for unavailable identity-bound
unlink primitives on this host:

```console
uv run pytest tests/generation tests/server/test_generation_routes.py tests/server/test_openai_routes.py tests/server/test_errors.py tests/storage/test_migrations.py tests/integration/test_generation.py tests/integration/test_reference_cloning.py tests/integration/test_alignment.py tests/integration/test_phase3_generation_artifact.py tests/openapi -q --tb=short -rs
```

Distribution checks passed **4 tests**, with **1 optional offline Worker dependency skip**:

```console
uv run pytest tests/packaging/test_build_distribution.py tests/packaging/test_wheel.py tests/packaging/test_tool_install.py -q --tb=short
```

The installed-artifact smoke builds and installs release artifacts, exercises the real public
server and isolated fake Worker, and verifies generation/cancellation/recovery. Its final extended
run also asserts terminal text redaction directly in installed SQLite and explicit idempotent retry
after killing and restarting Core. The final extended smoke passed: **1 passed in 13.82s**
(`uv run pytest tests/integration/test_phase3_generation_artifact.py -q --tb=short -rs`).

Other checks passed:

```console
uv run python scripts/generate_openapi_client.py
uv run python scripts/check_openapi.py
uv run python scripts/check_protocol.py
uv run mypy --platform linux
uv run ruff check src/tts_studio/generation src/tts_studio/server/routes/generation.py src/tts_studio/server/routes/openai.py src/tts_studio/server/errors.py src/tts_studio/storage/db.py tests/generation/test_service.py tests/server/test_generation_routes.py tests/storage/test_migrations.py tests/integration/test_generation.py tests/integration/test_phase3_generation_artifact.py
git diff --check
```

Strict mypy now runs directly and rejects every diagnostic. OpenAPI TypeScript paths include retry;
generated Python response types were regenerated without an interface change.

## Compatibility and limits

- Existing cancellation fixtures used >10,000 characters solely to keep synthesis active. They
  now stay within the public contract while retaining real Worker cancellation/crash coverage.
- Privacy redaction is logical SQLite redaction, not forensic erasure from old backups or storage.
- Cleanup failure recovery remains an operator repair/restart workflow; unsafe artifacts and
  consumed one-off references never become retry input.
- Live providers, model downloads, optional VieNeu hardware, and native Windows containment were
  not exercised by this verification. Existing platform/optional dependency skips are reported
  rather than claimed passed.
- Documentation synchronized: HTTP, operations, engine protocol prose, development status,
  migration expectations, generated client paths, and this tracked verification record.
