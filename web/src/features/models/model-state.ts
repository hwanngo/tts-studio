import type {
  DownloadJobResponse,
  ModelInstallationResponse,
} from "../../generated/api";

const ACTIVE_DOWNLOAD_STATES = new Set([
  "queued",
  "validating",
  "downloading",
  "verifying",
  "activating",
]);

const UNSAFE_TEXT = [
  /(?:file|socket|unix):\/{1,3}/i,
  /(?:^|\s)\/(?:Users|home|tmp|var|private|opt|etc)\//i,
  /[A-Za-z]:[\\/]/,
  /\\\\[^\\/\s]+[\\/]/,
  /\bbearer\s+\S+/i,
  /\bhf_[A-Za-z0-9]{20,}\b/,
];

export function isActiveDownload(job: DownloadJobResponse): boolean {
  return ACTIVE_DOWNLOAD_STATES.has(job.state);
}

export function progressPercent(job: DownloadJobResponse): number | null {
  if (job.total_bytes === null) return null;
  if (job.total_bytes === 0) return 100;
  return Math.min(100, Math.round((job.bytes_downloaded / job.total_bytes) * 100));
}

export function safeMessage(value: unknown, fallback: string): string {
  if (typeof value !== "string" || value.trim() === "") return fallback;
  if (UNSAFE_TEXT.some((pattern) => pattern.test(value))) return fallback;
  return value;
}

export function safeManagedPath(value: string, fallback = "Managed location unavailable"): string {
  if (
    value.startsWith("/") ||
    value.startsWith("\\") ||
    /^[A-Za-z]:[\\/]/.test(value) ||
    UNSAFE_TEXT.some((pattern) => pattern.test(value))
  ) {
    return fallback;
  }
  return value;
}

export function jobError(job: DownloadJobResponse, fallback = "The download could not be completed."): {
  code: string;
  message: string;
  retryable: boolean;
} | null {
  if (!job.error) return null;
  return {
    code: objectString(job.error, "code") ?? "download_failed",
    message: safeMessage(
      objectString(job.error, "message"),
      fallback,
    ),
    retryable: job.error.retryable === true,
  };
}

export function modelError(model: ModelInstallationResponse, fallback = "The Model Installation needs attention."): {
  code: string;
  message: string;
} | null {
  if (!model.last_error) return null;
  return {
    code: objectString(model.last_error, "code") ?? "model_error",
    message: safeMessage(
      objectString(model.last_error, "message"),
      fallback,
    ),
  };
}

export function modelEvidence(model: ModelInstallationResponse, fallback = "Compatibility evidence unavailable."): string[] {
  const evidence = model.compatibility_evidence.evidence;
  if (!Array.isArray(evidence)) return [];
  return evidence.flatMap((item) => {
    if (typeof item !== "object" || item === null) return [];
    const message = objectString(item as Record<string, unknown>, "message");
    return message ? [safeMessage(message, fallback)] : [];
  });
}

export function replicaSummary(model: ModelInstallationResponse): { ready: number; active: number } {
  return {
    ready: objectNumber(model.replica_summary, "ready") ?? 0,
    active: objectNumber(model.replica_summary, "active_generations") ?? 0,
  };
}

function objectString(value: Record<string, unknown>, key: string): string | null {
  return typeof value[key] === "string" ? value[key] : null;
}

function objectNumber(value: Record<string, unknown>, key: string): number | null {
  return typeof value[key] === "number" && Number.isFinite(value[key])
    ? value[key]
    : null;
}
