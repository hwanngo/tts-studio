# Task 1 verification report

## Delivered

- The FastAPI Core now validates `Host` against its configured listener authority and rejects DNS names or wrong ports. Wildcard listeners accept only numeric addresses from their configured IP family and port.
- Browser mutations with an `Origin` must use the accepted authority's exact `http` origin. Clients without `Origin`, including CLI and native clients, remain supported.
- The Web UI uses a memory-only bearer session. It prompts after authentication failure, sends the bearer header for JSON, PCM, authenticated event streams, and media, and clears the token on a current-session `401`.
- Protected retained audio is fetched to revocable object URLs for playback and download. The token is never placed in a URL or browser storage.
- The public HTTP contract documents the Host, Origin, and browser-session behavior.

## Test-first evidence

RED — before the final App-shell adjustment:

```text
pnpm --dir web test --run src/lib/api.test.ts src/app/App.test.tsx src/features/studio/StudioPage.test.tsx src/features/history/HistoryPage.test.tsx

FAIL src/app/App.test.tsx > prompts for a bearer token and retries the Core request without persisting the secret
Expected element with focus: <input ... name="api-token" ...>
Received element with focus: <body>
```

The failing behavior was the absent focus on the authentication token input (and the test also required the standard `current-password` autocomplete hint). The production adjustment adds a ref-driven focus effect and the browser credential hint.

GREEN:

```text
pnpm --dir web test --run src/app/App.test.tsx
13 passed

pnpm --dir web test --run
14 files passed, 118 tests passed

pnpm --dir web check
tsc --noEmit && locale key parity check passed

pnpm --dir web build
vite build succeeded

uv run pytest tests/server/test_authentication.py tests/server/test_errors.py tests/server/test_web_assets.py -q
17 passed

uv run ruff check src/tts_studio/server/app.py src/tts_studio/server/errors.py tests/server/test_authentication.py
All checks passed

git diff --check
passed
```

## Scope and concerns

No token values are persisted by this feature. Native `EventSource` cannot attach bearer headers, so authenticated model events intentionally use a fetch-based SSE consumer. The Host middleware retains a narrowly scoped `http://test` ASGI-transport exception so pre-existing in-process route tests continue to exercise public routes; real network listeners do not receive that server scope.

## Review remediation evidence

Added two regression tests before the implementation adjustment:

- Authenticated event reconnect delivers event ID 42, fails the stream reader, and asserts that the next authenticated request sends Last-Event-ID: 42.
- A delayed initial 401 envelope arrives after a newer bearer login succeeds and verifies that the login gate remains closed.

RED:

    pnpm --dir web test --run src/lib/api.test.ts src/app/App.test.tsx

    FAIL authenticated event reconnect resumes after the last delivered event when its reader fails
    Expected Last-Event-ID: "42"
    Received headers without Last-Event-ID

GREEN:

    pnpm --dir web test --run src/lib/api.test.ts src/app/App.test.tsx
    2 files passed, 21 tests passed

    pnpm --dir web test --run
    14 files passed, 120 tests passed

    pnpm --dir web check
    tsc --noEmit and locale key parity check passed

    pnpm --dir web build
    vite build succeeded

    git diff --check
    passed

The event consumer now persists the cursor as each parsed ID arrives, rather than only after the reader completes. The App increments a request version whenever authentication invalidates state or a new login starts; only the current request may update the system status or authentication gate.
