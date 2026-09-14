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

const storageValues = new Map<string, string>();
const browserStorage: Storage = {
  get length() { return storageValues.size; },
  clear() { storageValues.clear(); },
  getItem(key) { return storageValues.get(key) ?? null; },
  key(index) { return [...storageValues.keys()][index] ?? null; },
  removeItem(key) { storageValues.delete(key); },
  setItem(key, value) { storageValues.set(key, String(value)); },
};
Object.defineProperty(window, "localStorage", { configurable: true, value: browserStorage });
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: browserStorage });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
