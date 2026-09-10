import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import i18n from "../../i18n";
import { JobsPage } from "./JobsPage";

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const queuedJob = {
  id: "queued", model_id: "model", engine_id: "fake", voice_id: "voice", text: "queued",
  retain_artifact: true, state: "queued", bytes_written: 0, frame_count: 0, sample_rate: null,
  channel_count: null, artifact_id: null, artifact_url: null, correlation_id: "c1",
  cancellation_requested: false, error: null, created_at: "2026-09-05T10:00:00Z",
  updated_at: "2026-09-05T10:00:00Z",
};

afterEach(() => vi.useRealTimers());

test("renders an explicit empty jobs state", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
  render(<JobsPage />);
  expect(await screen.findByRole("heading", { name: "Jobs" })).toBeVisible();
  expect(await screen.findByText("No generation jobs yet")).toBeVisible();
});

test("renders queued through failed job states with diagnostics", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([
    { id: "queued", model_id: "model", engine_id: "fake", voice_id: "voice", text: "queued", retain_artifact: true, state: "queued", bytes_written: 0, frame_count: 0, sample_rate: null, channel_count: null, artifact_id: null, artifact_url: null, correlation_id: "c1", cancellation_requested: false, error: null, created_at: "2026-09-05T10:00:00Z", updated_at: "2026-09-05T10:00:00Z" },
    { id: "failed", model_id: "model", engine_id: "fake", voice_id: "voice", text: "failed", retain_artifact: true, state: "failed", bytes_written: 0, frame_count: 0, sample_rate: null, channel_count: null, artifact_id: null, artifact_url: null, correlation_id: "c2", cancellation_requested: false, error: { code: "worker_unavailable", message: "Worker offline", retryable: true }, created_at: "2026-09-05T10:00:00Z", updated_at: "2026-09-05T10:00:00Z" },
  ]), { status: 200 })));
  render(<JobsPage />);
  expect(await screen.findByText("Queued")).toBeVisible();
  expect(await screen.findByText("Failed")).toBeVisible();
  expect(screen.getAllByText("The speech worker is unavailable").length).toBeGreaterThan(0);
});

test("does not offer cancellation while finalizing", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([{ ...queuedJob, state: "finalizing" }])));
  render(<JobsPage />);

  expect(await screen.findByText("Finalizing")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument();
});

test("announces cancellation failures and disables the action while pending", async () => {
  let rejectCancel!: (reason: unknown) => void;
  const cancelPending = new Promise<Response>((_, reject) => { rejectCancel = reject; });
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/generations" && (init?.method ?? "GET") === "GET") return Promise.resolve(response([queuedJob]));
    if (url === "/api/v1/generations/queued/cancel") return cancelPending;
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", mock);
  render(<JobsPage />);

  const cancel = await screen.findByRole("button", { name: "Cancel generation" });
  await act(async () => { cancel.click(); });
  expect(screen.getByRole("button", { name: "Cancelling generation" })).toBeDisabled();
  rejectCancel(new Error("Cancellation unavailable"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
  expect(screen.getByRole("button", { name: "Cancel generation" })).toBeEnabled();
});

test("does not start polling for terminal jobs and cleans up active polling on unmount", async () => {
  const setInterval = vi.spyOn(window, "setInterval");
  const clearInterval = vi.spyOn(window, "clearInterval");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([{ ...queuedJob, state: "completed" }] )));
  const terminal = render(<JobsPage />);
  expect(await screen.findByText("Completed")).toBeVisible();
  expect(setInterval.mock.calls.some(([, delay]) => delay === 1000)).toBe(false);
  terminal.unmount();

  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([queuedJob])));
  const active = render(<JobsPage />);
  await screen.findByText("Queued");
  active.unmount();
  expect(clearInterval.mock.calls.length).toBeGreaterThan(0);
});

test("keeps polling other active jobs after cancellation and stops when all are terminal", async () => {
  vi.useFakeTimers();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith("/cancel")) return Promise.resolve(response({ ...queuedJob, state: "cancelled" }));
    reads += 1;
    return Promise.resolve(response(reads === 1 ? [queuedJob, { ...queuedJob, id: "second", state: "generating" }] : [
      { ...queuedJob, state: "cancelled" }, { ...queuedJob, id: "second", state: "completed" },
    ]));
  }));
  await act(async () => { render(<JobsPage />); });
  await act(async () => { screen.getAllByRole("button", { name: "Cancel generation" })[0].click(); });
  expect(screen.getByText("Cancelled")).toBeVisible();
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByText("Completed")).toBeVisible();
  expect(reads).toBe(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(reads).toBe(2);
});

test("renders Vietnamese jobs copy", async () => {
  await i18n.changeLanguage("vi-VN");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
  render(<JobsPage />);
  expect(await screen.findByRole("heading", { name: "Tác vụ" })).toBeVisible();
  expect(await screen.findByText("Chưa có tác vụ tạo giọng nào")).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("localizes unknown job status", async () => {
  await i18n.changeLanguage("vi-VN");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([{ ...queuedJob, state: "mystery" }])));
  render(<JobsPage />);
  expect(await screen.findByText("Không rõ")).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("does not refetch jobs when locale changes", async () => {
  const fetchMock = vi.fn().mockResolvedValue(response([]));
  vi.stubGlobal("fetch", fetchMock);
  await i18n.changeLanguage("en-US");
  render(<JobsPage />);
  await screen.findByText("No generation jobs yet");
  const calls = fetchMock.mock.calls.length;
  await i18n.changeLanguage("vi-VN");
  expect(fetchMock).toHaveBeenCalledTimes(calls);
  await i18n.changeLanguage("en-US");
});
