import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n from "../../i18n";
import type {
  GenerationJobResponse,
  ModelInstallationResponse,
  VoiceResponse,
} from "../../generated/api";
import { clearBrowserApiToken, setBrowserApiToken } from "../../lib/api";
import { StudioPage } from "./StudioPage";

beforeEach(async () => {
  await i18n.changeLanguage("en-US");
});

afterEach(() => clearBrowserApiToken());

const systemStatus = {
  version: "0.1.0",
  status: "healthy" as const,
  data_dir: "/workspace/.tts-studio",
  workers: [{ engine_id: "fake", status: "ready" as const, message: "" }],
};

const model: ModelInstallationResponse = {
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
  desired_replicas: 1,
  last_error: null,
  created_at: "2026-09-05T10:00:00Z",
  updated_at: "2026-09-05T10:00:00Z",
};

const voices: VoiceResponse[] = [
  { id: "fake-neutral", label: "Neutral", capabilities: ["preset"] },
  { id: "fake-warm", label: "Warm", capabilities: ["preset"] },
];

function job(overrides: Partial<GenerationJobResponse> = {}): GenerationJobResponse {
  return {
    id: "generation-one",
    model_id: "model-one",
    engine_id: "fake",
    voice_id: "fake-neutral",
    reference_id: null,
    saved_voice_id: null,
    text: "Hello from the studio.",
    retain_artifact: true,
    state: "queued",
    bytes_written: 0,
    frame_count: 0,
    sample_rate: null,
    channel_count: null,
    artifact_id: null,
    artifact_url: null,
    correlation_id: "correlation-one",
    cancellation_requested: false,
    error: null,
    created_at: "2026-09-05T10:00:00Z",
    updated_at: "2026-09-05T10:00:00Z",
    ...overrides,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function chooseModel(user: ReturnType<typeof userEvent.setup>, label: string, occurrence = 0) {
  const modelInput = screen.getByRole("combobox", { name: "Model" });
  await user.click(within(modelInput.parentElement!).getByRole("button", { name: "Open options" }));
  await user.click(screen.getAllByRole("option", { name: label })[occurrence]);
}

function installApi(options: {
  generationResponses?: Array<Response | Promise<Response>>;
  createdJob?: GenerationJobResponse;
  createdJobs?: GenerationJobResponse[];
  settingsResponse?: Response | Promise<Response>;
  retainByDefault?: boolean;
  runtimeCapabilities?: string[];
  pcmResponse?: Response | Promise<Response>;
  pcmResponses?: Array<Response | Promise<Response>>;
} = {}) {
  const generationResponses = [...(options.generationResponses ?? [json([])])];
  const createdJob = options.createdJob ?? job();
  const createdJobs = [...(options.createdJobs ?? [createdJob])];
  const pcmResponses = [...(options.pcmResponses ?? [])];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/v1/models") return json([model]);
    if (url === "/api/v1/runtime") return json({ workers: [{ engine_id: "fake", capabilities: options.runtimeCapabilities ?? ["streaming_synthesis", "preset_voices", "synthesis_cancellation"] }], version: "0.1.0", host: "127.0.0.1", port: 7860, data_dir: "/workspace/.tts-studio", generation_status: "idle", active_generations: {}, storage_accessible: true, database_accessible: true });
    if (url === "/api/v1/settings") return options.settingsResponse ?? json({
      retain_audio_by_default: options.retainByDefault ?? true,
      artifact_max_age_days: null,
      artifact_max_storage_bytes: null,
      api_token_env: null,
      host: "127.0.0.1",
      port: 7860,
      restart_required: false,
      retention: { retained_count: 0, retained_bytes: 0, max_age_days: null, max_storage_bytes: null },
    });
    if (url === "/api/v1/voices?model_id=model-one") return json(voices);
    if (url === "/api/v1/saved-voices?model_id=model-one") return json([]);
    if (url === "/api/v1/generations" && method === "GET") {
      return generationResponses.shift() ?? json([createdJob]);
    }
    if (url === "/api/v1/generations" && method === "POST") return json(createdJobs.shift() ?? createdJob, 202);
    if (url === "/api/v1/generations/generation-one/cancel" && method === "POST") {
      return json(job({ state: "cancelled", cancellation_requested: true }));
    }
    if (url.endsWith("/pcm")) {
      return pcmResponses.shift() ?? options.pcmResponse ?? new Response(new Uint8Array([0, 1, 2, 3]), { status: 200 });
    }
    if (url.includes("/artifacts/") && url.endsWith("/audio")) {
      return new Response(new Uint8Array([82, 73, 70, 70]), {
        status: 200,
        headers: { "Content-Type": "audio/wav" },
      });
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

test("validates text and discovers runtime voices for the selected model", async () => {
  const user = userEvent.setup();
  const mock = installApi();
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const voiceInput = await screen.findByRole("combobox", { name: "Runtime voice" });
  await user.click(within(voiceInput.parentElement!).getByRole("button", { name: "Open options" }));
  expect(screen.getByRole("option", { name: "Neutral" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Generate speech" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Enter text to generate speech.");
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByRole("alert")).toHaveTextContent("Nhập văn bản để tạo giọng.");
  await i18n.changeLanguage("en-US");
  expect(mock.mock.calls.some(([url, init]) => url === "/api/v1/generations" && init?.method === "POST")).toBe(false);
});

test("submits generation, shows live PCM progress, and cancels", async () => {
  const user = userEvent.setup();
  const mock = installApi({
    generationResponses: [
      json([]),
      json([job({ state: "generating", bytes_written: 120 })]),
    ],
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "Hello from the studio.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  expect(await screen.findByText("Generation queued")).toBeVisible();
  expect(screen.getByRole("button", { name: "Cancel generation" })).toBeVisible();
  expect(await screen.findByText("Live PCM stream")).toBeVisible();
  expect(screen.queryByRole("button", { name: /play/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("audio")).not.toBeInTheDocument();
  expect(mock).toHaveBeenCalledWith(
    "/api/v1/generations",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        model_id: "model-one",
        voice_id: "fake-neutral",
        text: "Hello from the studio.",
        retain_artifact: true,
      }),
    }),
  );

  await user.click(screen.getByRole("button", { name: "Cancel generation" }));
  expect(await screen.findByText("Cancelled")).toBeVisible();
  expect(mock).toHaveBeenCalledWith(
    "/api/v1/generations/generation-one/cancel",
    expect.objectContaining({ method: "POST" }),
  );
});

test("renders finalized WAV playback and download only after completion", async () => {
  const user = userEvent.setup();
  const createObjectURL = vi.fn(() => "blob:retained-generation");
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
  setBrowserApiToken("session-secret");
  const mock = installApi({
    createdJob: job({
      state: "completed",
      artifact_id: "artifact-one",
      artifact_url: "/api/v1/artifacts/artifact-one/audio",
      sample_rate: 48000,
      channel_count: 1,
      frame_count: 24000,
    }),
  });
  const view = render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "Finished speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  const player = await screen.findByLabelText("Finalized speech audio");
  expect(player).toHaveAttribute("src", "blob:retained-generation");
  expect(player).toHaveAttribute("preload", "metadata");
  expect(screen.getByRole("link", { name: "Download WAV" })).toHaveAttribute(
    "href",
    "blob:retained-generation",
  );
  expect(mock).toHaveBeenCalledWith(
    "/api/v1/artifacts/artifact-one/audio",
    expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer session-secret" }),
    }),
  );
  view.unmount();
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:retained-generation");
});

test("restores the newest generation so retained audio remains replayable", async () => {
  installApi({
    generationResponses: [json([
      job({ id: "older", state: "completed", retain_artifact: false, created_at: "2026-09-05T10:00:00Z", updated_at: "2026-09-05T10:01:00Z" }),
      job({ id: "newer", state: "completed", artifact_id: "artifact-newer", artifact_url: "/api/v1/artifacts/artifact-newer/audio", created_at: "2026-09-05T11:00:00Z", updated_at: "2026-09-05T11:01:00Z" }),
    ])],
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByLabelText("Finalized speech audio")).toHaveAttribute(
    "src",
    "/api/v1/artifacts/artifact-newer/audio",
  );
});

test("isolates PCM chunks when a newer generation replaces an active stream", async () => {
  const user = userEvent.setup();
  const createObjectURL = vi.fn((_value: Blob) => "blob:second-generation");
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
  let releaseFirstStream: (() => void) | undefined;
  let oldStreamReleased = false;
  const firstStream = new ReadableStream<Uint8Array>({
    start(controller) {
      releaseFirstStream = () => {
        oldStreamReleased = true;
        try {
          controller.enqueue(new Uint8Array([9, 9]));
          controller.close();
        } catch {
          // The replacement may have cancelled the old body already.
        }
      };
    },
  });
  installApi({
    generationResponses: [
      json([]),
      json([job({ id: "generation-one", state: "completed", retain_artifact: false, bytes_written: 2 })]),
      json([job({ id: "generation-two", state: "completed", retain_artifact: false, bytes_written: 1 })]),
    ],
    createdJobs: [
      job({ id: "generation-one", state: "queued", retain_artifact: false }),
      job({ id: "generation-two", state: "queued", retain_artifact: false }),
    ],
    pcmResponses: [
      new Response(firstStream, { status: 200 }),
      new Response(new Uint8Array([2]), { status: 200 }),
    ],
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "First speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));
  await screen.findByText("Queued");
  await user.type(screen.getByLabelText("Text"), " Second speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));
  releaseFirstStream?.();

  expect(await screen.findByLabelText("Finalized speech audio", {}, { timeout: 2500 })).toBeVisible();
  expect(oldStreamReleased).toBe(true);
  expect((createObjectURL.mock.calls[0]?.[0] as Blob).size).toBe(45);
});

test("does not subscribe to PCM for a restored terminal non-retained job", async () => {
  const mock = installApi({
    generationResponses: [json([job({ state: "completed", retain_artifact: false })])],
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByText("Generation completed without a retained artifact.")).toBeVisible();
  expect(mock.mock.calls.some(([url]) => url === "/api/v1/generations/generation-one/pcm")).toBe(false);
});

test("supports retention opt-out and keeps the option keyboard accessible", async () => {
  const user = userEvent.setup();
  const mock = installApi({ createdJob: job({ retain_artifact: false }) });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const checkbox = await screen.findByRole("checkbox", { name: /keep in history/i });
  await user.click(checkbox);
  expect(checkbox).toHaveFocus();
  await user.type(screen.getByLabelText("Text"), "Do not retain me.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  await waitFor(() => expect(mock).toHaveBeenCalledWith(
    "/api/v1/generations",
    expect.objectContaining({ body: expect.stringContaining('"retain_artifact":false') }),
  ));
});

test("initializes retention from persisted Settings", async () => {
  installApi({ retainByDefault: false });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByRole("checkbox", { name: /keep in history/i })).not.toBeChecked();
});

test("preserves an explicit retention override when Settings finishes loading", async () => {
  const user = userEvent.setup();
  let resolveSettings: ((response: Response) => void) | undefined;
  const settingsResponse = new Promise<Response>((resolve) => { resolveSettings = resolve; });
  installApi({ settingsResponse });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const checkbox = await screen.findByRole("checkbox", { name: /keep in history/i });
  await user.click(checkbox);
  expect(checkbox).not.toBeChecked();
  resolveSettings?.(json({
    retain_audio_by_default: true,
    artifact_max_age_days: null,
    artifact_max_storage_bytes: null,
    api_token_env: null,
    host: "127.0.0.1",
    port: 7860,
    restart_required: false,
    retention: { retained_count: 0, retained_bytes: 0, max_age_days: null, max_storage_bytes: null },
  }));

  await waitFor(() => expect(checkbox).not.toBeChecked());
});

test.each([
  ["pending", () => new Promise<Response>(() => undefined)],
  ["failed", () => Promise.reject(new Error("Settings unavailable"))],
])("omits untouched retention while Settings is %s", async (_state, settingsResponse) => {
  const user = userEvent.setup();
  const mock = installApi({ settingsResponse: settingsResponse() });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "Use the Core retention default.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  await waitFor(() => {
    const request = mock.mock.calls.find(([url, init]) => (
      url === "/api/v1/generations" && init?.method === "POST"
    ));
    expect(request).toBeDefined();
    expect(JSON.parse(String(request?.[1]?.body))).not.toHaveProperty("retain_artifact");
  });
});

test("keeps status text available without relying on color", async () => {
  installApi({ generationResponses: [json([job({ state: "failed", error: { message: "Worker offline" } })])] });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
  expect(screen.queryByText("Worker offline")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Failed");
  expect(within(screen.getByRole("status")).getByText("Failed")).toBeVisible();
});

test("renders supported prosody controls as sliders and serializes changed values", async () => {
  const user = userEvent.setup();
 const mock = installApi({ runtimeCapabilities: ["streaming_synthesis", "preset_voices", "synthesis_cancellation", "speed", "pitch", "volume", "inline_cues"] });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const speed = await screen.findByRole("slider", { name: "Speed" });
  expect(speed).toBeEnabled();
  expect(screen.getByRole("slider", { name: "Pitch" })).toBeEnabled();
  expect(screen.getByRole("slider", { name: "Volume" })).toBeEnabled();
  expect(speed).toHaveAttribute("min", "0.25");
  expect(speed).toHaveAttribute("max", "4");
  fireEvent.change(speed, { target: { value: "1.5" } });
  fireEvent.change(screen.getByRole("slider", { name: "Pitch" }), { target: { value: "0.25" } });
  fireEvent.change(screen.getByRole("slider", { name: "Volume" }), { target: { value: "1.25" } });
  await user.type(await screen.findByLabelText("Text"), "Controlled speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  await waitFor(() => {
    const request = mock.mock.calls.find(([url, init]) => url === "/api/v1/generations" && init?.method === "POST");
    expect(request).toBeDefined();
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ speed: 1.5, pitch: 0.25, volume: 1.25 });
  });
});

test("provides playable in-memory audio after a non-retained generation", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:ephemeral-generation"), revokeObjectURL: vi.fn() });
  installApi({
    generationResponses: [
      json([]),
      json([job({ state: "generating", retain_artifact: false, bytes_written: 4 })]),
      json([job({ state: "completed", retain_artifact: false, bytes_written: 4 })]),
    ],
    createdJob: job({ retain_artifact: false }),
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "Ephemeral speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  expect(await screen.findByText("Live PCM stream")).toBeVisible();
  expect(await screen.findByLabelText("Finalized speech audio")).toHaveAttribute("src", "blob:ephemeral-generation");
  expect(screen.queryByText("Generation completed without a retained artifact.")).not.toBeInTheDocument();
});

test("captures PCM when a non-retained job completes before polling observes generating", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:fast-generation"), revokeObjectURL: vi.fn() });
  installApi({
    generationResponses: [
      json([]),
      json([job({ state: "completed", retain_artifact: false, bytes_written: 4 })]),
    ],
    createdJob: job({ state: "queued", retain_artifact: false }),
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "Fast speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));

  expect(await screen.findByLabelText("Finalized speech audio")).toHaveAttribute("src", "blob:fast-generation");
});

test("drains queued PCM while a non-retained job transitions through finalizing", async () => {
  const user = userEvent.setup();
  let closeStream: (() => void) | undefined;
  let cancelled = false;
  const pcmStream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array([0, 1]));
      closeStream = () => controller.close();
    },
    cancel() {
      cancelled = true;
    },
  });
  const createObjectURL = vi.fn((_value: Blob) => "blob:drained-generation");
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
  installApi({
    generationResponses: [
      json([]),
      json([job({ state: "generating", retain_artifact: false, bytes_written: 2 })]),
      json([job({ state: "finalizing", retain_artifact: false, bytes_written: 2 })]),
      json([job({ state: "completed", retain_artifact: false, bytes_written: 2 })]),
    ],
    createdJob: job({ retain_artifact: false }),
    pcmResponse: new Response(pcmStream, { status: 200 }),
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  await user.type(await screen.findByLabelText("Text"), "Drained speech.");
  await user.click(screen.getByRole("button", { name: "Generate speech" }));
  expect(await screen.findByText("Live PCM stream")).toBeVisible();

  expect(await screen.findByText("Finalizing", {}, { timeout: 2500 })).toBeVisible();
  expect(cancelled).toBe(false);
  expect(createObjectURL).not.toHaveBeenCalled();

  closeStream?.();
  expect(await screen.findByLabelText("Finalized speech audio", {}, { timeout: 2500 })).toHaveAttribute("src", "blob:drained-generation");
  const wav = createObjectURL.mock.calls[0]?.[0] as Blob;
  expect(wav.size).toBe(46);
});

 test("does not offer cancellation or finalized-byte claims while finalizing", async () => {
  installApi({ generationResponses: [json([job({ state: "finalizing", bytes_written: 120 })])] });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByText("Finalizing")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument();
  expect(screen.queryByText(/finalized so far/i)).not.toBeInTheDocument();
  expect(screen.getByText("Preparing the finalized WAV")).toBeVisible();
});

test("localizes Studio labels when the locale changes", async () => {
  await i18n.changeLanguage("vi-VN");
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/models")) return Promise.resolve(json([]));
    if (url.endsWith("/generations")) return Promise.resolve(json([]));
    if (url.endsWith("/runtime")) return Promise.resolve(json({ workers: [] }));
    return Promise.resolve(json({ retain_audio_by_default: true }));
  }));
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);
  expect(await screen.findByRole("heading", { name: "Tạo giọng" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Thiết lập tạo giọng" })).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("updates saved voice labels when the locale changes without refetching", async () => {
  const user = userEvent.setup();
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/models")) return Promise.resolve(json([model]));
    if (url.endsWith("/generations")) return Promise.resolve(json([]));
    if (url.endsWith("/runtime")) return Promise.resolve(json({ workers: [{ engine_id: "fake", capabilities: [] }] }));
    if (url.endsWith("/settings")) return Promise.resolve(json({ retain_audio_by_default: true }));
    if (url.endsWith("/voices?model_id=model-one")) return Promise.resolve(json(voices));
    if (url.endsWith("/saved-voices?model_id=model-one")) return Promise.resolve(json([{ id: "saved-one", label: "My voice" }]));
    return Promise.resolve(json([]));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);
  await chooseModel(user, "fixtures/compatible · INT8");
  const voiceInput = await screen.findByRole("combobox", { name: "Runtime voice" });
  await user.click(within(voiceInput.parentElement!).getByRole("button", { name: "Open options" }));
  expect(await screen.findByRole("option", { name: "My voice (Saved)" })).toBeVisible();
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByRole("option", { name: "My voice (Đã lưu)" })).toBeVisible();
  expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/saved-voices?model_id=model-one"))).toHaveLength(1);
  await i18n.changeLanguage("en-US");
});

test("invalidates voices during model discovery and ignores obsolete responses", async () => {
  const user = userEvent.setup();
  const pending = new Map<string, Array<(response: Response) => void>>();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([model, { ...model, id: "model-two" }]));
    if (url === "/api/v1/generations") return Promise.resolve(json([]));
    if (url === "/api/v1/runtime") return Promise.resolve(json({ workers: [{ engine_id: "fake", capabilities: ["streaming_synthesis", "preset_voices"] }] }));
    if (url === "/api/v1/settings") return Promise.resolve(json({ retain_audio_by_default: true }));
    return new Promise<Response>((resolve) => { pending.set(url, [...(pending.get(url) ?? []), resolve]); });
  }));
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);
  const first = "/api/v1/voices?model_id=model-one";
  const second = "/api/v1/voices?model_id=model-two";
  const firstSaved = "/api/v1/saved-voices?model_id=model-one";
  const secondSaved = "/api/v1/saved-voices?model_id=model-two";
  await waitFor(() => expect(pending.has(first)).toBe(true));
  await act(async () => { pending.get(firstSaved)![0](json([])); });
  await act(async () => { pending.get(first)![0](json(voices)); });
  expect(screen.getByRole("button", { name: "Generate speech" })).toBeEnabled();
  await chooseModel(user, "fixtures/compatible · INT8", 1);
  expect(screen.queryByRole("option", { name: "Neutral" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Runtime voice")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Generate speech" })).toBeDisabled();
  await chooseModel(user, "fixtures/compatible · INT8", 0);
  await act(async () => { pending.get(second)![0](json([{ id: "stale", label: "Stale voice", capabilities: [] }])); });
  await act(async () => { pending.get(secondSaved)![0](json([])); });
  expect(screen.queryByRole("option", { name: "Stale voice" })).not.toBeInTheDocument();
  await act(async () => { pending.get(first)![1](json({ error: { message: "Discovery failed" } }, 503)); });
  await act(async () => { pending.get(firstSaved)![1](json([])); });
  expect(screen.getByLabelText("Runtime voice")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Generate speech" })).toBeDisabled();
});

test.each(["saved-first", "preset-first"])("merges saved and preset voices when %s response resolves first", async (order) => {
  const user = userEvent.setup();
  const pending = new Map<string, (response: Response) => void>();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([model]));
    if (url === "/api/v1/generations") return Promise.resolve(json([]));
    if (url === "/api/v1/runtime") return Promise.resolve(json({ workers: [{ engine_id: "fake", capabilities: ["streaming_synthesis", "preset_voices"] }] }));
    if (url === "/api/v1/settings") return Promise.resolve(json({ retain_audio_by_default: true }));
    if (url === "/api/v1/voices?model_id=model-one" || url === "/api/v1/saved-voices?model_id=model-one") {
      return new Promise<Response>((resolve) => pending.set(url, resolve));
    }
    throw new Error(`Unexpected request: ${url}`);
  }));
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);
  await waitFor(() => expect(pending.size).toBe(2));
  const preset = "/api/v1/voices?model_id=model-one";
  const saved = "/api/v1/saved-voices?model_id=model-one";
  const first = order === "saved-first" ? saved : preset;
  const second = order === "saved-first" ? preset : saved;
  await act(async () => { pending.get(first)!(json(first === saved ? [{ id: "saved-one", label: "Saved one" }] : voices)); });
  await act(async () => { pending.get(second)!(json(second === saved ? [{ id: "saved-one", label: "Saved one" }] : voices)); });

  const voiceInput = await screen.findByRole("combobox", { name: "Runtime voice" });
  expect(voiceInput).toHaveValue("Neutral");
  await user.click(within(voiceInput.parentElement!).getByRole("button", { name: "Open options" }));
  expect(screen.getByRole("option", { name: "Saved one (Saved)" })).toBeVisible();
  expect(screen.getByRole("option", { name: "Neutral" })).toBeVisible();
});

test("selects a saved voice when the model reports no preset voices", async () => {
  const user = userEvent.setup();
  const mock = installApi();
  mock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/models") return json([model]);
    if (url === "/api/v1/generations" && (init?.method ?? "GET") === "GET") return json([]);
    if (url === "/api/v1/runtime") return json({ workers: [{ engine_id: "fake", capabilities: ["streaming_synthesis"] }] });
    if (url === "/api/v1/settings") return json({ retain_audio_by_default: true });
    if (url === "/api/v1/voices?model_id=model-one") return json([]);
    if (url === "/api/v1/saved-voices?model_id=model-one") return json([{ id: "saved-only", label: "Saved only" }]);
    if (url === "/api/v1/generations" && init?.method === "POST") return json(job({ saved_voice_id: "saved-only" }), 202);
    throw new Error(`Unexpected request: ${url}`);
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const voiceInput = await screen.findByRole("combobox", { name: "Runtime voice" });
  await waitFor(() => expect(voiceInput).toHaveValue("Saved only (Saved)"));
  expect(voiceInput).toHaveAccessibleName("Runtime voice");
  await user.type(screen.getByLabelText("Text"), "Saved voice speech.");
  const generate = screen.getByRole("button", { name: "Generate speech" });
  expect(generate).toBeEnabled();
  expect(generate).not.toHaveAttribute("aria-describedby");
  await user.click(generate);
  await waitFor(() => expect(mock).toHaveBeenCalledWith("/api/v1/generations", expect.objectContaining({ body: expect.stringContaining('"saved_voice_id":"saved-only"') })));
  expect(await screen.findByText("Generation queued")).toBeVisible();
});

test("gates generation, cancellation, cues, and prosody from Worker capabilities", async () => {
  const mock = installApi({
    runtimeCapabilities: [],
    generationResponses: [json([job({ state: "generating" })])],
  });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByText("Generation is unavailable: selected Worker does not support streaming synthesis.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Generate speech" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel generation" })).toBeDisabled();
  expect(screen.getByText("Cancellation is unavailable: selected Worker does not support cancellation.")).toBeVisible();
  expect(document.getElementById("speech-text-hint")).toHaveTextContent("Inline delivery cues are unavailable for the selected Worker.");
  expect(screen.getByRole("slider", { name: "Speed" })).toBeDisabled();
  expect(mock.mock.calls.some(([url, init]) => url === "/api/v1/generations" && init?.method === "POST")).toBe(false);
});

test("recovers Studio generation and cancellation gates after a transient runtime probe", async () => {
  let runtimeCalls = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([model]));
    if (url === "/api/v1/generations" && (init?.method ?? "GET") === "GET") return Promise.resolve(json([job({ state: "generating" })]));
    if (url === "/api/v1/runtime") {
      runtimeCalls += 1;
      return Promise.resolve(runtimeCalls === 1
        ? json({ error: { code: "request_failed" } }, 503)
        : json({ workers: [{ engine_id: "fake", capabilities: ["streaming_synthesis", "preset_voices", "synthesis_cancellation"] }] }));
    }
    if (url === "/api/v1/settings") return Promise.resolve(json({ retain_audio_by_default: true }));
    if (url === "/api/v1/voices?model_id=model-one") return Promise.resolve(json(voices));
    if (url === "/api/v1/saved-voices?model_id=model-one") return Promise.resolve(json([]));
    throw new Error(`Unexpected request: ${url}`);
  }));
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const generate = await screen.findByRole("button", { name: "Generate speech" });
  const cancel = await screen.findByRole("button", { name: "Cancel generation" });
  await waitFor(() => {
    expect(generate).toBeEnabled();
    expect(cancel).toBeEnabled();
  });
  expect(runtimeCalls).toBeGreaterThanOrEqual(2);
});

test("preserves a valid selected non-first voice across a catalog refresh", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models") return Promise.resolve(json([model, { ...model, id: "model-two" }]));
    if (url === "/api/v1/generations") return Promise.resolve(json([]));
    if (url === "/api/v1/runtime") return Promise.resolve(json({ workers: [{ engine_id: "fake", capabilities: ["streaming_synthesis", "preset_voices"] }] }));
    if (url === "/api/v1/settings") return Promise.resolve(json({ retain_audio_by_default: true }));
    if (url === "/api/v1/voices?model_id=model-one" || url === "/api/v1/voices?model_id=model-two") return Promise.resolve(json(voices));
    if (url === "/api/v1/saved-voices?model_id=model-one" || url === "/api/v1/saved-voices?model_id=model-two") return Promise.resolve(json([]));
    throw new Error(`Unexpected request: ${url}`);
  }));
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  const voiceInput = await screen.findByRole("combobox", { name: "Runtime voice" });
  await user.click(within(voiceInput.parentElement!).getByRole("button", { name: "Open options" }));
  await user.click(screen.getByRole("option", { name: "Warm" }));
  expect(voiceInput).toHaveValue("Warm");
  await chooseModel(user, "fixtures/compatible · INT8", 1);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Runtime voice" })).toHaveValue("Warm"));
});

test("names preset voice capability as the exact generation blocker", async () => {
  installApi({ runtimeCapabilities: ["streaming_synthesis"] });
  render(<MemoryRouter><StudioPage systemStatus={{ state: "ready", value: systemStatus }} /></MemoryRouter>);

  expect(await screen.findByText("Generation is unavailable: selected Worker does not support preset voices.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Generate speech" })).toBeDisabled();
});
