import { describe, expect, it } from "vitest";
import i18n from "../i18n";
import { ApiError } from "./api";
import {
  errorMessageKey,
  formatBytes,
  formatDate,
  formatDuration,
  formatNumber,
  localizedMessage,
} from "./i18n";

describe("locale-aware formatters", () => {
  it("formats numbers and dates for English and Vietnamese", () => {
    expect(formatNumber(1234567.89, "en-US")).toBe("1,234,567.89");
    expect(formatNumber(1234567.89, "vi-VN")).toBe("1.234.567,89");
    expect(formatDate("2026-09-09T00:00:00.000Z", "en-US")).toBe("Sep 9, 2026");
    expect(formatDate("2026-09-09T00:00:00.000Z", "vi-VN")).toBe("9 thg 9, 2026");
  });

  it("uses the active i18n locale when an explicit locale is omitted", async () => {
    await i18n.changeLanguage("vi-VN");
    expect(formatNumber(1234.5)).toBe("1.234,5");
    expect(formatBytes(1536)).toBe("1,5 KB");
    expect(formatDuration(1500)).toBe("1,5 giây");
    await i18n.changeLanguage("en-US");
    expect(formatNumber(1234.5)).toBe("1,234.5");
    expect(formatDuration(1500)).toBe("1.5 seconds");
  });

  it("formats bytes and durations with locale-specific numbers", () => {
    expect(formatBytes(1536, "en-US")).toBe("1.5 KB");
    expect(formatBytes(1536, "vi-VN")).toBe("1,5 KB");
    expect(formatDuration(1500, "en-US")).toBe("1.5 seconds");
    expect(formatDuration(1500, "vi-VN")).toBe("1,5 giây");
    expect(formatDuration(null, "en-US")).toBe("Duration unavailable");
  });
});

describe("stable API error translation keys", () => {
  it("maps known API codes and falls back for unknown codes", () => {
    expect(errorMessageKey("settings_invalid")).toBe("errors.settingsInvalid");
    expect(errorMessageKey("model_not_found")).toBe("errors.modelNotFound");
    expect(errorMessageKey("http_error")).toBe("errors.httpError");
    expect(errorMessageKey("internal_error")).toBe("errors.internalError");
    expect(errorMessageKey("artifact_range_invalid")).toBe("errors.artifactRangeInvalid");
    expect(errorMessageKey("reference_cleanup_failed")).toBe("errors.referenceCleanupFailed");
    expect(errorMessageKey("reference_in_use")).toBe("errors.referenceInUse");
    expect(errorMessageKey("not-a-server-code")).toBe("errors.unknown");
    expect(errorMessageKey(undefined)).toBe("errors.unknown");
  });

  it("preserves typed API error metadata while translating by stable key", async () => {
    const error = new ApiError("opaque backend detail", "request_failed", true, "corr-123", { reason: "private" });
    const message = localizedMessage(error, "errors.unknown");
    expect(message).toMatchObject({ key: "errors.requestFailed", retryable: true, correlationId: "corr-123", details: { reason: "private" } });
    expect(message).not.toHaveProperty("message");
    await i18n.changeLanguage("vi-VN");
    expect(i18n.t(message.key)).toBe("Yêu cầu thất bại");
    await i18n.changeLanguage("en-US");
    expect(i18n.t(message.key)).toBe("The request failed");
  });

  it("uses the active locale at call time without translating arbitrary server text", async () => {
    await i18n.changeLanguage("vi-VN");
    expect(errorMessageKey("request_validation_failed")).toBe("errors.requestValidationFailed");
    expect(i18n.t(errorMessageKey("request_validation_failed"))).toBe("Yêu cầu không hợp lệ");
    await i18n.changeLanguage("en-US");
    expect(i18n.t(errorMessageKey("request_validation_failed"))).toBe("The request is invalid");
  });
});
