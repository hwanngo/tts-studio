import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import i18n from "../../i18n";
import { clearBrowserApiToken, setBrowserApiToken } from "../../lib/api";
import { HistoryPage } from "./HistoryPage";

afterEach(() => clearBrowserApiToken());

test("renders a clear empty history state", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
  render(<HistoryPage />);
  expect(await screen.findByRole("heading", { name: "History" })).toBeVisible();
  expect(await screen.findByText("No retained audio yet")).toBeVisible();
});

test("renders retained audio with WAV playback and delete control", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([
    { id: "artifact-one", job_id: "job-one", byte_size: 1024, sha256: "a".repeat(64), sample_rate: 48000, channel_count: 1, frame_count: 24000, duration_ms: 500, created_at: "2026-09-05T10:00:00Z", retained_at: "2026-09-05T10:00:00Z", audio_url: "/api/v1/artifacts/artifact-one/audio" },
  ]), { status: 200 })));
  render(<HistoryPage />);
  expect(await screen.findByLabelText("Audio artifact artifact-one")).toHaveAttribute("src", "/api/v1/artifacts/artifact-one/audio");
  expect(screen.getByRole("link", { name: "Download WAV" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Delete retained audio" })).toBeVisible();
});

test("fetches protected History audio into a revocable authenticated object URL", async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input) === "/api/v1/history") {
      return Promise.resolve(new Response(JSON.stringify([artifacts[0]]), { status: 200 }));
    }
    return Promise.resolve(new Response(new Uint8Array([82, 73, 70, 70]), {
      status: 200,
      headers: { "Content-Type": "audio/wav" },
    }));
  });
  const createObjectURL = vi.fn(() => "blob:history-audio");
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
  setBrowserApiToken("session-secret");

  const view = render(<HistoryPage />);

  expect(await screen.findByLabelText("Audio artifact artifact-one")).toHaveAttribute(
    "src",
    "blob:history-audio",
  );
  expect(screen.getByRole("link", { name: "Download WAV" })).toHaveAttribute(
    "href",
    "blob:history-audio",
  );
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/artifacts/artifact-one/audio",
    expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer session-secret" }),
    }),
  );
  view.unmount();
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:history-audio");
});

test("announces history unavailable", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 503 })));
  render(<HistoryPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("History is unavailable");
});

const artifacts = [
  { id: "artifact-one", job_id: "job-one", byte_size: 1024, sha256: "a".repeat(64), sample_rate: 48000, channel_count: 1, frame_count: 24000, duration_ms: 500, created_at: "2026-09-05T10:00:00Z", retained_at: "2026-09-05T10:00:00Z", audio_url: "/api/v1/artifacts/artifact-one/audio" },
  { id: "artifact-two", job_id: "job-two", byte_size: 2048, sha256: "b".repeat(64), sample_rate: 44100, channel_count: 2, frame_count: 44100, duration_ms: 1000, created_at: "2026-09-05T11:00:00Z", retained_at: "2026-09-05T11:00:00Z", audio_url: "/api/v1/artifacts/artifact-two/audio" },
];

function stubHistory(items = artifacts) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/v1/history" && (init?.method ?? "GET") === "GET") {
      return Promise.resolve(new Response(JSON.stringify(items), { status: 200 }));
    }
    return Promise.resolve(new Response(null, { status: 204 }));
  });
}

test("confirms per-row deletion and uses the exact encoded artifact URL", async () => {
  const encodedArtifact = { ...artifacts[0], id: "artifact/one two" };
  const mock = stubHistory([encodedArtifact]);
  vi.stubGlobal("fetch", mock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact/one two");
  await user.click(screen.getByRole("button", { name: "Delete retained audio" }));

  expect(confirm).toHaveBeenCalledOnce();
  expect(confirm).toHaveBeenCalledWith("Delete retained audio artifact artifact/one two?");
  await waitFor(() => expect(screen.queryByLabelText("Audio artifact artifact/one two")).not.toBeInTheDocument());
  expect(mock.mock.calls.filter(([, init]) => init?.method === "DELETE").map(([input]) => String(input))).toEqual([
    "/api/v1/history/artifact%2Fone%20two",
  ]);
  confirm.mockRestore();
});

test("cancelling per-row deletion makes no DELETE request", async () => {
  const mock = stubHistory();
  vi.stubGlobal("fetch", mock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const user = userEvent.setup();
  render(<HistoryPage />);

  const audio = await screen.findByLabelText("Audio artifact artifact-one");
  const row = audio.closest("li");
  expect(row).not.toBeNull();
  await user.click(within(row!).getByRole("button", { name: "Delete retained audio" }));

  expect(confirm).toHaveBeenCalledWith("Delete retained audio artifact artifact-one?");
  expect(mock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toEqual([]);
  expect(screen.getByLabelText("Audio artifact artifact-one")).toBeVisible();
  confirm.mockRestore();
});

test("supports individual selection, selected count, and disabled empty bulk deletion", async () => {
  vi.stubGlobal("fetch", stubHistory());
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  const deleteSelected = screen.getByRole("button", { name: "Delete selected" });
  expect(deleteSelected).toBeDisabled();
  expect(screen.getByText("0 selected")).toBeVisible();

  const checkbox = screen.getByRole("checkbox", { name: "Select artifact artifact-one" });
  fireEvent.click(checkbox);
  expect(checkbox).toBeChecked();
  expect(screen.getByText("1 selected")).toBeVisible();
  expect(deleteSelected).toBeEnabled();
});

test("toggles all artifact selections from the Select all control", async () => {
  vi.stubGlobal("fetch", stubHistory());
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  const selectAll = screen.getByRole("checkbox", { name: "Select all artifacts" });
  fireEvent.click(selectAll);
  expect(selectAll).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Select artifact artifact-one" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Select artifact artifact-two" })).toBeChecked();
  expect(screen.getByText("2 selected")).toBeVisible();

  fireEvent.click(selectAll);
  expect(selectAll).not.toBeChecked();
  expect(screen.getByText("0 selected")).toBeVisible();
});

test("confirms once and removes all successfully deleted artifacts", async () => {
  const mock = stubHistory();
  vi.stubGlobal("fetch", mock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  fireEvent.click(screen.getByRole("checkbox", { name: "Select all artifacts" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm).toHaveBeenCalledWith("Delete 2 retained audio artifacts?");
  await waitFor(() => expect(screen.queryByLabelText("Audio artifact artifact-one")).not.toBeInTheDocument());
  expect(mock.mock.calls.filter(([, init]) => init?.method === "DELETE").map(([input]) => String(input))).toEqual([
    "/api/v1/history/artifact-one",
    "/api/v1/history/artifact-two",
  ]);
  expect(screen.queryByLabelText("Audio artifact artifact-two")).not.toBeInTheDocument();
  expect(screen.getByText("No retained audio yet")).toBeVisible();
  confirm.mockRestore();
});

test("cancelling bulk deletion makes no DELETE request", async () => {
  const mock = stubHistory();
  vi.stubGlobal("fetch", mock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const user = userEvent.setup();
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  await user.click(screen.getByRole("checkbox", { name: "Select all artifacts" }));
  await user.click(screen.getByRole("button", { name: "Delete selected" }));

  expect(confirm).toHaveBeenCalledWith("Delete 2 retained audio artifacts?");
  expect(mock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toEqual([]);
  expect(screen.getByLabelText("Audio artifact artifact-one")).toBeVisible();
  expect(screen.getByLabelText("Audio artifact artifact-two")).toBeVisible();
  confirm.mockRestore();
});

test("keeps failed bulk deletions visible and announces partial failure", async () => {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/v1/history" && (init?.method ?? "GET") === "GET") {
      return Promise.resolve(new Response(JSON.stringify(artifacts), { status: 200 }));
    }
    return String(input).includes("artifact-one")
      ? Promise.resolve(new Response(null, { status: 204 }))
      : Promise.resolve(new Response(JSON.stringify({ error: { message: "Artifact two is locked" } }), { status: 503 }));
  });
  vi.stubGlobal("fetch", mock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  fireEvent.click(screen.getByRole("checkbox", { name: "Select all artifacts" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

  await waitFor(() => expect(screen.queryByLabelText("Audio artifact artifact-one")).not.toBeInTheDocument());
  expect(screen.getByLabelText("Audio artifact artifact-two")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("Some artifacts could not be deleted.");
  expect(screen.getByRole("alert")).toHaveTextContent("artifact-two: The request failed");
  expect(screen.getByText("1 selected")).toBeVisible();
  confirm.mockRestore();
});

test("keeps selection controls keyboard accessible", async () => {
  vi.stubGlobal("fetch", stubHistory());
  const user = userEvent.setup();
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  const selectAll = screen.getByRole("checkbox", { name: "Select all artifacts" });
  selectAll.focus();
  await user.keyboard(" ");
  expect(selectAll).toBeChecked();
  expect(screen.getByRole("button", { name: "Delete selected" })).toHaveAttribute("type", "button");
});

test("localizes history labels when the locale changes", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
  await i18n.changeLanguage("vi-VN");
  render(<HistoryPage />);
  expect(await screen.findByRole("heading", { name: "Lịch sử" })).toBeVisible();
  expect(screen.getByText("Chưa có âm thanh nào được lưu")).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("updates localized history errors when the locale changes", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 503 })));
  render(<HistoryPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("The request failed");
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByRole("alert")).toHaveTextContent("Yêu cầu thất bại");
  await i18n.changeLanguage("en-US");
});

test("keeps retained audio visible and announces delete failures", async () => {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/v1/history" && (init?.method ?? "GET") === "GET") {
      return Promise.resolve(new Response(JSON.stringify([
        { id: "artifact-one", job_id: "job-one", byte_size: 1024, sha256: "a".repeat(64), sample_rate: 48000, channel_count: 1, frame_count: 24000, duration_ms: 500, created_at: "2026-09-05T10:00:00Z", retained_at: "2026-09-05T10:00:00Z", audio_url: "/api/v1/artifacts/artifact-one/audio" },
      ]), { status: 200 }));
    }
    return Promise.resolve(new Response(JSON.stringify({ error: { message: "Artifact deletion unavailable" } }), { status: 503 }));
  });
  vi.stubGlobal("fetch", mock);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<HistoryPage />);

  await screen.findByLabelText("Audio artifact artifact-one");
  await screen.findByRole("button", { name: "Delete retained audio" }).then((button) => button.click());
  expect(await screen.findByRole("alert")).toHaveTextContent("The request failed");
  expect(screen.getByLabelText("Audio artifact artifact-one")).toBeVisible();
  confirm.mockRestore();
});
