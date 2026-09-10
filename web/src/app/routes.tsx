import { Navigate, Route, Routes } from "react-router-dom";
import { ModelsPage } from "../features/models/ModelsPage";
import { OverviewPage } from "../features/overview/OverviewPage";
import { StudioPage } from "../features/studio/StudioPage";
import { VoicesPage } from "../features/voices/VoicesPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { HistoryPage } from "../features/history/HistoryPage";
import { ProvidersPage } from "../features/providers/ProvidersPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import type { SystemStatusResult } from "../lib/api";

type AppRoutesProps = {
  systemStatus: SystemStatusResult;
  theme: "light" | "dark";
  onThemeChange: (theme: "light" | "dark") => void;
};

export function AppRoutes({ systemStatus, theme, onThemeChange }: AppRoutesProps) {
  return (
    <Routes>
      <Route path="/" element={<StudioPage systemStatus={systemStatus} />} />
      <Route path="/overview" element={<OverviewPage systemStatus={systemStatus} />} />
      <Route path="/models" element={<ModelsPage />} />
      <Route path="/voices" element={<VoicesPage />} />
      <Route path="/jobs" element={<JobsPage />} />
      <Route path="/history" element={<HistoryPage />} />
      <Route path="/providers" element={<ProvidersPage />} />
      <Route path="/settings" element={<SettingsPage theme={theme} onThemeChange={onThemeChange} />} />
      <Route path="*" element={<Navigate replace to="/" />} />
    </Routes>
  );
}
