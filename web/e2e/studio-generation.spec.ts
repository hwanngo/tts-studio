import { expect, test } from "@playwright/test";

const systemStatus = {
  version: "0.1.0",
  status: "healthy",
  data_dir: "/workspace/.tts-studio",
  workers: [{ engine_id: "fake", status: "ready", message: "" }],
};

const model = {
  id: "model-one",
  repository_id: "fixtures/compatible",
  requested_revision: "main",
  resolved_commit: "a".repeat(40),
  engine_installation_id: "fake@1",
  compatibility_evidence: {},
  runtime_variant: "int8",
  byte_size: 1024,
  cache_path: "models/fixtures--compatible/aaaaaaaa",
  desired_load_state: "loaded",
  observed_load_state: "loaded",
  replica_summary: { ready: 1, active_generations: 0 },
  last_error: null,
  created_at: "2026-09-05T10:00:00Z",
  updated_at: "2026-09-05T10:00:00Z",
};

function completedJob(retainArtifact = true) {
  return {
    id: "generation-one",
    model_id: "model-one",
    engine_id: "fake",
    voice_id: "fake-neutral",
    text: "Hello from a real browser.",
    retain_artifact: retainArtifact,
    state: "completed",
    bytes_written: 1024,
    frame_count: 24000,
    sample_rate: 48000,
    channel_count: 1,
    artifact_id: retainArtifact ? "artifact-one" : null,
    artifact_url: retainArtifact ? "/api/v1/artifacts/artifact-one/audio" : null,
    correlation_id: "correlation-one",
    cancellation_requested: false,
    error: null,
    created_at: "2026-09-05T10:00:00Z",
    updated_at: "2026-09-05T10:00:01Z",
  };
}

function queuedJob(state: "queued" | "cancelled" = "queued") {
  return {
    ...completedJob(true),
    id: "generation-queued",
    state,
    artifact_id: null,
    artifact_url: null,
    bytes_written: state === "queued" ? 0 : 128,
    frame_count: state === "queued" ? 0 : 2400,
    updated_at: "2026-09-05T10:00:01Z",
  };
}

type MockOptions = {
  generationGet?: unknown;
  generationStatus?: number;
  generationPost?: unknown;
  cancelPost?: unknown;
  history?: unknown;
};

async function mockPublicApi(page: import("@playwright/test").Page, options: MockOptions = {}) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/system") return route.fulfill({ json: systemStatus });
    if (url.pathname === "/api/v1/runtime") return route.fulfill({ json: {
      ...systemStatus,
      workers: [{ engine_id: "fake", status: "ready", capabilities: ["streaming_synthesis", "preset_voices", "synthesis_cancellation"] }],
    } });
    if (url.pathname === "/api/v1/settings") return route.fulfill({ json: { retain_audio_by_default: true } });
    if (url.pathname === "/api/v1/saved-voices") return route.fulfill({ json: [] });
    if (url.pathname === "/api/v1/models") return route.fulfill({ json: [model] });
    if (url.pathname === "/api/v1/voices") return route.fulfill({ json: [{ id: "fake-neutral", label: "Neutral", capabilities: ["preset"] }] });
    if (url.pathname === "/api/v1/generations" && request.method() === "GET") {
      if (options.generationStatus && options.generationStatus >= 400) {
        return route.fulfill({ status: options.generationStatus, json: { error: { code: "worker_unavailable", message: "Scheduler unavailable", retryable: true } } });
      }
      return route.fulfill({ json: options.generationGet ?? [] });
    }
    if (url.pathname === "/api/v1/generations" && request.method() === "POST") return route.fulfill({ status: 202, json: options.generationPost ?? completedJob(request.postDataJSON().retain_artifact) });
    if (url.pathname.endsWith("/cancel") && request.method() === "POST") return route.fulfill({ status: 200, json: options.cancelPost ?? queuedJob("cancelled") });
    if (url.pathname === "/api/v1/history") return route.fulfill({ json: options.history ?? [] });
    return route.fulfill({ status: 404, json: { error: { message: "Not found" } } });
  });
}

test("shows form validation in Chromium", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/");

  await expect(page.getByRole("button", { name: "Generate speech" })).toBeEnabled();
  await page.getByRole("button", { name: "Generate speech" }).click();
  await expect(page.getByRole("alert")).toContainText("Enter text to generate speech.");
});

test("shows generation progress and supports cancellation in Chromium", async ({ page }) => {
  await mockPublicApi(page, { generationPost: queuedJob(), cancelPost: queuedJob("cancelled") });
  await page.goto("/");

  await page.getByLabel("Text").fill("A cancellable browser generation.");
  await page.getByRole("button", { name: "Generate speech" }).click();
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();
  await expect(page.getByText("Generation queued")).toBeVisible();
  await page.getByRole("button", { name: "Cancel generation" }).click();
  await expect(page.getByText("Cancelled", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel generation" })).not.toBeVisible();
});

test("generates speech in Chromium and exposes finalized WAV controls", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Create speech" })).toBeVisible();
  await page.getByLabel("Text").fill("Hello from a real browser.");
  await expect(page.getByRole("combobox", { name: "Runtime voice" })).toHaveValue("Neutral");
  await page.getByRole("button", { name: "Generate speech" }).click();

  await expect(page.getByText("Completed")).toBeVisible();
  await expect(page.locator('audio[aria-label="Finalized speech audio"]')).toHaveAttribute("src", "/api/v1/artifacts/artifact-one/audio");
  await expect(page.getByRole("link", { name: "Download WAV" })).toHaveAttribute("download", "tts-studio-artifact-one.wav");
});

test("keeps retention opt-out keyboard reachable in Chromium", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/");

  const retention = page.getByRole("checkbox", { name: /keep in history/i });
  await retention.focus();
  await expect(retention).toBeFocused();
  await page.keyboard.press("Space");
  await expect(retention).not.toBeChecked();
});

test("shows empty Jobs and History states in Chromium", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/jobs");
  await expect(page.getByText("No generation jobs yet")).toBeVisible();

  await page.goto("/history");
  await expect(page.getByText("No retained audio yet")).toBeVisible();
});

test("shows the Jobs error state in Chromium", async ({ page }) => {
  await mockPublicApi(page, { generationStatus: 503 });
  await page.goto("/jobs");

  await expect(page.getByRole("alert")).toContainText("Jobs unavailable");
  await expect(page.getByRole("alert")).toContainText("The speech worker is unavailable");
});
