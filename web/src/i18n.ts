import i18n, { type Resource } from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

const modules = import.meta.glob<{ default: Record<string, unknown> }>("./locales/*/main.json", {
  eager: true,
});

const resources: Resource = {};
for (const [path, mod] of Object.entries(modules)) {
  const lng = path.match(/locales\/([^/]+)\/main\.json$/)?.[1];
  if (lng) resources[lng] = { main: mod.default };
}

function updateDocumentLanguage(language: string | undefined) {
  if (typeof document !== "undefined") document.documentElement.lang = language || "en-US";
}

i18n.on("languageChanged", updateDocumentLanguage);
updateDocumentLanguage(i18n.language);

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: "en-US",
    supportedLngs: ["en-US", "vi-VN"],
    ns: ["main"],
    defaultNS: "main",
    detection: {
      order: ["localStorage", "navigator"],
      caches: ["localStorage"],
    },
    interpolation: {
      escapeValue: false,
    },
  })
  .then(() => updateDocumentLanguage(i18n.resolvedLanguage ?? i18n.language));

export default i18n;
