import i18n from "../i18n";
import { ApiError } from "./api";

export type LocalizedMessage = {
  key: string;
  values?: Record<string, unknown>;
  retryable?: boolean;
  correlationId?: string;
  details?: Record<string, unknown>;
};

export function localizedMessage(reason: unknown, fallbackKey: string): LocalizedMessage {
  if (reason instanceof ApiError) {
    return {
      key: errorMessageKey(reason.code),
      retryable: reason.retryable,
      correlationId: reason.correlationId,
      details: reason.details,
    };
  }
  return { key: fallbackKey };
}

const ERROR_KEYS: Record<string, string> = {
  adapter_unavailable: "adapterUnavailable",
  artifact_delete_failed: "artifactDeleteFailed",
  artifact_not_found: "artifactNotFound",
  artifact_range_invalid: "artifactRangeInvalid",
  http_error: "httpError",
  authentication_failed: "authenticationFailed",
  capability_unsupported: "capabilityUnsupported",
  confirmation_required: "confirmationRequired",
  download_not_found: "downloadNotFound",
  download_failed: "downloadFailed",
  download_cancelled: "downloadCancelled",
  insufficient_storage: "insufficientStorage",
  checksum_mismatch: "checksumMismatch",
  generation_not_found: "generationNotFound",
  generation_failed: "generationFailed",
  generation_cancelled: "generationCancelled",
  generation_request_invalid: "generationRequestInvalid",
  revision_not_found: "revisionNotFound",
  model_load_failed: "modelLoadFailed",
  model_unload_failed: "modelUnloadFailed",
  voice_list_failed: "voiceListFailed",
  synthesis_failed: "synthesisFailed",
  invalid_audio: "invalidAudio",
  synthesis_cancelled: "synthesisCancelled",
  model_in_use: "modelInUse",
  model_incompatible: "modelIncompatible",
  model_not_found: "modelNotFound",
  model_registry: "modelRegistry",
  model_variant_unavailable: "modelVariantUnavailable",
  internal_error: "internalError",
  not_found: "notFound",
  provider_configuration_missing: "providerConfigurationMissing",
  provider_in_use: "providerInUse",
  provider_not_found: "providerNotFound",
  provider_profile_invalid: "providerProfileInvalid",
  reference_capability_unsupported: "referenceCapabilityUnsupported",
  reference_cleanup_failed: "referenceCleanupFailed",
  cleanup_failed: "cleanupFailed",
  reference_recovery_required: "referenceRecoveryRequired",
  reference_in_use: "referenceInUse",
  reference_invalid: "referenceInvalid",
  reference_not_found: "referenceNotFound",
  reference_request_invalid: "referenceRequestInvalid",
  reference_unsupported: "referenceUnsupported",
  repository_id_invalid: "repositoryIdInvalid",
  request_failed: "requestFailed",
  request_validation_failed: "requestValidationFailed",
  service_operation_failed: "serviceOperationFailed",
  service_unsupported: "serviceUnsupported",
  settings_invalid: "settingsInvalid",
  unsupported_speech_request: "unsupportedSpeechRequest",
  voice_not_found: "voiceNotFound",
  worker_unavailable: "workerUnavailable",
};

function activeLocale(locale?: string): string {
  return locale ?? i18n.language;
}

export function formatDate(value: string, locale?: string): string {
  return new Intl.DateTimeFormat(activeLocale(locale), { dateStyle: "medium" }).format(new Date(value));
}

export function formatNumber(value: number, locale?: string): string {
  return new Intl.NumberFormat(activeLocale(locale)).format(value);
}

export function formatBytes(value: number, locale?: string): string {
  const resolvedLocale = activeLocale(locale);
  if (!Number.isFinite(value) || value < 0) return i18n.getFixedT(resolvedLocale)("format.sizeUnavailable");
  if (value < 1024) return `${formatNumber(value, resolvedLocale)} B`;

  const units = ["KB", "MB", "GB", "TB"];
  let amount = value / 1024;
  let unit = units[0];
  for (let index = 1; amount >= 1024 && index < units.length; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  const precision = amount >= 10 || Number.isInteger(amount) ? 0 : 1;
  return `${new Intl.NumberFormat(resolvedLocale, { maximumFractionDigits: precision }).format(amount)} ${unit}`;
}

export function formatDuration(milliseconds: number | null, locale?: string): string {
  const resolvedLocale = activeLocale(locale);
  if (milliseconds === null || !Number.isFinite(milliseconds) || milliseconds < 0) {
    return i18n.getFixedT(resolvedLocale)("format.durationUnavailable");
  }
  return new Intl.NumberFormat(resolvedLocale, {
    maximumFractionDigits: 1,
    style: "unit",
    unit: "second",
    unitDisplay: "long",
  }).format(milliseconds / 1000);
}

export function errorMessageKey(code: string | undefined): string {
  return `errors.${code ? ERROR_KEYS[code] ?? "unknown" : "unknown"}`;
}
