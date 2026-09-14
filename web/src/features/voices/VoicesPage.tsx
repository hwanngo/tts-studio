import { CircleOff, LoaderCircle, Mic2, Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Combobox } from "@/components/ui/combobox";
import { Textarea } from "@/components/ui/textarea";
import type { ModelInstallationResponse, VoiceResponse, WorkerRuntimeResponse } from "../../generated/api";
import { fetchModels, fetchRuntime, fetchVoices, previewVoice } from "../../lib/api";
import { localizedMessage, type LocalizedMessage } from "../../lib/i18n";

function engineIdForModel(model: ModelInstallationResponse | undefined): string | undefined {
  return model?.engine_installation_id?.split("@", 1)[0];
}

export function VoicesPage() {
  const { t } = useTranslation();
  const [models, setModels] = useState<ModelInstallationResponse[]>([]);
  const [modelId, setModelId] = useState("");
  const [voices, setVoices] = useState<VoiceResponse[]>([]);
  const [runtimeWorkers, setRuntimeWorkers] = useState<WorkerRuntimeResponse[]>([]);
  const [runtimeFactsState, setRuntimeFactsState] = useState<"loading" | "advertised" | "unknown">("loading");
  const [error, setError] = useState<LocalizedMessage | null>(null);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [loading, setLoading] = useState(false);
  const [sampleText, setSampleText] = useState(() => t("voicesPage.defaultSample"));
  const [previewingVoiceId, setPreviewingVoiceId] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<{ voiceId: string; message: LocalizedMessage } | null>(null);
  const [previewAnnouncement, setPreviewAnnouncement] = useState<{ key: string; values?: Record<string, unknown> } | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLabel, setPreviewLabel] = useState<string | null>(null);
  const previewController = useRef<AbortController | null>(null);
  const runtimeRefreshToken = useRef(0);
  const refreshRuntimeFacts = useCallback((signal?: AbortSignal) => {
    const token = runtimeRefreshToken.current + 1;
    runtimeRefreshToken.current = token;
    setRuntimeFactsState("loading");
    return fetchRuntime(signal).then(
      (runtime) => {
        if (!signal?.aborted && runtimeRefreshToken.current === token) {
          setRuntimeWorkers(Array.isArray(runtime.workers) ? runtime.workers : []);
          setRuntimeFactsState("advertised");
        }
      },
      () => { if (!signal?.aborted && runtimeRefreshToken.current === token) setRuntimeFactsState("unknown"); },
    );
  }, []);

  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);
  useEffect(() => () => { previewController.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    fetchModels(controller.signal).then(
      (items) => { if (!controller.signal.aborted) { setModels(items); setModelId(items[0]?.id ?? ""); setModelsLoading(false); } },
      (reason: unknown) => { if (!controller.signal.aborted) { setModels([]); setModelId(""); setModelsLoading(false); setError(localizedMessage(reason, "errors.unknown")); } },
    );
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void refreshRuntimeFacts(controller.signal);
    return () => controller.abort();
  }, [refreshRuntimeFacts]);
  useEffect(() => {
    if (runtimeFactsState !== "unknown") return;
    const retry = window.setTimeout(() => { void refreshRuntimeFacts(); }, 750);
    return () => window.clearTimeout(retry);
  }, [refreshRuntimeFacts, runtimeFactsState]);
  useEffect(() => {
    setVoices([]); setError(null); setLoading(Boolean(modelId));
    if (!modelId) return;
    const controller = new AbortController();
    fetchVoices(modelId, controller.signal).then(
      (items) => { if (!controller.signal.aborted) { setVoices(items); setLoading(false); void refreshRuntimeFacts(); } },
      (reason: unknown) => { if (!controller.signal.aborted) { setVoices([]); setLoading(false); setError(localizedMessage(reason, "errors.unknown")); } },
    );
    return () => controller.abort();
  }, [modelId, refreshRuntimeFacts]);

  const selectedModel = models.find((model) => model.id === modelId);
  const capabilities = new Set(runtimeWorkers.find((worker) => worker.engine_id === engineIdForModel(selectedModel))?.capabilities ?? []);
  const runtimeFactsKnown = runtimeFactsState === "advertised";
  const previewSupported = runtimeFactsKnown && capabilities.has("streaming_synthesis") && capabilities.has("preset_voices");
  const previewReason = runtimeFactsKnown ? "voicesPage.previewUnavailable" : "voicesPage.capabilitiesChecking";

  function selectModel(nextModelId: string) {
    previewController.current?.abort(); previewController.current = null;
    setPreviewingVoiceId(null); setPreviewError(null); setPreviewAnnouncement(null); setPreviewLabel(null); setPreviewUrl(null); setModelId(nextModelId);
  }
  async function handlePreview(voice: VoiceResponse) {
    if (!modelId || previewingVoiceId || !previewSupported) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setPreviewingVoiceId(voice.id); setPreviewError(null); setPreviewAnnouncement({ key: "voicesPage.previewing", values: { label: voice.label } });
    try {
      const blob = await previewVoice(modelId, voice.id, sampleText, controller.signal);
      if (controller.signal.aborted) return;
      setPreviewLabel(voice.label); setPreviewUrl(URL.createObjectURL(blob)); setPreviewAnnouncement({ key: "voicesPage.previewReady", values: { label: voice.label } });
    } catch (reason: unknown) {
      if (!controller.signal.aborted) { setPreviewError({ voiceId: voice.id, message: localizedMessage(reason, "errors.unknown") }); setPreviewAnnouncement(null); }
    } finally {
      if (!controller.signal.aborted) { setPreviewingVoiceId(null); previewController.current = null; }
    }
  }

  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">{t("voicesPage.eyebrow")}</p><h1>{t("voicesPage.title")}</h1><p>{t("voicesPage.description")}</p></div></header>
    <Card className="voice-controls" role="region" aria-label={t("voicesPage.previewControls")}>
      <div className="field-grid"><Combobox label={t("voicesPage.model")} id="voice-model" value={modelId} onValueChange={selectModel} placeholder={t("voicesPage.chooseModel")} options={models.map((model) => ({ value: model.id, label: model.repository_id }))} emptyText={t("voicesPage.noInstalledModels")} /><Textarea label={t("voicesPage.sampleText")} id="voice-sample" value={sampleText} onChange={(event) => setSampleText(event.target.value)} hint={t("voicesPage.sampleHint")} /></div>
      {error ? <Alert variant="error" role="alert">{t(error.key, error.values)}</Alert> : null}
      {modelsLoading ? <p role="status" aria-live="polite">{t("voicesPage.loadingModels")}</p> : null}
      {loading ? <p role="status" aria-live="polite">{t("voicesPage.discovering")}</p> : null}
      {previewAnnouncement ? <p className="sr-only" role="status" aria-live="polite">{t(previewAnnouncement.key, previewAnnouncement.values)}</p> : null}
      {previewUrl ? <div className="audio-result"><span>{t("voicesPage.latestPreview", { label: previewLabel })}</span><audio controls src={previewUrl} aria-label={t("voicesPage.previewAudio", { label: previewLabel ?? t("voicesPage.voiceFallback") })} /></div> : null}
      {!modelsLoading && !error && models.length === 0 ? <div className="empty-state panel-state"><CircleOff aria-hidden="true" size={20} /><div className="empty-state-content"><p>{t("voicesPage.noModels")}</p><a className="button-link" href="/models">{t("voicesPage.openModels")}</a></div></div> : null}
    </Card>
    {!modelsLoading && !loading && !error && modelId && voices.length === 0 ? <div className="empty-state panel-state"><CircleOff aria-hidden="true" size={20} /><div className="empty-state-content"><p>{t("voicesPage.noVoices")}</p></div></div> : null}
    {voices.length > 0 ? <ul className="voice-list" aria-label={t("voicesPage.runtimeVoices")}>{voices.map((voice) => { const reasonId = `preview-capability-reason-${voice.id}`; return <li key={voice.id}><Card className="voice-card"><span className="fact-icon" aria-hidden="true"><Mic2 size={19} /></span><div className="voice-card-copy"><strong>{voice.label}</strong><small>{voice.id} · {voice.capabilities.join(", ") || t("voicesPage.noCapabilities")}</small></div><div><Button size="small" variant="neutral" aria-label={previewingVoiceId === voice.id ? t("voicesPage.previewing", { label: voice.label }).replace("…", "") : t("voicesPage.preview", { label: voice.label })} aria-describedby={!previewSupported ? reasonId : undefined} aria-busy={previewingVoiceId === voice.id} onClick={() => void handlePreview(voice)} disabled={Boolean(previewingVoiceId) || !modelId || !previewSupported}>{previewingVoiceId === voice.id ? <LoaderCircle className="status-spinner" aria-hidden="true" size={16} /> : <Play aria-hidden="true" size={16} />}{previewingVoiceId === voice.id ? t("voicesPage.previewingButton") : t("voicesPage.previewButton")}</Button>{!previewSupported ? <p className="component-field-hint" id={reasonId}>{t(previewReason)}</p> : null}</div>{previewError?.voiceId === voice.id ? <Alert className="voice-preview-error" variant="error" role="alert">{t(previewError.message.key, previewError.message.values)}</Alert> : null}</Card></li>; })}</ul> : null}
  </div>;
}
