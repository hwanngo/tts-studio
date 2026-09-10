import { afterEach, expect, test, vi } from "vitest";
import {
  clearRetention,
  fetchRuntime,
  fetchService,
  fetchSettings,
  installService,
  restartService,
  uninstallService,
  updateSettings,
} from "./api";

afterEach(() => vi.unstubAllGlobals());

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
