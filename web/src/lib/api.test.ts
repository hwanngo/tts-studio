import { afterEach, expect, test, vi } from "vitest";
import {
  clearBrowserApiToken,
  clearRetention,
  consumeGenerationPcm,
  createAuthenticatedMediaUrl,
  fetchRuntime,
  fetchService,
  fetchSettings,
  installService,
  restartService,
  setBrowserApiToken,
  subscribeToModelEvents,
  uninstallService,
  updateSettings,
} from "./api";

afterEach(() => {
  clearBrowserApiToken();
  vi.unstubAllGlobals();
});

test("settings, runtime, and service methods use typed public contracts", async () => {
  const settings = { retain_audio_by_default: true, artifact_max_age_days: 30,
    artifact_max_storage_bytes: 1000, api_token_env: "TTS_STUDIO_API_TOKEN",
    host: "127.0.0.1", port: 7860, restart_required: false,
    retention: { retained_count: 2, retained_bytes: 800, max_age_days: 30, max_storage_bytes: 1000 } };
  const runtime = { version: "1.0", host: "127.0.0.1", port: 7860, data_dir: "/tmp/data",
    generation_status: "idle", active_generations: {}, workers: [], storage_accessible: true,
    database_accessible: true };
  const service = { status: "healthy", installed: true, running: true, healthy: true, message: "ok" };
  const operation = { operation: "install", changed: true, message: "installed" };
  const cleared = { deleted: 2, skipped: 0, failed: 0, issues: [] };
  const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = url.endsWith("/retention/clear") ? cleared : url.endsWith("/runtime") ? runtime :
      url.endsWith("/service") ? service : url.includes("/service/") ? operation : settings;
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
  });
  vi.stubGlobal("fetch", fetchMock);

  const fetched = await fetchSettings();
  expect(fetched.retention.retained_count).toBe(2);
  await updateSettings({ retain_audio_by_default: false });
  expect((await clearRetention()).deleted).toBe(2);
  expect((await fetchRuntime()).storage_accessible).toBe(true);
  expect((await fetchService()).healthy).toBe(true);
  expect((await installService()).changed).toBe(true);
  expect((await uninstallService()).operation).toBe("install");
  expect((await restartService()).message).toBe("installed");

  expect(fetchMock.mock.calls.map(([url, init]) => [String(url), init?.method ?? "GET"])).toEqual([
    ["/api/v1/settings", "GET"],
    ["/api/v1/settings", "PATCH"],
    ["/api/v1/settings/retention/clear", "POST"],
    ["/api/v1/runtime", "GET"],
    ["/api/v1/service", "GET"],
    ["/api/v1/service/install", "POST"],
    ["/api/v1/service/uninstall", "POST"],
    ["/api/v1/service/restart", "POST"],
  ]);
  const patch = fetchMock.mock.calls[1][1] as RequestInit;
  expect(JSON.parse(String(patch.body))).toEqual({
    retain_audio_by_default: false,
  });
  expect(JSON.stringify(fetchMock.mock.calls)).not.toContain('"api_token":"');
});

test("stable error envelope propagates through settings methods", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    error: {
      code: "settings_invalid",
      correlation_id: "corr-123",
      details: { field: "port" },
      message: "The settings could not be saved.",
      retryable: false,
      source: "core",
    },
  }), { status: 422 })));
  await expect(fetchSettings()).rejects.toMatchObject({
    code: "settings_invalid",
    correlationId: "corr-123",
    details: { field: "port" },
    message: "The settings could not be saved.",
  });
});

test("memory-only browser session authenticates JSON without putting the token in the URL", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({}), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  setBrowserApiToken("session-secret");

  await fetchSettings();

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/settings",
    expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer session-secret" }),
    }),
  );
  expect(String(fetchMock.mock.calls[0][0])).not.toContain("session-secret");
  expect(localStorage.getItem("tts-studio-api-token")).toBeNull();
});

test("browser session authenticates PCM, event, and media streaming requests", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array([1, 2]));
      controller.close();
    },
  });
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/pcm")) return Promise.resolve(new Response(stream, { status: 200 }));
    if (url.endsWith("/events")) {
      return Promise.resolve(new Response("event: download.progress\ndata: {}\n\n", { status: 200 }));
    }
    return Promise.resolve(new Response(new Uint8Array([3, 4]), {
      status: 200,
      headers: { "Content-Type": "audio/wav" },
    }));
  });
  const createObjectURL = vi.fn(() => "blob:authenticated-audio");
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", { ...URL, createObjectURL });
  setBrowserApiToken("session-secret");

  const chunks: number[][] = [];
  await consumeGenerationPcm("job-one", (chunk) => chunks.push([...chunk]));
  const unsubscribe = subscribeToModelEvents(() => undefined);
  const mediaUrl = await createAuthenticatedMediaUrl("/api/v1/artifacts/artifact-one/audio");
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  unsubscribe();

  expect(chunks).toEqual([[1, 2]]);
  expect(mediaUrl).toBe("blob:authenticated-audio");
  for (const [, init] of fetchMock.mock.calls) {
    expect(init?.headers).toEqual(expect.objectContaining({ Authorization: "Bearer session-secret" }));
  }
});

test("authenticated event reconnect resumes after the last delivered event when its reader fails", async () => {
  const encoder = new TextEncoder();
  let streamController!: ReadableStreamDefaultController<Uint8Array>;
  const interruptedStream = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      controller.enqueue(encoder.encode("id: 42\nevent: download.progress\ndata: {}\n\n"));
    },
  });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(interruptedStream, { status: 200 }))
    .mockResolvedValueOnce(new Response("", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  setBrowserApiToken("session-secret");

  const onEvent = vi.fn();
  const unsubscribe = subscribeToModelEvents(onEvent);
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  await vi.waitFor(() => expect(onEvent).toHaveBeenCalledOnce());
  streamController.error(new Error("connection interrupted"));
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), { timeout: 2_000 });
  unsubscribe();

  expect(fetchMock.mock.calls[1][1]?.headers).toEqual(expect.objectContaining({
    Authorization: "Bearer session-secret",
    "Last-Event-ID": "42",
  }));
});

test("authentication failure clears the browser session before the next request", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      error: {
        code: "authentication_failed",
        correlation_id: "corr-auth",
        details: {},
        message: "Authentication failed.",
        retryable: false,
        source: "authentication",
      },
    }), { status: 401 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  setBrowserApiToken("expired-secret");

  await expect(fetchSettings()).rejects.toMatchObject({ code: "authentication_failed" });
  await fetchSettings();

  expect(fetchMock.mock.calls[0][1]?.headers).toEqual(
    expect.objectContaining({ Authorization: "Bearer expired-secret" }),
  );
  expect(fetchMock.mock.calls[1][1]?.headers).not.toHaveProperty("Authorization");
});

test("a stale authentication failure cannot clear a newer browser token", async () => {
  let resolveFirst!: (response: Response) => void;
  const firstResponse = new Promise<Response>((resolve) => { resolveFirst = resolve; });
  const fetchMock = vi.fn()
    .mockReturnValueOnce(firstResponse)
    .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);

  const staleRequest = fetchSettings();
  setBrowserApiToken("new-session-secret");
  resolveFirst(new Response(JSON.stringify({
    error: {
      code: "authentication_failed",
      correlation_id: "corr-stale",
      details: {},
      message: "Authentication failed.",
      retryable: false,
      source: "authentication",
    },
  }), { status: 401 }));
  await expect(staleRequest).rejects.toMatchObject({ code: "authentication_failed" });
  await fetchSettings();

  expect(fetchMock.mock.calls[1][1]?.headers).toEqual(
    expect.objectContaining({ Authorization: "Bearer new-session-secret" }),
  );
});
