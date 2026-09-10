import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import i18n from "../../i18n";
import { ProvidersPage } from "./ProvidersPage";

beforeEach(async () => {
  await i18n.changeLanguage("en-US");
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/v1/providers" && (init?.method ?? "GET") === "GET") {
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
    }
    return Promise.resolve(new Response(JSON.stringify({ id: "provider-one", kind: "openai_compatible", label: "Local", base_url: "http://127.0.0.1:9000/v1", model: "tts-1", api_key_env: "TTS_PROVIDER_KEY", created_at: "2026-09-08T00:00:00Z", updated_at: "2026-09-08T00:00:00Z" }), { status: 201 }));
  }));
});

test("localizes provider page labels when the locale changes", async () => {
  await i18n.changeLanguage("vi-VN");
  render(<ProvidersPage />);
  expect(await screen.findByRole("heading", { name: "Nhà cung cấp" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Thêm nhà cung cấp" })).toBeVisible();
  await i18n.changeLanguage("en-US");
});

test("updates localized provider errors when the locale changes", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 503 })));
  render(<ProvidersPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("The request failed");
  await i18n.changeLanguage("vi-VN");
  expect(screen.getByRole("alert")).toHaveTextContent("Yêu cầu thất bại");
  await i18n.changeLanguage("en-US");
});

test("creates an OpenAI-compatible provider profile without a secret field", async () => {
  render(<ProvidersPage />);
  expect(await screen.findByRole("heading", { name: "Providers" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Local" } });
  fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "http://127.0.0.1:9000/v1" } });
  fireEvent.change(screen.getByLabelText("Provider model"), { target: { value: "tts-1" } });
  fireEvent.change(screen.getByLabelText("API key environment variable"), { target: { value: "TTS_PROVIDER_KEY" } });
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  expect(await screen.findByText("Local")).toBeVisible();
  const request = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input) === "/api/v1/providers" && init?.method === "POST");
  expect(request?.[1]?.body).not.toContain('"api_key":');
});
