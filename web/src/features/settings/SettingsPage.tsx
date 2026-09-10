import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Alert } from "../../components/ui/alert";
import { Button } from "../../components/ui/button";
import { Card } from "../../components/ui/card";
import { Input } from "../../components/ui/input";
import type { RuntimeResponse, ServiceResponse, SettingsResponse } from "../../generated/api";
import {
  clearRetention,
  fetchRuntime,
  fetchService,
  fetchSettings,
  installService,
  restartService,
  uninstallService,
  updateSettings,
} from "../../lib/api";
import { formatBytes, formatNumber, localizedMessage, type LocalizedMessage } from "../../lib/i18n";

type Theme = "light" | "dark";
type SettingsPageProps = { theme: Theme; onThemeChange: (theme: Theme) => void };
type LoadState<T> = { state: "loading" } | { state: "ready"; value: T } | { state: "error"; message: LocalizedMessage };
type Operation = "save" | "install" | "uninstall" | "restart" | "clear" | null;
type LifecycleOperation = Exclude<Operation, "save" | "clear" | null>;
const OPERATION_KEYS: Record<LifecycleOperation, string> = { install: "settingsPage.operationInstall", uninstall: "settingsPage.operationUninstall", restart: "settingsPage.operationRestart" };
const OPERATION_BUTTON_KEYS: Record<LifecycleOperation, string> = { install: "settingsPage.install", uninstall: "settingsPage.uninstall", restart: "settingsPage.restart" };

const CAPABILITY_KEYS: Record<string, string> = {
  streaming_synthesis: "settingsPage.capabilityStreamingSynthesis",
  preset_voices: "settingsPage.capabilityPresetVoices",
  inline_cues: "settingsPage.capabilityInlineCues",
  speed: "settingsPage.capabilitySpeed",
  pitch: "settingsPage.capabilityPitch",
  volume: "settingsPage.capabilityVolume",
  cancellation: "settingsPage.capabilityCancellation",
  cloning: "settingsPage.capabilityCloning",
};

function capabilityLabel(value: string, t: (key: string, options?: Record<string, unknown>) => string) {
  return t(CAPABILITY_KEYS[value] ?? "settingsPage.capabilityUnknown", { value: value.replaceAll("_", " ") });
}

const KNOWN_STATUS_KEYS = new Set(["idle", "queued", "loading", "generating", "finalizing", "completed", "failed", "cancelled", "healthy", "uninstalled", "ready", "unhealthy", "available", "unavailable"]);
function localizedStatus(status: string, t: (key: string, options?: Record<string, unknown>) => string) {
  return KNOWN_STATUS_KEYS.has(status) ? t(`status.${status}`) : t("settingsPage.unknown");
}

function serviceMessageKey(status: string) {
  if (status === "healthy") return "settingsPage.serviceHealthy";
  if (status === "uninstalled") return "settingsPage.serviceUninstalled";
  return "settingsPage.serviceStatusUnknown";
}

function workerMessage(message: string, t: (key: string, options?: Record<string, unknown>) => string) {
  if (!message) return t("settingsPage.noWorkerMessage");
  const key = message === "Worker is ready." ? "settingsPage.workerReadyMessage" : message === "Worker unavailable." ? "settingsPage.workerUnavailableMessage" : null;
  return key ? t(key) : t("settingsPage.unknownWorkerMessage");
}

function Section({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={`${title.toLowerCase().replaceAll(" ", "-")}-heading`}>
      <Card
        header={<div><h2 id={`${title.toLowerCase().replaceAll(" ", "-")}-heading`}>{title}</h2><p>{description}</p></div>}
      >
        {children}
      </Card>
    </section>
  );
}

function LoadNotice({ label, state, t }: { label: string; state: LoadState<unknown>; t: (key: string, options?: Record<string, unknown>) => string }) {
  if (state.state === "loading") return <Alert role="status" aria-label={t("settingsPage.loading", { label: label.toLowerCase() })}>{t("settingsPage.loading", { label: label.toLowerCase() })}</Alert>;
  if (state.state === "error") return <Alert variant="error" role="alert">{t(state.message.key, state.message.values)}</Alert>;
  return null;
}

export function SettingsPage({ theme, onThemeChange }: SettingsPageProps) {
  const { t, i18n } = useTranslation();
  const [settings, setSettings] = useState<LoadState<SettingsResponse>>({ state: "loading" });
  const [runtime, setRuntime] = useState<LoadState<RuntimeResponse>>({ state: "loading" });
  const [service, setService] = useState<LoadState<ServiceResponse>>({ state: "loading" });
  const [form, setForm] = useState({ retain_audio_by_default: true, artifact_max_age_days: "", artifact_max_storage_bytes: "" });
  const [busy, setBusy] = useState<Operation>(null);
  const [confirming, setConfirming] = useState<Operation>(null);
  const [notice, setNotice] = useState<LocalizedMessage | null>(null);
  const [error, setError] = useState<LocalizedMessage | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchSettings(controller.signal).then((value) => {
      setSettings({ state: "ready", value });
      setForm({
        retain_audio_by_default: value.retain_audio_by_default,
        artifact_max_age_days: value.artifact_max_age_days?.toString() ?? "",
        artifact_max_storage_bytes: value.artifact_max_storage_bytes?.toString() ?? "",
      });
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setSettings({ state: "error", message: localizedMessage(reason, "settingsPage.settingsUnavailable") }); });
    fetchRuntime(controller.signal).then((value) => setRuntime({ state: "ready", value })).catch((reason: unknown) => { if (!controller.signal.aborted) setRuntime({ state: "error", message: localizedMessage(reason, "settingsPage.runtimeUnavailable") }); });
    fetchService(controller.signal).then((value) => setService({ state: "ready", value })).catch((reason: unknown) => { if (!controller.signal.aborted) setService({ state: "error", message: localizedMessage(reason, "settingsPage.serviceUnavailable") }); });
    return () => controller.abort();
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (settings.state !== "ready" || busy !== null) return;
    setBusy("save"); setNotice(null); setError(null);
    try {
      const value = await updateSettings({
        retain_audio_by_default: form.retain_audio_by_default,
        artifact_max_age_days: form.artifact_max_age_days ? Number(form.artifact_max_age_days) : null,
        artifact_max_storage_bytes: form.artifact_max_storage_bytes ? Number(form.artifact_max_storage_bytes) : null,
      });
      setSettings({ state: "ready", value }); setNotice({ key: "settingsPage.settingsSaved" });
    } catch (reason: unknown) { setError(localizedMessage(reason, "settingsPage.saveFailed")); }
    finally { setBusy(null); }
  }

  async function runClear() {
    if (busy !== null || settings.state !== "ready") return;
    setBusy("clear"); setNotice(null); setError(null);
    try { const result = await clearRetention(); setNotice({ key: "settingsPage.cleanupComplete", values: { deleted: result.deleted, skipped: result.skipped, failed: result.failed } }); }
    catch (reason: unknown) { setError(localizedMessage(reason, "settingsPage.cleanupFailed")); }
    finally { setBusy(null); setConfirming(null); }
  }

  async function runLifecycle(operation: LifecycleOperation) {
    if (busy !== null || settings.state !== "ready") return;
    setBusy(operation); setNotice(null); setError(null);
    try {
      if (operation === "install") await installService(); else if (operation === "uninstall") await uninstallService(); else await restartService();
      setNotice({ key: "settingsPage.serviceOperationComplete", values: { operationKey: OPERATION_KEYS[operation] } });
      if (operation !== "restart") setService((current) => current.state === "ready" ? { ...current, value: { ...current.value, installed: operation === "install", running: operation === "install", status: operation === "install" ? "healthy" : "uninstalled" } } : current);
    } catch (reason: unknown) {
      setError(localizedMessage(reason, "settingsPage.operationFailed"));
    } finally { setBusy(null); setConfirming(null); }
  }

  const settingsValue = settings.state === "ready" ? settings.value : null;
  const renderNotice = (message: LocalizedMessage) => t(message.key, { ...message.values, operation: message.values?.operationKey ? t(String(message.values.operationKey)) : message.values?.operation });
  return (
    <div className="page-stack settings-page">
      <header className="page-header"><div><p className="eyebrow">{t("settingsPage.eyebrow")}</p><h1>{t("settingsPage.title")}</h1><p>{t("settingsPage.description")}</p></div></header>
      {notice ? <Alert variant="success" role="status" aria-live="polite">{renderNotice(notice)}</Alert> : null}
      {error ? <Alert variant="error" role="alert">{t(error.key, error.values)}</Alert> : null}
      {busy ? <Alert role="status" aria-live="polite">{busy === "save" ? t("settingsPage.saving") : busy === "clear" ? t("settingsPage.clearing") : t("settingsPage.serviceInProgress", { operation: t(OPERATION_KEYS[busy as LifecycleOperation]) })}</Alert> : null}
      <LoadNotice label={t("settingsPage.title")} state={settings} t={t} />

      <Section title={t("settingsPage.appearance")} description={t("settingsPage.appearanceDescription")}>
        <fieldset className="theme-options"><legend className="sr-only">{t("settingsPage.theme")}</legend>{(["light", "dark"] as const).map((option) => <label key={option}><input type="radio" name="theme" value={option} checked={theme === option} onChange={() => onThemeChange(option)} />{t(`settingsPage.${option}`)}</label>)}</fieldset>
      </Section>

      <Section title={t("settingsPage.retention")} description={t("settingsPage.retentionDescription")}>
        <form onSubmit={(event) => void save(event)}>
          <div className="settings-field-grid">
            <label className="settings-field"><span>{t("settingsPage.retainDefault")}</span><input type="checkbox" disabled={settings.state !== "ready" || busy !== null} checked={form.retain_audio_by_default} onChange={(event) => setForm({ ...form, retain_audio_by_default: event.target.checked })} /></label>
            <Input label={t("settingsPage.maxAge")} inputMode="numeric" type="number" min="1" value={form.artifact_max_age_days} disabled={settings.state !== "ready" || busy !== null} onChange={(event) => setForm({ ...form, artifact_max_age_days: event.target.value })} placeholder={t("settingsPage.noLimit")} />
            <Input label={t("settingsPage.maxStorage")} inputMode="numeric" type="number" min="1" value={form.artifact_max_storage_bytes} disabled={settings.state !== "ready" || busy !== null} onChange={(event) => setForm({ ...form, artifact_max_storage_bytes: event.target.value })} placeholder={t("settingsPage.noLimit")} />
          </div>
          <div className="settings-actions"><Button type="submit" disabled={settings.state !== "ready" || busy !== null}>{busy === "save" ? t("settingsPage.saving") : t("settingsPage.save")}</Button><span>{t("settingsPage.retainedSummary", { count: formatNumber(settingsValue?.retention.retained_count ?? 0, i18n.language), bytes: formatBytes(settingsValue?.retention.retained_bytes ?? 0, i18n.language) })}</span></div>
        </form>
        <div className="settings-danger"><p>{t("settingsPage.clearDescription")}</p>{confirming === "clear" ? <div className="confirmation"><span>{t("settingsPage.confirmClear")}</span><Button type="button" onClick={() => void runClear()} disabled={busy !== null}>{t("settingsPage.confirmClearButton")}</Button><Button type="button" variant="outline" disabled={busy !== null} onClick={() => setConfirming(null)}>{t("settingsPage.cancel")}</Button></div> : <Button type="button" variant="outline" disabled={settings.state !== "ready" || busy !== null} onClick={() => setConfirming("clear")}>{t("settingsPage.clear")}</Button>}</div>
      </Section>

      <Section title={t("settingsPage.runtime")} description={t("settingsPage.runtimeDescription")}>
        <LoadNotice label={t("settingsPage.runtime")} state={runtime} t={t} />
        {runtime.state === "ready" ? <>
          <dl className="settings-facts"><div><dt>{t("settingsPage.version")}</dt><dd>{runtime.value.version}</dd></div><div><dt>{t("settingsPage.generationStatus")}</dt><dd>{localizedStatus(runtime.value.generation_status, t)}</dd></div><div><dt>{t("settingsPage.storage")}</dt><dd>{runtime.value.storage_accessible ? t("settingsPage.accessible") : t("settingsPage.unavailable")}</dd></div><div><dt>{t("settingsPage.database")}</dt><dd>{runtime.value.database_accessible ? t("settingsPage.accessible") : t("settingsPage.unavailable")}</dd></div></dl>
          <div className="worker-runtime-list">
            {runtime.value.workers.length === 0 ? <p className="muted-copy">{t("settingsPage.noWorkers")}</p> : runtime.value.workers.map((worker) => <article className="worker-runtime" key={worker.engine_id}>
              <div><h3>{worker.engine_id} {t("settingsPage.worker")}</h3><span className={`worker-state worker-state-${worker.status}`}>{localizedStatus(worker.status, t)}</span></div>
              <p>{workerMessage(worker.message, t)}</p>
              <dl className="settings-facts"><div><dt>{t("settingsPage.engineVersion")}</dt><dd>{worker.engine_version ?? t("settingsPage.unknown")}</dd></div><div><dt>{t("settingsPage.maxConcurrency")}</dt><dd>{worker.max_concurrency ?? t("settingsPage.unknown")}</dd></div></dl>
              <div className="worker-capabilities" aria-label={t("settingsPage.capabilities", { engine: worker.engine_id })}>{worker.capabilities === null ? <span>{t("settingsPage.capabilitiesUnavailable")}</span> : worker.capabilities.length === 0 ? <span>{t("settingsPage.noCapabilities")}</span> : worker.capabilities.map((capability) => <span key={capability}>{capabilityLabel(capability, t)}</span>)}</div>
            </article>)}
          </div>
          <div className="settings-actions"><Link className="text-link" to="/overview">{t("settingsPage.openOverview")}</Link><Link className="text-link" to="/models">{t("settingsPage.manageModels")}</Link></div>
        </> : null}
      </Section>

      <Section title={t("settingsPage.networking")} description={t("settingsPage.networkingDescription")}>
        <div className="settings-field-grid"><Input label={t("settingsPage.effectiveHost")} readOnly value={settingsValue?.host ?? ""} /><Input label={t("settingsPage.effectivePort")} readOnly value={settingsValue?.port ?? ""} /><Input label={t("settingsPage.apiTokenEnv")} readOnly value={settingsValue?.api_token_env ?? t("settingsPage.notConfigured")} hint={t("settingsPage.apiTokenHint")} /><div className="settings-field-wide"><Input label={t("settingsPage.dataDirectory")} readOnly value={runtime.state === "ready" ? runtime.value.data_dir : ""} /></div></div>
      </Section>

      <Section title={t("settingsPage.service")} description={t("settingsPage.serviceDescription")}>
        <LoadNotice label={t("settingsPage.service")} state={service} t={t} />
        {service.state === "ready" ? <p className="service-summary">{t(serviceMessageKey(service.value.status), { status: localizedStatus(service.value.status, t) })}</p> : null}
        <div className="settings-actions lifecycle-actions">{(["install", "uninstall", "restart"] as const).map((operation) => confirming === operation ? <div className="confirmation" key={operation}><span>{operation === "restart" ? t("settingsPage.confirmRestart") : t("settingsPage.confirmOperation", { operation: t(OPERATION_KEYS[operation]) })}</span><Button type="button" onClick={() => void runLifecycle(operation)} disabled={busy !== null}>{t("settingsPage.confirm", { operation: t(OPERATION_KEYS[operation]) })}</Button><Button type="button" variant="outline" disabled={busy !== null} onClick={() => setConfirming(null)}>{t("settingsPage.cancel")}</Button></div> : <Button key={operation} type="button" variant="outline" disabled={settings.state !== "ready" || busy !== null} onClick={() => setConfirming(operation)}>{t(OPERATION_BUTTON_KEYS[operation])}</Button>)}</div>
      </Section>
    </div>
  );
}
