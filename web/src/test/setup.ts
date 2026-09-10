import "@testing-library/jest-dom/vitest";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import en from "../locales/en-US/main.json";
import viCatalog from "../locales/vi-VN/main.json";

void i18n.use(initReactI18next).init({
  lng: "en-US",
  fallbackLng: "en-US",
  ns: ["main"],
  defaultNS: "main",
  resources: { "en-US": { main: en }, "vi-VN": { main: viCatalog } },
  interpolation: { escapeValue: false },
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
