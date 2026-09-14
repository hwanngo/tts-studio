import type {
  DownloadJobResponse,
  DownloadRequest,
  ErrorEnvelope,
  AudioArtifactResponse,
  GenerationJobResponse,
  GenerationRequest,
  SavedVoiceResponse,
  ModelInstallationResponse,
  ModelValidationRequest,
  ModelValidationResponse,
  ProviderRequest,
  ProviderResponse,
  RuntimeResponse,
  ServiceOperationResponse,
  ServiceResponse,
  SettingsPatchRequest,
  SettingsResponse,
  ClearRetentionResponse,
  SystemStatus,
  VoiceResponse,
  WorkerSummary,
} from "../generated/api";

export type WorkerStatus = WorkerSummary;
export type { SystemStatus };

export const API_ERROR_CODES = [
  "adapter_unavailable",
  "artifact_delete_failed",
  "artifact_not_found",
  "artifact_range_invalid",
  "authentication_failed",
  "http_error",
  "capability_unsupported",
  "confirmation_required",
  "download_not_found",
  "download_failed",
  "download_cancelled",
  "insufficient_storage",
  "checksum_mismatch",
  "generation_not_found",
  "generation_failed",
  "generation_cancelled",
  "generation_request_invalid",
  "host_not_allowed",
  "revision_not_found",
  "model_load_failed",
  "model_unload_failed",
  "voice_list_failed",
  "synthesis_failed",
  "invalid_audio",
  "synthesis_cancelled",
  "model_in_use",
  "model_incompatible",
  "model_not_found",
  "model_registry",
  "model_variant_unavailable",
  "internal_error",
  "not_found",
  "origin_not_allowed",
  "provider_configuration_missing",
  "provider_in_use",
  "provider_not_found",
  "provider_profile_invalid",
  "reference_capability_unsupported",
  "reference_cleanup_failed",
  "cleanup_failed",
  "reference_recovery_required",
  "reference_in_use",
  "reference_invalid",
  "reference_not_found",
  "reference_request_invalid",
  "reference_unsupported",
  "repository_id_invalid",
  "request_failed",
  "request_validation_failed",
  "service_operation_failed",
  "service_unsupported",
  "settings_invalid",
  "unsupported_speech_request",
  "voice_not_found",
  "worker_unavailable",
] as const;

export type KnownApiErrorCode = (typeof API_ERROR_CODES)[number];
export type ApiErrorCode = KnownApiErrorCode | (string & {});

export type SystemStatusResult =
  | { state: "loading" }
  | { state: "ready"; value: SystemStatus }
  | { state: "error"; message: string };

let browserApiToken: string | null = null;
const authenticationFailureListeners = new Set<() => void>();

export function setBrowserApiToken(token: string): void {
  browserApiToken = token;
}

export function clearBrowserApiToken(): void {
  browserApiToken = null;
}

export function hasBrowserApiToken(): boolean {
  return browserApiToken !== null;
}

export function subscribeToAuthenticationFailure(listener: () => void): () => void {
  authenticationFailureListeners.add(listener);
  return () => authenticationFailureListeners.delete(listener);
}

export async function fetchSystemStatus(signal?: AbortSignal): Promise<SystemStatus> {
  return requestJson<SystemStatus>("/api/v1/system", { signal }, "Core status request");
}

export function fetchSettings(signal?: AbortSignal): Promise<SettingsResponse> {
  return requestJson("/api/v1/settings", { signal }, "Settings request");
}

export function updateSettings(
  payload: SettingsPatchRequest,
  signal?: AbortSignal,
): Promise<SettingsResponse> {
  return requestJson("/api/v1/settings", jsonRequest("PATCH", payload, signal), "Settings update request");
}

export function clearRetention(signal?: AbortSignal): Promise<ClearRetentionResponse> {
  return requestJson(
    "/api/v1/settings/retention/clear",
    jsonRequest("POST", { confirm: true }, signal),
    "Retention cleanup request",
  );
}

export function fetchRuntime(signal?: AbortSignal): Promise<RuntimeResponse> {
  return requestJson("/api/v1/runtime", { signal }, "Runtime request");
}

export function fetchService(signal?: AbortSignal): Promise<ServiceResponse> {
  return requestJson("/api/v1/service", { signal }, "Service status request");
}

export function installService(signal?: AbortSignal): Promise<ServiceOperationResponse> {
  return serviceOperation("install", signal);
}

export function uninstallService(signal?: AbortSignal): Promise<ServiceOperationResponse> {
  return serviceOperation("uninstall", signal);
}

export function restartService(signal?: AbortSignal): Promise<ServiceOperationResponse> {
  return serviceOperation("restart", signal);
}

function serviceOperation(
  operation: "install" | "uninstall" | "restart",
  signal?: AbortSignal,
): Promise<ServiceOperationResponse> {
  return requestJson(
    `/api/v1/service/${operation}`,
    jsonRequest("POST", { confirm: true }, signal),
    `Service ${operation} request`,
  );
}

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly retryable: boolean;
  readonly correlationId?: string;
  readonly details?: Record<string, unknown>;

  constructor(
    message: string,
    code: ApiErrorCode = "request_failed",
    retryable = false,
    correlationId?: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.retryable = retryable;
    this.correlationId = correlationId;
    this.details = details;
  }
}

export function fetchModels(signal?: AbortSignal): Promise<ModelInstallationResponse[]> {
  return requestJson("/api/v1/models", { signal }, "Model list request");
}

export function fetchDownloads(signal?: AbortSignal): Promise<DownloadJobResponse[]> {
  return requestJson("/api/v1/downloads", { signal }, "Download list request");
}

export function fetchVoices(modelId: string, signal?: AbortSignal): Promise<VoiceResponse[]> {
  return requestJson(
    `/api/v1/voices?model_id=${encodeURIComponent(modelId)}`,
    { signal },
    "Voice list request",
  );
}

export async function previewVoice(
  modelId: string,
  voiceId: string,
  text: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await request(
    "/api/v1/voices/preview",
    {
      ...jsonRequest("POST", { model_id: modelId, voice_id: voiceId, text }, signal),
      headers: { Accept: "audio/wav", "Content-Type": "application/json" },
    },
    "Voice preview request",
  );
  return response.blob();
}

export function fetchSavedVoices(modelId: string, signal?: AbortSignal): Promise<SavedVoiceResponse[]> {
  return requestJson(
    `/api/v1/saved-voices?model_id=${encodeURIComponent(modelId)}`,
    { signal },
    "Saved Voice list request",
  );
}

export type ModelVoice = VoiceResponse & { source: "preset" | "saved" };

/**
 * Resolve the two public Voice catalogs as one deterministic list.  Preset Voices
 * retain their API order, then Saved Voices retain theirs.  A catalog failure does
 * not hide the other usable catalog; callers receive an error only when neither
 * request succeeds.
 */
export async function fetchModelVoices(modelId: string, signal?: AbortSignal): Promise<ModelVoice[]> {
  const [presetResult, savedResult] = await Promise.allSettled([
    fetchVoices(modelId, signal),
    fetchSavedVoices(modelId, signal),
  ]);
  const presets = presetResult.status === "fulfilled"
    ? presetResult.value.map((voice) => ({ ...voice, source: "preset" as const }))
    : [];
  const saved = savedResult.status === "fulfilled"
    ? savedResult.value.map((voice) => ({ id: `saved:${voice.id}`, label: voice.label, capabilities: ["saved"], source: "saved" as const }))
    : [];
  if (presetResult.status === "rejected" && savedResult.status === "rejected") {
    throw presetResult.reason;
  }
  return [...presets, ...saved];
}

export function fetchProviders(signal?: AbortSignal): Promise<ProviderResponse[]> {
  return requestJson("/api/v1/providers", { signal }, "Provider list request");
}

export function createProvider(payload: ProviderRequest): Promise<ProviderResponse> {
  return requestJson("/api/v1/providers", jsonRequest("POST", payload), "Provider creation request");
}

export function validateProvider(providerId: string): Promise<ProviderResponse> {
  return requestJson(`/api/v1/providers/${encodeURIComponent(providerId)}/validate`, jsonRequest("POST"), "Provider validation request");
}

export async function deleteProvider(providerId: string): Promise<void> {
  await request(`/api/v1/providers/${encodeURIComponent(providerId)}`, jsonRequest("DELETE"), "Provider deletion request");
}

export function fetchGenerations(signal?: AbortSignal): Promise<GenerationJobResponse[]> {
  return requestJson("/api/v1/generations", { signal }, "Generation list request");
}

export function createGeneration(payload: GenerationRequest): Promise<GenerationJobResponse> {
  return requestJson(
    "/api/v1/generations",
    jsonRequest("POST", payload),
    "Generation request",
  );
}

export function cancelGeneration(jobId: string): Promise<GenerationJobResponse> {
  return requestJson(
    `/api/v1/generations/${encodeURIComponent(jobId)}/cancel`,
    jsonRequest("POST"),
    "Generation cancellation request",
  );
}

export async function consumeGenerationPcm(
  jobId: string,
  onChunk: (chunk: Uint8Array) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await request(
    `/api/v1/generations/${encodeURIComponent(jobId)}/pcm`,
    { signal, headers: { Accept: "application/octet-stream" } },
    "Live PCM request",
  );
  if (!response.body) throw new ApiError("Live PCM response did not include a body");
  const reader = response.body.getReader();
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) return;
      onChunk(next.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export function fetchHistory(signal?: AbortSignal): Promise<AudioArtifactResponse[]> {
  return requestJson("/api/v1/history", { signal }, "History request");
}

export async function deleteHistory(artifactId: string): Promise<void> {
  await request(
    `/api/v1/history/${encodeURIComponent(artifactId)}`,
    jsonRequest("DELETE"),
    "History deletion request",
  );
}

export function validateModel(
  payload: ModelValidationRequest,
  signal?: AbortSignal,
): Promise<ModelValidationResponse> {
  return requestJson(
    "/api/v1/models/validate",
    jsonRequest("POST", payload, signal),
    "Model validation request",
  );
}

export function startDownload(payload: DownloadRequest): Promise<DownloadJobResponse> {
  return requestJson(
    "/api/v1/downloads",
    jsonRequest("POST", payload),
    "Model download request",
  );
}

export function cancelDownload(jobId: string): Promise<DownloadJobResponse> {
  return requestJson(
    `/api/v1/downloads/${encodeURIComponent(jobId)}/cancel`,
    jsonRequest("POST"),
    "Download cancellation request",
  );
}

export async function removeModel(modelId: string): Promise<void> {
  await request(
    `/api/v1/models/${encodeURIComponent(modelId)}/remove`,
    jsonRequest("POST"),
    "Model removal request",
  );
}

export function updateModelReplicas(modelId: string, desiredReplicas: number): Promise<ModelInstallationResponse> {
  return requestJson(
    `/api/v1/models/${encodeURIComponent(modelId)}/replicas`,
    jsonRequest("PATCH", { desired_replicas: desiredReplicas }),
    "Worker replica configuration request",
  );
}

const MODEL_EVENT_TYPES = [
  "model.validation",
  "download.queued",
  "download.validating",
  "download.progress",
  "download.activating",
  "model.activated",
  "download.cancelled",
  "download.failed",
  "stream.reset",
] as const;

export function subscribeToModelEvents(onEvent: () => void): () => void {
  if (browserApiToken !== null) return subscribeToAuthenticatedModelEvents(onEvent);
  const source = new EventSource("/api/v1/events");
  for (const eventType of MODEL_EVENT_TYPES) source.addEventListener(eventType, onEvent);
  return () => source.close();
}

export async function createAuthenticatedMediaUrl(
  url: string,
  signal?: AbortSignal,
): Promise<string> {
  const response = await request(
    url,
    { signal, headers: { Accept: "audio/wav" } },
    "Audio request",
  );
  return URL.createObjectURL(await response.blob());
}

function subscribeToAuthenticatedModelEvents(onEvent: () => void): () => void {
  const controller = new AbortController();
  let reconnectTimer: number | undefined;
  let lastEventId: string | undefined;

  const connect = async () => {
    try {
      const response = await request(
        "/api/v1/events",
        {
          signal: controller.signal,
          headers: {
            Accept: "text/event-stream",
            ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
          },
        },
        "Model event stream",
      );
      lastEventId = await consumeModelEvents(
        response,
        onEvent,
        controller.signal,
        lastEventId,
        (eventId) => { lastEventId = eventId; },
      );
    } catch (error) {
      if (controller.signal.aborted || (error instanceof ApiError && error.code === "authentication_failed")) {
        return;
      }
    }
    if (!controller.signal.aborted) reconnectTimer = window.setTimeout(() => void connect(), 1000);
  };

  void connect();
  return () => {
    controller.abort();
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
  };
}

async function consumeModelEvents(
  response: Response,
  onEvent: () => void,
  signal: AbortSignal,
  previousEventId?: string,
  onLastEventId?: (eventId: string) => void,
): Promise<string | undefined> {
  if (!response.body) throw new ApiError("Model event stream did not include a body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastEventId = previousEventId;
  try {
    while (!signal.aborted) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, { stream: !next.done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        let eventType = "message";
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith("event:")) eventType = line.slice(6).trimStart();
          if (line.startsWith("id:")) {
            lastEventId = line.slice(3).trimStart();
            onLastEventId?.(lastEventId);
          }
        }
        if ((MODEL_EVENT_TYPES as readonly string[]).includes(eventType)) onEvent();
      }
      if (next.done) return lastEventId;
    }
    return lastEventId;
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

function jsonRequest(method: string, body?: unknown, signal?: AbortSignal): RequestInit {
  return {
    method,
    headers: {
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    ...(signal ? { signal } : {}),
  };
}

async function requestJson<T>(
  url: string,
  options: RequestInit,
  description: string,
): Promise<T> {
  const response = await request(url, options, description);
  return response.json() as Promise<T>;
}

async function request(
  url: string,
  options: RequestInit,
  description: string,
): Promise<Response> {
  const token = browserApiToken;
  const response = await fetch(url, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.headers as Record<string, string> | undefined),
      ...(token === null ? {} : { Authorization: `Bearer ${token}` }),
    },
  });
  if (response.ok) return response;

  if (response.status === 401 && browserApiToken === token) {
    clearBrowserApiToken();
    for (const listener of authenticationFailureListeners) listener();
  }

  let envelope: ErrorEnvelope | null = null;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    // A non-JSON failure still receives a stable, status-bearing fallback below.
  }
  if (envelope?.error && typeof envelope.error.message === "string") {
    throw new ApiError(
      envelope.error.message,
      envelope.error.code,
      envelope.error.retryable,
      envelope.error.correlation_id,
      envelope.error.details,
    );
  }
  throw new ApiError(`${description} failed with ${response.status}`);
}
