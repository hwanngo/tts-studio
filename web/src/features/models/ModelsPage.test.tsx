import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import i18n from "../../i18n";
import type {
  DownloadJobResponse,
  ModelInstallationResponse,
  ModelValidationResponse,
} from "../../generated/api";
import { ModelsPage } from "./ModelsPage";

const compatibleValidation: ModelValidationResponse = {
  repository_id: "fixtures/compatible",
  requested_revision: "main",
  compatible: true,
  selected_engine_id: "fake",
  results: [
    {
      engine_id: "fake",
      engine_version: "1.0.0",
      available: true,
      compatible: true,
      resolved_commit: "a".repeat(40),
      required_files: ["config.json", "model.bin"],
      available_variants: [
        { id: "int8", label: "INT8" },
        { id: "fp32", label: "FP32" },
      ],
      estimated_bytes: 1024,
      evidence: [{ code: "architecture_supported", message: "Architecture is supported." }],
      error_code: null,
      error_retryable: null,
    },
  ],
};

const installedModel: ModelInstallationResponse = {
  id: "model-one",
  repository_id: "fixtures/compatible",
  requested_revision: "main",
  resolved_commit: "a".repeat(40),
  engine_installation_id: "fake@1",
  compatibility_evidence: {
    engine_id: "fake",
    engine_version: "1.0.0",
    evidence: [{ code: "architecture_supported", message: "Architecture is supported." }],
  },
  runtime_variant: "int8",
  byte_size: 1024,
  cache_path: "models/fixtures--compatible/aaaaaaaa",
  desired_load_state: "unloaded",
  observed_load_state: "unloaded",
  replica_summary: { ready: 0, active_generations: 0 },
  desired_replicas: 1,
  last_error: null,
  created_at: "2026-09-05T10:00:00Z",
  updated_at: "2026-09-05T10:00:00Z",
};

function download(
  overrides: Partial<DownloadJobResponse> = {},
): DownloadJobResponse {
  return {
    id: "job-one",
    repository_id: "fixtures/compatible",
    requested_revision: "main",
    engine_installation_id: "fake@1",
    state: "downloading",
    bytes_downloaded: 512,
    total_bytes: 1024,
    phase: "downloading",
    staging_path: "staging/job-one",
    target_model_id: null,
    cancellation_requested: false,
    correlation_id: "correlation-one",
    error: null,
    created_at: "2026-09-05T10:00:00Z",
    updated_at: "2026-09-05T10:00:01Z",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type ApiState = {
  models: ModelInstallationResponse[];
  downloads: DownloadJobResponse[];
  validation: ModelValidationResponse;
  modelsResponses?: Array<Response | Promise<Response>>;
  downloadsResponses?: Array<Response | Promise<Response>>;
  validationResponses?: Array<Response | Promise<Response>>;
  downloadResponses?: Array<Response | Promise<Response>>;
};

function installApi(initial: Partial<ApiState> = {}) {
  const state: ApiState = {
    models: initial.models ?? [],
    downloads: initial.downloads ?? [],
    validation: initial.validation ?? compatibleValidation,
    modelsResponses: initial.modelsResponses ? [...initial.modelsResponses] : undefined,
    downloadsResponses: initial.downloadsResponses ? [...initial.downloadsResponses] : undefined,
    validationResponses: initial.validationResponses ? [...initial.validationResponses] : undefined,
    downloadResponses: initial.downloadResponses ? [...initial.downloadResponses] : undefined,
  };

  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (url === "/api/v1/models" && method === "GET") {
      return state.modelsResponses?.shift() ?? jsonResponse(state.models);
    }
    if (url === "/api/v1/downloads" && method === "GET") {
      return state.downloadsResponses?.shift() ?? jsonResponse(state.downloads);
    }
    if (url === "/api/v1/models/validate" && method === "POST") {
      return state.validationResponses?.shift() ?? jsonResponse(state.validation);
    }
    if (url === "/api/v1/downloads" && method === "POST") {
      const response = state.downloadResponses?.shift();
      if (response) return response;
      const created = download({ state: "queued", phase: "queued", bytes_downloaded: 0 });
      state.downloads = [created, ...state.downloads];
      return jsonResponse(created, 202);
    }
    if (url === "/api/v1/downloads/job-one/cancel" && method === "POST") {
      const cancelled = download({
        state: "cancelled",
        phase: "cancelled",
        cancellation_requested: true,
        error: {
          code: "download_cancelled",
          message: "Model download was cancelled.",
          retryable: true,
        },
      });
      state.downloads = [cancelled];
      return jsonResponse(cancelled);
    }
    if (url === "/api/v1/models/model-one/remove" && method === "POST") {
      state.models = [];
      return new Response(null, { status: 204 });
    }

    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", mock);
  return { mock, state };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Set<EventListener>>();
  readonly url: string;
  closed = false;

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data: Record<string, unknown> = {}) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  MockEventSource.instances = [];
});

test("shows the repository, revision, and variant controls with distinct empty states", async () => {
  installApi();
  render(<ModelsPage />);

  expect(screen.getByRole("heading", { name: "Models" })).toBeVisible();
  expect(screen.getByLabelText("Repository ID")).toBeVisible();
  expect(screen.getByPlaceholderText("organization/repository")).toBeVisible();
  expect(screen.getByLabelText("Revision")).toBeVisible();
  expect(screen.getByLabelText("Variant")).toBeDisabled();
  expect(await screen.findByText("No downloads yet")).toBeVisible();
  expect(screen.getByText("No models installed")).toBeVisible();
});

test("validates a compatible repository and starts the selected variant download", async () => {
  const { mock } = installApi();
  render(<ModelsPage />);

  fireEvent.change(screen.getByLabelText("Repository ID"), {
    target: { value: "fixtures/compatible" },
  });
  fireEvent.change(screen.getByLabelText("Revision"), { target: { value: "main" } });
  fireEvent.submit(screen.getByRole("form", { name: "Validate a model repository" }));

  const card = await screen.findByRole("article", { name: "fake compatibility" });
  expect(within(card).getByText("Compatible")).toBeVisible();
  expect(within(card).getByText("Architecture is supported.")).toBeVisible();
  expect(screen.getByLabelText("Variant")).toHaveValue("INT8");

  fireEvent.click(screen.getByRole("button", { name: "Download model" }));

  await waitFor(() => {
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/downloads",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          repository_id: "fixtures/compatible",
          requested_revision: "main",
          variant: "int8",
        }),
      }),
    );
  });
  expect(await screen.findByText("Queued")).toBeVisible();
});

test("invalidates edited validation and ignores a response for the old form values", async () => {
  const firstValidation = deferred<Response>();
  const secondValidation = deferred<Response>();
  installApi({
    validationResponses: [firstValidation.promise, secondValidation.promise],
  });
  render(<ModelsPage />);

  fireEvent.change(screen.getByLabelText("Repository ID"), {
    target: { value: "fixtures/compatible" },
  });
  fireEvent.change(screen.getByLabelText("Revision"), { target: { value: "main" } });
  fireEvent.submit(screen.getByRole("form", { name: "Validate a model repository" }));

  fireEvent.change(screen.getByLabelText("Repository ID"), {
    target: { value: "fixtures/slow" },
  });
  fireEvent.change(screen.getByLabelText("Revision"), { target: { value: "dev" } });
  expect(screen.getByLabelText("Variant")).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Download model" })).not.toBeInTheDocument();

  firstValidation.resolve(jsonResponse(compatibleValidation));
  await act(async () => {
    await firstValidation.promise;
    await Promise.resolve();
  });
  expect(screen.queryByRole("article", { name: "fake compatibility" })).not.toBeInTheDocument();

  fireEvent.submit(screen.getByRole("form", { name: "Validate a model repository" }));
  secondValidation.resolve(
    jsonResponse({
      ...compatibleValidation,
      repository_id: "fixtures/slow",
      requested_revision: "dev",
    }),
  );

  expect(await screen.findByText("fixtures/slow")).toBeVisible();
  expect(screen.getByRole("button", { name: "Download model" })).toBeVisible();
});

test("renders incompatible adapter evidence without a download action", async () => {
  installApi({
    validation: {
      repository_id: "fixtures/incompatible",
      requested_revision: null,
      compatible: false,
      selected_engine_id: null,
      results: [
        {
          ...compatibleValidation.results[0],
          compatible: false,
          resolved_commit: null,
          available_variants: [],
          evidence: [{ code: "architecture_unsupported", message: "Architecture is unsupported." }],
          error_code: "model_incompatible",
        },
      ],
    },
  });
  render(<ModelsPage />);

  fireEvent.change(screen.getByLabelText("Repository ID"), {
    target: { value: "fixtures/incompatible" },
  });
  fireEvent.submit(screen.getByRole("form", { name: "Validate a model repository" }));

  expect(await screen.findByText("No compatible adapter")).toBeVisible();
  expect(screen.getByText("Architecture is unsupported.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Download model" })).not.toBeInTheDocument();
});

test("distinguishes determinate progress from downloads whose total size is unavailable", async () => {
  installApi({
    downloads: [
      download(),
      download({
        id: "job-unknown",
        repository_id: "fixtures/slow",
        bytes_downloaded: 640,
        total_bytes: null,
      }),
    ],
  });
  render(<ModelsPage />);

  const known = await screen.findByRole("progressbar", {
    name: "Download progress for fixtures/compatible",
  });
  expect(known).toHaveAttribute("value", "512");
  expect(known).toHaveAttribute("max", "1024");
  expect(screen.getByText("512 B of 1 KB (50%)")).toBeVisible();

  const unknown = screen.getByRole("progressbar", {
    name: "Download progress for fixtures/slow",
  });
  expect(unknown).not.toHaveAttribute("value");
  expect(screen.getByText("640 B downloaded · Total size unavailable")).toBeVisible();
});

test("cancels an active download and offers an inline retry", async () => {
  const { mock } = installApi({ downloads: [download()] });
  render(<ModelsPage />);

  fireEvent.click(await screen.findByRole("button", { name: "Cancel fixtures/compatible download" }));

  await waitFor(() =>
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/downloads/job-one/cancel",
      expect.objectContaining({ method: "POST" }),
    ),
  );
  expect(await screen.findByText("Cancelled")).toBeVisible();
  expect(screen.getAllByText("The download was cancelled").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "Try fixtures/compatible again" })).toBeVisible();
});

test("shows failed recovery separately and moves retry details back into the form", async () => {
  installApi({
    downloads: [
      download({
        state: "failed",
        phase: "failed",
        error: {
          code: "recovery_required",
          message: "The interrupted download needs recovery.",
          retryable: true,
        },
      }),
    ],
  });
  render(<ModelsPage />);

  expect(await screen.findByText("Recovery required")).toBeVisible();
  expect(screen.getAllByText("Something went wrong").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "Try fixtures/compatible again" }));

  expect(screen.getByLabelText("Repository ID")).toHaveValue("fixtures/compatible");
  expect(screen.getByLabelText("Revision")).toHaveValue("main");
  expect(screen.getByLabelText("Repository ID")).toHaveFocus();
});

test("shows installation facts and makes replacement and removal explicit", async () => {
  const modelWithError: ModelInstallationResponse = {
    ...installedModel,
    last_error: {
      code: "adapter_unavailable",
      message: "Restart the adapter, then check this installation again.",
    },
  };
  const replacement = deferred<Response>();
  const { mock } = installApi({
    models: [modelWithError],
    downloadResponses: [replacement.promise],
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<ModelsPage />);

  const card = await screen.findByRole("article", { name: "fixtures/compatible installation" });
  expect(within(card).getByText("aaaaaaaaaaaa")).toBeVisible();
  expect(within(card).getByText("INT8")).toBeVisible();
  expect(within(card).getByText("1 KB")).toBeVisible();
  expect(within(card).getByText("models/fixtures--compatible/aaaaaaaa")).toBeVisible();
  expect(within(card).getByText("Desired: Unloaded")).toBeVisible();
  expect(within(card).getByText("Observed: Unloaded")).toBeVisible();
  expect(within(card).getByText("0 ready · 0 active generations")).toBeVisible();
  expect(within(card).getByText("Architecture is supported.")).toBeVisible();
  expect(
    within(card).getAllByText("The speech adapter is unavailable")[0],
  ).toBeVisible();

  fireEvent.click(within(card).getByRole("button", { name: "Check for update" }));
  expect(screen.getByLabelText("Repository ID")).toHaveValue("fixtures/compatible");
  expect(screen.getByLabelText("Revision")).toHaveValue("main");
  expect(screen.getByLabelText("Repository ID")).toHaveFocus();

  fireEvent.submit(screen.getByRole("form", { name: "Validate a model repository" }));
  const replaceButton = await screen.findByRole("button", { name: "Replace installed revision" });
  expect(replaceButton).toBeVisible();

  fireEvent.click(replaceButton);
  await waitFor(() =>
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/downloads",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          repository_id: "fixtures/compatible",
          requested_revision: "main",
          variant: "int8",
        }),
      }),
    ),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "Starting download…" })).toBeVisible());
  expect(screen.getByRole("article", { name: "fixtures/compatible installation" })).toBeVisible();

  replacement.reject(new Error("replacement failed"));
  expect(await screen.findByText("Download could not start")).toBeVisible();
  expect(screen.getByText("Something went wrong", { selector: "p" })).toBeVisible();
  expect(screen.getByRole("article", { name: "fixtures/compatible installation" })).toBeVisible();

  fireEvent.click(within(card).getByRole("button", { name: "Remove installation" }));
  expect(confirm).toHaveBeenCalledWith(
    "Remove the fixtures/compatible Model Installation? This cannot be undone.",
  );
  expect(mock).not.toHaveBeenCalledWith(
    "/api/v1/models/model-one/remove",
    expect.anything(),
  );

  confirm.mockReturnValue(true);
  fireEvent.click(within(card).getByRole("button", { name: "Remove installation" }));
  await waitFor(() =>
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/models/model-one/remove",
      expect.objectContaining({ method: "POST" }),
    ),
  );
});

test("focuses a linked error summary while keeping native form controls keyboard reachable", async () => {
  installApi();
  const user = userEvent.setup();
  render(<ModelsPage />);

  const repository = screen.getByLabelText("Repository ID");
  const revision = screen.getByLabelText("Revision");
  const variant = screen.getByLabelText("Variant");
  const validate = screen.getByRole("button", { name: "Validate repository" });
  expect(variant).toBeDisabled();
  await user.tab();
  expect(repository).toHaveFocus();
  await user.tab();
  expect(revision).toHaveFocus();
  await user.tab();
  expect(validate).toHaveFocus();
  await user.tab({ shift: true });
  expect(revision).toHaveFocus();
  await user.tab({ shift: true });
  expect(repository).toHaveFocus();
  await user.keyboard("{Enter}");

  const summary = await screen.findByRole("alert");
  expect(summary).toHaveFocus();
  expect(within(summary).getByRole("link", { name: "Enter a repository ID." })).toHaveAttribute(
    "href",
    "#repository-id",
  );
  expect(repository).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText("Enter a repository ID.", { selector: "p" })).toBeVisible();
});

test("follows Tab and Shift+Tab order and activates validation/download with keyboard keys", async () => {
  const { mock } = installApi();
  const user = userEvent.setup();
  render(<ModelsPage />);

  const repository = screen.getByLabelText("Repository ID");
  const revision = screen.getByLabelText("Revision");
  const validate = screen.getByRole("button", { name: "Validate repository" });
  await user.tab();
  expect(repository).toHaveFocus();
  await user.type(repository, "fixtures/compatible");
  await user.tab();
  expect(revision).toHaveFocus();
  await user.type(revision, "main");
  await user.tab();
  expect(validate).toHaveFocus();
  await user.tab({ shift: true });
  expect(revision).toHaveFocus();

  await user.keyboard("{Enter}");
  const downloadButton = await screen.findByRole("button", { name: "Download model" });
  expect(mock).toHaveBeenCalledWith(
    "/api/v1/models/validate",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        repository_id: "fixtures/compatible",
        requested_revision: "main",
      }),
    }),
  );
  const variant = screen.getByLabelText("Variant");
  expect(variant).toBeEnabled();
  await user.tab();
  expect(variant).toHaveFocus();
  await user.tab();
  expect(validate).toHaveFocus();
  await user.tab();
  expect(downloadButton).toHaveFocus();

  await user.keyboard(" ");
  await waitFor(() =>
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/downloads",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          repository_id: "fixtures/compatible",
          requested_revision: "main",
          variant: "int8",
        }),
      }),
    ),
  );
});

test("activates download cancellation with Enter from the rendered Tab order", async () => {
  const { mock } = installApi({ downloads: [download()] });
  const user = userEvent.setup();
  render(<ModelsPage />);

  const cancel = await screen.findByRole("button", {
    name: "Cancel fixtures/compatible download",
  });
  await user.tab();
  await user.tab();
  await user.tab();
  await user.tab();
  expect(cancel).toHaveFocus();

  await user.keyboard("{Enter}");

  await waitFor(() =>
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/downloads/job-one/cancel",
      expect.objectContaining({ method: "POST" }),
    ),
  );
  expect(await screen.findByText("Cancelled")).toBeVisible();
});

test("activates cancelled-download retry with Space and returns details to the form", async () => {
  installApi({
    downloads: [download({ state: "cancelled", phase: "cancelled" })],
  });
  const user = userEvent.setup();
  render(<ModelsPage />);

  const retry = await screen.findByRole("button", { name: "Try fixtures/compatible again" });
  await user.tab();
  await user.tab();
  await user.tab();
  await user.tab();
  expect(retry).toHaveFocus();

  await user.keyboard(" ");

  expect(screen.getByLabelText("Repository ID")).toHaveValue("fixtures/compatible");
  expect(screen.getByLabelText("Revision")).toHaveValue("main");
  expect(screen.getByLabelText("Repository ID")).toHaveFocus();
});

test("activates installation update with Enter and removal with Space", async () => {
  const { mock } = installApi({ models: [installedModel] });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(<ModelsPage />);

  const card = await screen.findByRole("article", { name: "fixtures/compatible installation" });
  const update = within(card).getByRole("button", { name: "Check for update" });
  const remove = within(card).getByRole("button", { name: "Remove installation" });
  await user.tab();
  await user.tab();
  await user.tab();
  await user.tab();
  expect(update).toHaveFocus();

  await user.keyboard("{Enter}");
  expect(screen.getByLabelText("Repository ID")).toHaveValue("fixtures/compatible");
  expect(screen.getByLabelText("Revision")).toHaveValue("main");
  expect(screen.getByLabelText("Repository ID")).toHaveFocus();

  await user.tab();
  await user.tab();
  await user.tab();
  await user.tab();
  expect(remove).toHaveFocus();
  await user.keyboard(" ");

  expect(confirm).toHaveBeenCalledWith(
    "Remove the fixtures/compatible Model Installation? This cannot be undone.",
  );
  await waitFor(() =>
    expect(mock).toHaveBeenCalledWith(
      "/api/v1/models/model-one/remove",
      expect.objectContaining({ method: "POST" }),
    ),
  );
});

test("does not show empty states when either initial snapshot resource fails", async () => {
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/models" || url === "/api/v1/downloads") {
      throw new Error(`${url} unavailable`);
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", mock);
  render(<ModelsPage />);

  expect((await screen.findAllByText("Model data unavailable")).length).toBeGreaterThan(0);
  expect(screen.queryByText("No downloads yet")).not.toBeInTheDocument();
  expect(screen.queryByText("No models installed")).not.toBeInTheDocument();
  expect(screen.getByText("Downloads unavailable")).toBeVisible();
  expect(screen.getByText("Installed models unavailable")).toBeVisible();
});

test("ignores an older snapshot response that resolves after a newer SSE refresh", async () => {
  const firstModels = deferred<Response>();
  const firstDownloads = deferred<Response>();
  const secondModels = deferred<Response>();
  const secondDownloads = deferred<Response>();
  const olderModel = { ...installedModel, id: "old-model", repository_id: "fixtures/old" };
  const newerModel = { ...installedModel, id: "new-model", repository_id: "fixtures/new" };
  const { mock } = installApi({
    modelsResponses: [firstModels.promise, secondModels.promise],
    downloadsResponses: [firstDownloads.promise, secondDownloads.promise],
  });
  render(<ModelsPage />);

  expect(MockEventSource.instances).toHaveLength(1);
  MockEventSource.instances[0].emit("download.progress", { job_id: "job-one" });
  await waitFor(() =>
    expect(mock.mock.calls.filter(([input]) => String(input) === "/api/v1/models")).toHaveLength(2),
  );

  await act(async () => {
    secondModels.resolve(jsonResponse([newerModel]));
    secondDownloads.resolve(jsonResponse([]));
    await Promise.all([secondModels.promise, secondDownloads.promise]);
    await Promise.resolve();
  });
  expect(await screen.findByRole("article", { name: "fixtures/new installation" })).toBeVisible();

  firstModels.resolve(jsonResponse([olderModel]));
  firstDownloads.resolve(jsonResponse([]));
  await act(async () => {
    await firstModels.promise;
    await firstDownloads.promise;
    await Promise.resolve();
  });
  expect(screen.getByRole("article", { name: "fixtures/new installation" })).toBeVisible();
  expect(screen.queryByRole("article", { name: "fixtures/old installation" })).not.toBeInTheDocument();
});

test("refreshes download and installation snapshots after model events", async () => {
  const { mock, state } = installApi({
    downloads: [download({ state: "queued", phase: "queued" })],
  });
  const view = render(<ModelsPage />);

  expect(await screen.findByText("Queued")).toBeVisible();
  expect(MockEventSource.instances).toHaveLength(1);
  expect(MockEventSource.instances[0].url).toBe("/api/v1/events");

  state.downloads = [download({
    state: "completed",
    phase: "completed",
    bytes_downloaded: 1024,
    target_model_id: "model-one",
  })];
  state.models = [installedModel];
  MockEventSource.instances[0].emit("download.progress", { job_id: "job-one" });
  MockEventSource.instances[0].emit("download.activating", { job_id: "job-one" });
  MockEventSource.instances[0].emit("model.activated", { job_id: "job-one" });

  expect(await screen.findByRole("article", { name: "fixtures/compatible installation" })).toBeVisible();
  expect(screen.getByText("Completed")).toBeVisible();
  expect(
    mock.mock.calls.filter(([input, init]) =>
      String(input) === "/api/v1/models" && (init?.method ?? "GET") === "GET"
    ),
  ).toHaveLength(2);

  view.unmount();
  expect(MockEventSource.instances[0].closed).toBe(true);
});

test("announces API failures in a focusable summary with a recovery action", async () => {
  const mock = vi
    .fn()
    .mockResolvedValueOnce(
      jsonResponse(
        {
          error: {
            code: "adapter_unavailable",
            message: "No installed engine adapter is available.",
            source: "model_registry",
            retryable: true,
            correlation_id: "correlation-one",
          },
        },
        503,
      ),
    )
    .mockResolvedValueOnce(jsonResponse([]));
  vi.stubGlobal("fetch", mock);
  render(<ModelsPage />);

  const summary = await screen.findByRole("alert", { name: "Model data unavailable" });
  await waitFor(() => expect(summary).toHaveFocus());
  expect(summary).toHaveTextContent("Model data unavailable");
  expect(within(summary).getByRole("button", { name: "Refresh model data" })).toBeVisible();
  expect(summary).not.toHaveTextContent("correlation-one");
});

test("never renders staging paths or correlation identifiers from download responses", async () => {
  installApi({
    models: [
      {
        ...installedModel,
        cache_path: "/Users/private/.tts-studio/models/model-one",
      },
    ],
    downloads: [
      download({
        staging_path: "/Users/private/.tts-studio/staging/job-one",
        correlation_id: "secret-correlation-id",
      }),
    ],
    validation: {
      ...compatibleValidation,
      results: [
        {
          ...compatibleValidation.results[0],
          evidence: [
            {
              code: "adapter_detail",
              message: "Loaded from /Users/private/.cache/model.bin",
            },
          ],
        },
      ],
    },
  });
  render(<ModelsPage />);

  expect(await screen.findByText("Downloading")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Repository ID"), {
    target: { value: "fixtures/compatible" },
  });
  fireEvent.submit(screen.getByRole("form", { name: "Validate a model repository" }));
  expect(await screen.findByText("Compatibility evidence unavailable.")).toBeVisible();
  expect(screen.queryByText(/\/Users\/private/)).not.toBeInTheDocument();
  expect(screen.queryByText(/secret-correlation-id/)).not.toBeInTheDocument();
});

test("renders Vietnamese model copy", async () => {
  await i18n.changeLanguage("vi-VN");
  installApi();
  render(<ModelsPage />);
  expect(screen.getByRole("heading", { name: "Mô hình" })).toBeVisible();
  expect(await screen.findByText("Chưa có lượt tải nào")).toBeVisible();
  expect(screen.getByText("Chưa cài đặt mô hình nào")).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("localizes unknown model states and required file counts", async () => {
  await i18n.changeLanguage("vi-VN");
  installApi({
    downloads: [download({ state: "mystery", phase: "mystery", total_bytes: null })],
  });
  render(<ModelsPage />);
  expect(await screen.findByText("Không rõ")).toBeVisible();
  expect(screen.getByText("Giai đoạn: Không rõ")).toBeVisible();
  await i18n.changeLanguage("en-US");
});
