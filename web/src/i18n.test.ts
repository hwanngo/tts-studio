import { describe, expect, it } from "vitest";
import i18n from "./i18n";

describe("i18n bootstrap", () => {
  it("supports the approved locales and main namespace", () => {
    expect(i18n.options.supportedLngs).toEqual(expect.arrayContaining(["en-US", "vi-VN"]));
    expect(i18n.options.defaultNS).toBe("main");
    expect(i18n.options.fallbackLng).toEqual(expect.arrayContaining(["en-US"]));
  });

  it("bundles the main catalog for each supported locale", () => {
    expect(i18n.hasResourceBundle("en-US", "main")).toBe(true);
    expect(i18n.hasResourceBundle("vi-VN", "main")).toBe(true);
  });

  it("updates the document language at initialization and when the locale changes", async () => {
    await i18n.changeLanguage("vi-VN");
    expect(document.documentElement.lang).toBe("vi-VN");
    await i18n.changeLanguage("en-US");
    expect(document.documentElement.lang).toBe("en-US");
  });
});
