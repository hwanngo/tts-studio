import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import i18n from "../../i18n";
import { VoicesPage } from "./VoicesPage";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function chooseModel(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getByRole("button", { name: "Open options" }));
  await user.click(screen.getByRole("option", { name: label }));
}

test("clears catalogs during discovery and failure and ignores stale success and failure", async () => {
  const user = userEvent.setup();
  const pending = new Map<string, Array<(response: Response) => void>>();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([
      { id: "one", repository_id: "fixtures/one" }, { id: "two", repository_id: "fixtures/two" },
    ]));
    return new Promise<Response>((resolve) => { pending.set(url, [...(pending.get(url) ?? []), resolve]); });
  }));
  render(<VoicesPage />);
  const first = "/api/v1/voices?model_id=one";
  const second = "/api/v1/voices?model_id=two";
  await waitFor(() => expect(pending.has(first)).toBe(true));
  await act(async () => { pending.get(first)![0](json([{ id: "first", label: "First voice", capabilities: [] }])); });
  expect(screen.getByText("First voice")).toBeVisible();
  await chooseModel(user, "fixtures/two");
  expect(screen.queryByText("First voice")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Discovering runtime voices");
  await chooseModel(user, "fixtures/one");
  await act(async () => { pending.get(second)![0](json([{ id: "stale", label: "Stale voice", capabilities: [] }])); });
  expect(screen.queryByText("Stale voice")).not.toBeInTheDocument();
  await act(async () => { pending.get(first)![1](json({ error: { message: "Discovery failed" } }, 503)); });
  expect(screen.getByRole("alert")).toHaveTextContent("The request failed");
  expect(screen.queryByRole("list", { name: "Runtime voices" })).not.toBeInTheDocument();
  await chooseModel(user, "fixtures/two");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await chooseModel(user, "fixtures/one");
  await act(async () => { pending.get(second)![1](json({ error: { message: "Obsolete failure" } }, 503)); });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await act(async () => { pending.get(first)![2](json([])); });
  expect(screen.getByText("No runtime voices were reported for this model.")).toBeVisible();
});

test("keeps controls separate, previews the selected voice, and exposes success", async () => {
  const user = userEvent.setup();
  const preview = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(new Response(new Blob(["wav"], { type: "audio/wav" }), {
    status: 200,
    headers: { "Content-Type": "audio/wav" },
  })));
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:preview"), revokeObjectURL: vi.fn() });
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([{ id: "one", repository_id: "fixtures/one" }]));
    if (url === "/api/v1/voices?model_id=one") return Promise.resolve(json([
      { id: "v1", label: "Voice One", capabilities: [] },
      { id: "v2", label: "Voice Two", capabilities: ["streaming"] },
    ]));
    if (url === "/api/v1/voices/preview") return preview(input, init);
    throw new Error(`Unexpected request: ${url}`);
  }));
  render(<VoicesPage />);
  await waitFor(() => expect(screen.getByText("Voice One")).toBeVisible());

  const controls = screen.getByRole("region", { name: "Preview controls" });
  expect(controls.querySelectorAll(":scope > .ui-card")).toHaveLength(0);
  expect(screen.getAllByRole("button", { name: /Preview Voice/ })).toHaveLength(2);
  expect(screen.getByLabelText("Sample text")).toHaveValue("Hello from TTS Studio.");

  await user.clear(screen.getByLabelText("Sample text"));
  await user.type(screen.getByLabelText("Sample text"), "A custom preview.");
  await user.click(screen.getByRole("button", { name: "Preview Voice One" }));
  await waitFor(() => expect(screen.getByLabelText("Preview audio for Voice One")).toBeInTheDocument());
  expect(screen.getByRole("status")).toHaveTextContent("Preview ready for Voice One.");
  expect(screen.getAllByRole("status")).toHaveLength(1);
  expect(preview).toHaveBeenCalledTimes(1);
  expect(preview.mock.calls[0]?.[1]).toMatchObject({ method: "POST" });
  expect(JSON.parse(String(preview.mock.calls[0]?.[1]?.body))).toEqual({ model_id: "one", voice_id: "v1", text: "A custom preview." });
});

test("announces the active preview and revokes replaced and unmounted object URLs", async () => {
  const user = userEvent.setup();
  const revokeObjectURL = vi.fn();
  let objectUrlNumber = 0;
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => `blob:preview-${++objectUrlNumber}`), revokeObjectURL });
  const responses: Array<(response: Response) => void> = [];
  const preview = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => responses.push(resolve)));
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([{ id: "one", repository_id: "fixtures/one" }]));
    if (url === "/api/v1/voices?model_id=one") return Promise.resolve(json([{ id: "v1", label: "Voice One", capabilities: [] }]));
    if (url === "/api/v1/voices/preview") return preview(input);
    throw new Error(`Unexpected request: ${url}`);
  }));
  const view = render(<VoicesPage />);
  await waitFor(() => expect(screen.getByText("Voice One")).toBeVisible());
  const button = screen.getByRole("button", { name: "Preview Voice One" });
  await user.click(button);
  expect(screen.getByRole("button", { name: "Previewing Voice One" })).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("status")).toHaveTextContent("Previewing Voice One");
  await act(async () => { responses[0]!(new Response(new Blob(["one"], { type: "audio/wav" }))); });
  await waitFor(() => expect(screen.getByLabelText("Preview audio for Voice One")).toBeInTheDocument());
  await user.click(screen.getByRole("button", { name: "Preview Voice One" }));
  await act(async () => { responses[1]!(new Response(new Blob(["two"], { type: "audio/wav" }))); });
  await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1"));
  view.unmount();
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-2");
  expect(revokeObjectURL).toHaveBeenCalledTimes(2);
});

test("shows an accessible preview failure and blocks a second request", async () => {
  const user = userEvent.setup();
  let rejectPreview: ((reason?: unknown) => void) | undefined;
  const preview = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((_, reject) => { rejectPreview = reject; }));
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([{ id: "one", repository_id: "fixtures/one" }]));
    if (url === "/api/v1/voices?model_id=one") return Promise.resolve(json([
      { id: "v1", label: "Voice One", capabilities: [] }, { id: "v2", label: "Voice Two", capabilities: [] },
    ]));
    if (url === "/api/v1/voices/preview") return preview(input);
    throw new Error(`Unexpected request: ${url}`);
  }));
  render(<VoicesPage />);
  await waitFor(() => expect(screen.getByText("Voice One")).toBeVisible());
  await user.click(screen.getByRole("button", { name: "Preview Voice One" }));
  expect(screen.getByRole("button", { name: "Preview Voice Two" })).toBeDisabled();
  await act(async () => { rejectPreview!(new Error("Preview failed")); });
  const voiceOneRow = screen.getByText("Voice One").closest("li");
  const voiceTwoRow = screen.getByText("Voice Two").closest("li");
  expect(voiceOneRow).not.toBeNull();
  expect(voiceTwoRow).not.toBeNull();
  expect(within(voiceOneRow!).getByRole("alert")).toHaveTextContent("Something went wrong");
  expect(within(voiceTwoRow!).queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("renders Vietnamese voices copy", async () => {
  await i18n.changeLanguage("vi-VN");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json([])));
  render(<VoicesPage />);
  expect(await screen.findByRole("heading", { name: "Giọng chạy" })).toBeVisible();
  expect(screen.getByLabelText("Điều khiển nghe thử")).toBeVisible();
  expect(screen.getByLabelText("Văn bản nghe thử")).toHaveValue("Xin chào từ TTS Studio.");
  await i18n.changeLanguage("en-US");
});

test("keeps the selected model and sample text when locale changes without refetching", async () => {
  const calls: string[] = [];
  const user = userEvent.setup();
  await i18n.changeLanguage("en-US");
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url === "/api/v1/models") return Promise.resolve(json([{ id: "one", repository_id: "fixtures/one" }]));
    if (url === "/api/v1/voices?model_id=one") return Promise.resolve(json([{ id: "v1", label: "Voice One", capabilities: [] }]));
    throw new Error(`Unexpected request: ${url}`);
  }));
  render(<VoicesPage />);
  await waitFor(() => expect(screen.getByText("Voice One")).toBeVisible());
  const before = calls.length;
  await user.clear(screen.getByLabelText("Sample text"));
  await user.type(screen.getByLabelText("Sample text"), "Custom sample");
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByText("Voice One")).toBeVisible();
  expect(screen.getByLabelText("Văn bản nghe thử")).toHaveValue("Custom sample");
  expect(calls.length).toBe(before);
  await i18n.changeLanguage("en-US");
});
