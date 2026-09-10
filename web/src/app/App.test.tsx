import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import i18n from "../i18n";
import { fetchSystemStatus } from "../lib/api";
import { App } from "./App";

const systemStatus = {
  version: "0.1.0",
  status: "healthy" as const,
  data_dir: "/workspace/.tts-studio",
  workers: [
    {
      engine_id: "vieneu",
      status: "ready" as const,
      message: "",
    },
  ],
};

beforeEach(async () => {
  localStorage.clear();
  await i18n.changeLanguage("en-US");
  document.documentElement.removeAttribute("data-theme");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const body = String(input).endsWith("/settings") ? {
        retain_audio_by_default: true,
        artifact_max_age_days: null,
        artifact_max_storage_bytes: null,
        api_token_env: null,
        host: "127.0.0.1",
        port: 7860,
        restart_required: false,
        retention: { retained_count: 0, retained_bytes: 0, max_age_days: null, max_storage_bytes: null },
      } : systemStatus;
      return Promise.resolve(new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }),
  );
});

test("opens on the Studio workspace", async () => {
  render(
    <MemoryRouter initialEntries={["/"]}>
      <App />
    </MemoryRouter>,
  );

  expect(screen.getByRole("heading", { name: "Create speech" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Studio" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(await screen.findByText("Core online")).toBeVisible();
  expect(screen.getByText("1 Worker")).toBeVisible();
});

test("translates the shell and persists the selected language", async () => {
  render(
    <MemoryRouter initialEntries={["/"]}>
      <App />
    </MemoryRouter>,
  );

  expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute(
    "href",
    "#main-content",
  );
  expect(screen.getByRole("link", { name: "Studio" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Overview" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Settings" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Studio" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", { name: "Studio" }).closest("nav")).toHaveAttribute(
    "aria-label",
    "Primary navigation",
  );
  expect(screen.getByText("Local speech workspace")).toBeVisible();
  expect(screen.getByText("Runs locally")).toBeVisible();
  expect(screen.getByRole("button", { name: "Switch to dark mode" })).toBeVisible();
  expect(screen.getByRole("group", { name: "Select language" })).toBeVisible();

  const vietnamese = screen.getByRole("button", { name: "Tiếng Việt" });
  expect(vietnamese).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(vietnamese);

  expect(await screen.findByRole("link", { name: "Tổng quan" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Chuyển đến nội dung chính" })).toHaveAttribute(
    "href",
    "#main-content",
  );
  expect(screen.getByRole("link", { name: "Cài đặt" })).toBeVisible();
  expect(screen.getByText("Không gian làm việc giọng nói cục bộ")).toBeVisible();
  expect(screen.getByText("Chạy trên máy này")).toBeVisible();
  expect(screen.getByRole("button", { name: "Chuyển sang chế độ tối" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Tiếng Việt" })).toHaveAttribute("aria-pressed", "true");
  expect(localStorage.getItem("i18nextLng")).toBe("vi-VN");
});

test("toggles and persists the visual theme", async () => {
  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  );

  const toggle = screen.getByRole("button", { name: "Switch to dark mode" });
  expect(document.documentElement.dataset.theme).toBe("light");
  fireEvent.click(toggle);
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(screen.getByRole("button", { name: "Switch to light mode" })).toBeVisible();
  expect(localStorage.getItem("tts-studio-theme")).toBe("dark");
});

test("keeps Overview as a secondary route", async () => {
  render(
    <MemoryRouter initialEntries={["/overview"]}>
      <App />
    </MemoryRouter>,
  );

  expect(screen.getByRole("heading", { name: "System overview" })).toBeVisible();
  expect(await screen.findByText("/workspace/.tts-studio")).toBeVisible();
  expect(await screen.findByText("vieneu")).toBeVisible();
  expect(screen.getByText("Ready")).toBeVisible();
});

test("shows zero supervised Workers as a neutral empty state", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(
    new Response(JSON.stringify({ ...systemStatus, workers: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

  render(
    <MemoryRouter initialEntries={["/overview"]}>
      <App />
    </MemoryRouter>,
  );

  const summary = await screen.findByText("0 supervised");
  expect(summary).toHaveClass("status-neutral");
  expect(summary).not.toHaveClass("status-ready");
  expect(screen.getByText("No Workers are running.", { exact: false })).toBeVisible();
});

test("links the generation workflow areas and keeps later areas planned", () => {
  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  );

  expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute("href", "/models");
  expect(screen.getByRole("link", { name: "Providers" })).toHaveAttribute("href", "/providers");
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  expect(screen.getByText("Settings").closest("[aria-disabled='true']")).not.toBeInTheDocument();
  for (const name of ["Voices", "Jobs", "History"]) {
    expect(screen.getByRole("link", { name })).toHaveAttribute("href", `/${name.toLowerCase()}`);
  }
  expect(screen.queryByText("Planned")).not.toBeInTheDocument();
});

test("explains why generation controls are unavailable without an installed model", () => {
  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  );

  expect(screen.getByLabelText("Text")).not.toBeDisabled();
  expect(screen.getByLabelText("Model")).not.toBeDisabled();
  expect(screen.getByLabelText("Runtime voice")).toBeDisabled();
  const generateButton = screen.getByRole("button", { name: "Generate speech" });
  expect(generateButton).toBeDisabled();
  expect(generateButton).toHaveAttribute("data-slot", "button");
  expect(screen.getByText("Install a compatible model to generate speech.")).toBeVisible();
});

test("moves focus and updates the title after route navigation", async () => {
  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  );

  fireEvent.click(screen.getByRole("link", { name: "Overview" }));

  expect(await screen.findByRole("heading", { name: "System overview" })).toBeVisible();
  await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  expect(document.title).toBe("System overview · TTS Studio");
});

test("announces a Core status loading failure", async () => {
  vi.mocked(fetch).mockRejectedValueOnce(new TypeError("Network unavailable"));

  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  );

  expect(screen.getByRole("status")).toHaveTextContent("Checking Core status");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Could not reach the local Core. Start TTS Studio and refresh this page.",
  );
});

test("fetchSystemStatus consumes the native system endpoint", async () => {
  const controller = new AbortController();

  await expect(fetchSystemStatus(controller.signal)).resolves.toEqual(systemStatus);
  expect(fetch).toHaveBeenCalledWith("/api/v1/system", {
    headers: { Accept: "application/json" },
    signal: controller.signal,
  });
});

test("fetchSystemStatus rejects unsuccessful responses", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 503 }));

  await expect(fetchSystemStatus()).rejects.toThrow("Core status request failed with 503");
});

test("sidebar theme toggle updates the Settings page selection", async () => {
  localStorage.clear();
  vi.mocked(fetch).mockImplementation((input) => {
    const url = String(input);
    const body = url.endsWith("/system") ? systemStatus : url.endsWith("/settings") ? {
      retain_audio_by_default: true, artifact_max_age_days: null, artifact_max_storage_bytes: null,
      api_token_env: null, host: "127.0.0.1", port: 7860, restart_required: false,
      retention: { retained_count: 0, retained_bytes: 0, max_age_days: null, max_storage_bytes: null },
    } : url.endsWith("/runtime") ? {
      version: "0.1.0", host: "127.0.0.1", port: 7860, data_dir: "/workspace/.tts-studio",
      generation_status: "idle", active_generations: {}, workers: [], storage_accessible: true, database_accessible: true,
    } : { status: "healthy", installed: true, running: true, healthy: true, message: "ok" };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  });
  render(<MemoryRouter initialEntries={["/settings"]}><App /></MemoryRouter>);
  await screen.findByRole("heading", { name: "Settings" });
  fireEvent.click(screen.getByRole("button", { name: "Switch to dark mode" }));
  expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();
});
