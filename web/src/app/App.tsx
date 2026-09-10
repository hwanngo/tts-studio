import {
  Boxes,
  BriefcaseBusiness,
  Clock3,
  History,
  LayoutDashboard,
  Mic2,
  Moon,
  Settings,
  Sun,
  SlidersHorizontal,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, useLocation } from "react-router-dom";
import { fetchSystemStatus, type SystemStatusResult } from "../lib/api";
import { AppRoutes } from "./routes";

type NavigationItem = {
  labelKey: string;
  icon: LucideIcon;
  href?: string;
};

const navigation: NavigationItem[] = [
  { labelKey: "nav.studio", icon: Sparkles, href: "/" },
  { labelKey: "nav.overview", icon: LayoutDashboard, href: "/overview" },
  { labelKey: "nav.models", icon: Boxes, href: "/models" },
  { labelKey: "nav.voices", icon: Mic2, href: "/voices" },
  { labelKey: "nav.jobs", icon: BriefcaseBusiness, href: "/jobs" },
  { labelKey: "nav.history", icon: History, href: "/history" },
  { labelKey: "nav.providers", icon: SlidersHorizontal, href: "/providers" },
  { labelKey: "nav.settings", icon: Settings, href: "/settings" },
];

const pageTitleKeys: Record<string, string> = {
  "/": "page.studio",
  "/overview": "page.overview",
  "/models": "page.models",
  "/voices": "page.voices",
  "/jobs": "page.jobs",
  "/history": "page.history",
  "/providers": "page.providers",
  "/settings": "page.settings",
};

const languages = [
  { code: "en-US", label: "EN", nameKey: "language.enUS" },
  { code: "vi-VN", label: "VI", nameKey: "language.viVN" },
] as const;

type Theme = "light" | "dark";

function readTheme(): Theme {
  try {
    return localStorage.getItem("tts-studio-theme") === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function App() {
  const { t, i18n } = useTranslation();
  const [systemStatus, setSystemStatus] = useState<SystemStatusResult>({ state: "loading" });
  const [theme, setTheme] = useState<Theme>(readTheme);
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const previousPathname = useRef(location.pathname);

  useEffect(() => {
    const controller = new AbortController();

    fetchSystemStatus(controller.signal).then(
      (value) => setSystemStatus({ state: "ready", value }),
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setSystemStatus({
            state: "error",
            message: error instanceof Error ? error.message : t("status.error"),
          });
        }
      },
    );

    return () => controller.abort();
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("tts-studio-theme", theme); } catch { /* browser storage can be unavailable */ }
  }, [theme]);

  useEffect(() => {
    document.title = t(pageTitleKeys[location.pathname] ?? "page.default");

    if (previousPathname.current !== location.pathname) {
      mainRef.current?.focus({ preventScroll: true });
      previousPathname.current = location.pathname;
    }
  }, [location.pathname, t]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        {t("a11y.skipToContent")}
      </a>
      <aside className="sidebar">
        <div className="brand" aria-label={t("a11y.brand")}>
          <span className="brand-mark" aria-hidden="true">
            <Mic2 size={20} strokeWidth={1.8} />
          </span>
          <span>
            <strong>TTS Studio</strong>
            <small>{t("brand.subtitle")}</small>
          </span>
        </div>

        <nav aria-label={t("a11y.primaryNavigation")}>
          <ul className="nav-list">
            {navigation.map(({ labelKey, icon: Icon, href }) => (
              <li key={labelKey}>
                {href ? (
                  <NavLink
                    className={({ isActive }) => `nav-item${isActive ? " nav-item-active" : ""}`}
                    end={href === "/"}
                    to={href}
                  >
                    <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
                    <span>{t(labelKey)}</span>
                  </NavLink>
                ) : (
                  <span aria-disabled="true" className="nav-item nav-item-planned">
                    <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
                    <span>{t(labelKey)}</span>
                    <small className="planned-label">{t("common.planned")}</small>
                  </span>
                )}
              </li>
            ))}
          </ul>
        </nav>

        <div className="local-note">
          <Clock3 aria-hidden="true" size={16} strokeWidth={1.8} />
          <span>{t("nav.localNote")}</span>
        </div>
        <div className="language-selector" role="group" aria-label={t("language.select")}>
          {languages.map((language) => (
            <button
              key={language.code}
              className="language-button"
              type="button"
              onClick={() => void i18n.changeLanguage(language.code)}
              aria-label={t(language.nameKey)}
              aria-pressed={i18n.resolvedLanguage === language.code}
            >
              {language.label}
            </button>
          ))}
        </div>
        <button
          className="theme-toggle"
          type="button"
          onClick={() => setTheme((current) => current === "light" ? "dark" : "light")}
          aria-label={t(theme === "light" ? "theme.switchToDark" : "theme.switchToLight")}
          aria-pressed={theme === "dark"}
        >
          {theme === "light" ? <Moon aria-hidden="true" size={16} strokeWidth={1.8} /> : <Sun aria-hidden="true" size={16} strokeWidth={1.8} />}
          <span>{t(theme === "light" ? "theme.dark" : "theme.light")}</span>
        </button>
      </aside>

      <main ref={mainRef} id="main-content" className="main-content" tabIndex={-1}>
        <AppRoutes systemStatus={systemStatus} theme={theme} onThemeChange={setTheme} />
      </main>
    </div>
  );
}
