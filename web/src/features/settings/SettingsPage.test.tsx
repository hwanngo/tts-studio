import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import i18n from "../../i18n";
import { SettingsPage } from "./SettingsPage";

const settings = {
  retain_audio_by_default: true,
  artifact_max_age_days: 30,
  artifact_max_storage_bytes: 1000,
  api_token_env: "TTS_STUDIO_API_TOKEN",
  host: "127.0.0.1",
  port: 7860,
  restart_required: false,
  retention: { retained_count: 2, retained_bytes: 800, max_age_days: 30, max_storage_bytes: 1000 },
};
const runtime = {
  version: "1.0.0",
  host: "127.0.0.1",
  port: 7860,
  data_dir: "/safe/.tts-studio",
  generation_status: "idle",
  active_generations: {},
  workers: [{
    engine_id: "fake",
    status: "ready",
    message: "Worker is ready.",
    capabilities: ["streaming_synthesis", "preset_voices"],
    engine_version: "0.2.0",
    max_concurrency: 1,
  }],
  storage_accessible: true,
  database_accessible: true,
};
const service = { status: "healthy", installed: true, running: true, healthy: true, message: "opaque service detail" };

function response(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

function renderPage(onThemeChange = vi.fn()) {
  return render(
    <MemoryRouter>
      <SettingsPage theme="light" onThemeChange={onThemeChange} />
    </MemoryRouter>,
  );
}

beforeEach(async () => {
  await i18n.changeLanguage("en-US");
  vi.restoreAllMocks();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/settings") && init?.method === "PATCH") return response(settings);
    if (url.endsWith("/settings/retention/clear")) return response({ deleted: 2, skipped: 0, failed: 0, issues: [] });
    if (url.endsWith("/runtime")) return response(runtime);
    if (url.endsWith("/service")) return response(service);
    if (url.endsWith("/service/restart")) return response({ error: { code: "service_unsupported", message: "Restart unsupported in this mode.", retryable: false } }, 409);
    return response(settings);
  }));
});

test("localizes settings labels when the locale changes", async () => {
  await i18n.changeLanguage("vi-VN");
  renderPage();
  expect(await screen.findByRole("heading", { name: "Cài đặt" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Giao diện" })).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("renders independent settings, runtime, and lifecycle sections", async () => {
  renderPage();
  expect(screen.getByRole("status", { name: /settings/i })).toBeVisible();
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Appearance" })).toBeVisible();
  expect(screen.getByRole("heading", { name: /Retention and privacy/i })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Runtime information" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Networking" })).toBeVisible();
  expect(screen.getByRole("heading", { name: /Service and lifecycle/i })).toBeVisible();
  expect(screen.getByLabelText("Resolved data directory")).toHaveValue("/safe/.tts-studio");
  expect(screen.getByRole("heading", { name: "fake Worker" })).toBeVisible();
  expect(screen.getByText("Worker is ready.")).toBeVisible();
  expect(screen.getByText("The local service is healthy (Healthy).")).toBeVisible();
  expect(screen.queryByText("opaque service detail")).not.toBeInTheDocument();
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByText("Dịch vụ trên máy đang hoạt động bình thường (Khỏe mạnh).")).toBeVisible();
  expect(screen.queryByText("opaque service detail")).not.toBeInTheDocument();
  await i18n.changeLanguage("en-US");
  expect(screen.getByText("Idle")).toBeVisible();
  expect(screen.getAllByText("Accessible")).toHaveLength(2);
  expect(screen.getByText("Streaming synthesis")).toBeVisible();
  expect(screen.getByText("Preset voices")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open Overview" })).toHaveAttribute("href", "/overview");
  expect(screen.getByRole("link", { name: "Manage Models" })).toHaveAttribute("href", "/models");
});

test("saves editable retention settings without sending restart-bound token configuration", async () => {
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByLabelText("Retain generated audio by default"));
  fireEvent.change(screen.getByLabelText("Maximum age (days)"), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/saved/i));
  const patch = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url).endsWith("/settings") && init?.method === "PATCH");
  expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
    retain_audio_by_default: false,
    artifact_max_age_days: 7,
    artifact_max_storage_bytes: 1000,
  });
});

test("updates saved notices when the locale changes", async () => {
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Settings saved successfully."));
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByRole("status")).toHaveTextContent("Đã lưu cài đặt.");
  await i18n.changeLanguage("en-US");
});

test("translates lifecycle operation values in confirmation and success messages", async () => {
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByRole("button", { name: "Install service" }));
  expect(screen.getByText("Confirm install service?")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Confirm install" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("install service completed successfully."));
});

test("uses a localized fallback for unknown worker messages", async () => {
  vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/runtime")) return response({ ...runtime, workers: [{ ...runtime.workers[0], message: "opaque server detail" }] });
    if (url.endsWith("/service")) return response(service);
    return response(settings);
  });
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  expect(screen.getByText("Worker status message unavailable.")).toBeVisible();
  expect(screen.queryByText("opaque server detail")).not.toBeInTheDocument();
});

test("requires confirmation before clearing retention", async () => {
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  const clear = screen.getByRole("button", { name: "Clear retained history" });
  fireEvent.click(clear);
  expect(screen.getByText(/confirm clearing/i)).toBeVisible();
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/retention/clear"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Confirm clear history" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/deleted 2/i));
});

test("requires confirmation before restart and reports unsupported lifecycle state", async () => {
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByRole("button", { name: "Restart service" }));
  expect(screen.getByText(/confirm restarting/i)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Confirm restart" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Service management is not supported"));
});

test("disables persisted settings controls when the initial settings request fails", async () => {
  vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/settings")) return response({ error: { code: "unavailable", message: "Settings unavailable", retryable: true } }, 503);
    if (url.endsWith("/runtime")) return response(runtime);
    return response(service);
  });
  renderPage();
  expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
  expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Clear retained history" })).toBeDisabled();
  expect(screen.getByLabelText("API token environment variable")).toHaveAttribute("readonly");
  expect(screen.getByLabelText("API token environment variable")).toHaveValue("Not configured");
});

test("keeps destructive confirmation visible and pending while cleanup is in flight", async () => {
  let resolveClear: ((value: Response) => void) | undefined;
  vi.mocked(fetch).mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/settings/retention/clear")) return new Promise<Response>((resolve) => { resolveClear = resolve; });
    const url = String(input);
    if (url.endsWith("/runtime")) return response(runtime);
    if (url.endsWith("/service")) return response(service);
    return response(settings);
  });
  renderPage();
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByRole("button", { name: "Clear retained history" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm clear history" }));
  expect(screen.getByRole("status")).toHaveTextContent("Clearing retained history");
  expect(screen.getByRole("button", { name: "Confirm clear history" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  resolveClear?.(await response({ deleted: 1, skipped: 0, failed: 0, issues: [] }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/deleted 1/i));
});

test("theme control invokes the shared setter and keeps read-only fields non-editable", async () => {
  const onThemeChange = vi.fn();
  renderPage(onThemeChange);
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
  expect(onThemeChange).toHaveBeenCalledWith("dark");
  expect(screen.getByLabelText("Effective host")).toHaveAttribute("readonly");
  expect(screen.getByLabelText("Effective port")).toHaveAttribute("readonly");
  expect(screen.getByLabelText("Resolved data directory")).toHaveAttribute("readonly");
  expect(screen.getByLabelText("API token environment variable")).toHaveAttribute("readonly");
  expect(screen.queryByLabelText("Secret value")).not.toBeInTheDocument();
});
