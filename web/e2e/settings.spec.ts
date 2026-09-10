import { expect, test } from "@playwright/test";

const settings = {
  retain_audio_by_default: true,
  artifact_max_age_days: 30,
  artifact_max_storage_bytes: 4096,
  api_token_env: "TTS_STUDIO_API_TOKEN",
  host: "127.0.0.1",
  port: 7860,
  restart_required: false,
  retention: { retained_count: 2, retained_bytes: 1024, max_age_days: 30, max_storage_bytes: 4096 },
};
const runtime = {
  version: "0.1.0",
  host: "127.0.0.1",
  port: 7860,
  data_dir: "/workspace/.tts-studio",
  generation_status: "idle",
  active_generations: {},
  workers: [{ engine_id: "fake", status: "ready", message: "", capabilities: ["health"], engine_version: "0.2.0", max_concurrency: 1 }],
  storage_accessible: true,
  database_accessible: true,
};
const service = { status: "healthy", installed: true, running: true, healthy: true, message: "Core is healthy" };

type SettingsMockOptions = { settingsStatus?: number; restartStatus?: number; slow?: boolean; deferClear?: boolean };

async function mockSettingsApi(page: import("@playwright/test").Page, options: SettingsMockOptions = {}) {
  let clearRequests = 0;
  let releaseClear: (() => void) | undefined;
  const clearPending = new Promise<void>((resolve) => { releaseClear = resolve; });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (options.slow) await new Promise((resolve) => setTimeout(resolve, 350));
    if (url.pathname === "/api/v1/system") return route.fulfill({ json: { version: "0.1.0", status: "healthy", data_dir: "/workspace/.tts-studio", workers: [] } });
    if (url.pathname === "/api/v1/settings" && options.settingsStatus) {
      return route.fulfill({ status: options.settingsStatus, json: { error: { message: "Settings unavailable", code: "unavailable", retryable: true } } });
    }
    if (url.pathname === "/api/v1/settings" && request.method() === "PATCH") return route.fulfill({ json: settings });
    if (url.pathname === "/api/v1/settings/retention/clear") {
      clearRequests += 1;
      if (options.deferClear) await clearPending;
      return route.fulfill({ json: { deleted: 2, skipped: 0, failed: 0, issues: [] } });
    }
    if (url.pathname === "/api/v1/settings") return route.fulfill({ json: settings });
    if (url.pathname === "/api/v1/runtime") return route.fulfill({ json: runtime });
    if (url.pathname === "/api/v1/service/restart" && options.restartStatus) return route.fulfill({ status: options.restartStatus, json: { error: { message: "Restart unsupported in this mode.", code: "service_unsupported", retryable: false } } });
    if (url.pathname === "/api/v1/service") return route.fulfill({ json: service });
    return route.fulfill({ status: 404, json: { error: { message: "Not found" } } });
  });
  return { releaseClear: () => releaseClear?.(), getClearRequests: () => clearRequests };
}

test("renders Settings responsively, synchronizes themes, and moves focus after navigation", async ({ page }) => {
  await mockSettingsApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Create speech" })).toBeVisible();
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.locator(".nav-item-active")).toContainText("Settings");
  await expect(page).toHaveTitle("Settings · TTS Studio");
  await expect(page.locator("main")).toBeFocused();
  await expect(page.getByLabel("Resolved data directory")).toHaveValue("/workspace/.tts-studio");
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", await page.evaluate(() => document.documentElement.clientWidth));

  await page.getByRole("button", { name: "Dark theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("radio", { name: "Light" }).check();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.locator("main")).toHaveJSProperty("scrollWidth", await page.evaluate(() => document.documentElement.clientWidth));
  await expect(page.getByLabel("Resolved data directory")).toBeVisible();
});

test("reports successful save and clear results through public requests", async ({ page }) => {
  let patchRequests = 0;
  let clearRequests = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/settings") && request.method() === "PATCH") patchRequests += 1;
    if (request.url().endsWith("/settings/retention/clear")) clearRequests += 1;
  });
  await mockSettingsApi(page);
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  await page.getByLabel("Maximum age (days)").fill("7");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status")).toContainText("Settings saved");
  await expect(page.getByLabel("API token environment variable")).toHaveAttribute("readonly", "");
  expect(patchRequests).toBe(1);

  await page.getByRole("button", { name: "Clear retained history" }).click();
  await page.getByRole("button", { name: "Confirm clear history" }).click();
  await expect(page.getByRole("status")).toContainText("deleted 2");
  expect(clearRequests).toBe(1);
});

test("shows loading and error states without exposing secrets or paths", async ({ page }) => {
  await mockSettingsApi(page, { slow: true });
  await page.goto("/settings");
  await expect(page.getByRole("status", { name: "Settings loading" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByText("TTS_STUDIO_API_TOKEN")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("secret-value");

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mockSettingsApi(page, { settingsStatus: 503 });
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Settings unavailable");
  await expect(page.getByRole("button", { name: "Save settings" })).toBeDisabled();
});

test("keeps destructive controls pending and prevents duplicate clear requests", async ({ page }) => {
  const pending = await mockSettingsApi(page, { deferClear: true });
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.getByRole("button", { name: "Clear retained history" }).click();
  await page.getByRole("button", { name: "Confirm clear history" }).click();
  await expect(page.getByRole("status")).toContainText("Clearing retained history");
  await expect(page.getByRole("button", { name: "Confirm clear history" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await expect.poll(() => pending.getClearRequests()).toBe(1);
  pending.releaseClear();
  await expect(page.getByRole("status")).toContainText("deleted 2");
});

test("requires confirmation and reports unsupported lifecycle operations", async ({ page }) => {
  await mockSettingsApi(page, { restartStatus: 409 });
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  await page.getByRole("button", { name: "Clear retained history" }).click();
  await expect(page.getByText("Confirm clearing retained history?")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm clear history" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Clear retained history" })).toBeVisible();

  await page.getByRole("button", { name: "Restart service" }).click();
  await expect(page.getByText("Confirm restarting service?")).toBeVisible();
  await page.getByRole("button", { name: "Confirm restart" }).click();
  await expect(page.getByRole("alert")).toContainText("unsupported");
  await expect(page.getByRole("button", { name: "Restart service" })).toHaveCount(1);
});
