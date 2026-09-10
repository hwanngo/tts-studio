import { CheckCircle2, CircleOff, LoaderCircle, WandSparkles } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Combobox } from "@/components/ui/combobox";
import { Textarea } from "@/components/ui/textarea";
import type { GenerationJobResponse, ModelInstallationResponse, VoiceResponse, WorkerRuntimeResponse } from "../../generated/api";
import {
  ApiError,
  cancelGeneration,
  consumeGenerationPcm,
  createGeneration,
  fetchGenerations,
  fetchModels,
  fetchSavedVoices,
  fetchSettings,
  fetchRuntime,
  fetchVoices,
  type SystemStatusResult,
} from "../../lib/api";
import { errorMessageKey, formatNumber, localizedMessage, type LocalizedMessage } from "../../lib/i18n";

type StudioPageProps = { systemStatus: SystemStatusResult };
const POLLING_STATES = new Set(["queued", "loading", "generating", "finalizing"]);
const CANCELLABLE_STATES = new Set(["queued", "loading", "generating"]);
const OPTION_DEFAULTS = { speed: 1, pitch: 0, volume: 1 } as const;

type OptionName = keyof typeof OPTION_DEFAULTS;
function engineIdForModel(model: ModelInstallationResponse): string { return model.engine_installation_id.split("@", 1)[0]; }
function stateLabel(state: string): string { return state.charAt(0).toUpperCase() + state.slice(1); }

function pcmToWav(chunks: Uint8Array[]): Blob {
  const byteLength = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
  const buffer = new ArrayBuffer(44 + byteLength);
  const view = new DataView(buffer);
  const writeString = (offset: number, value: string) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)));
  writeString(0, "RIFF");
  view.setUint32(4, 36 + byteLength, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 48000, true);
  view.setUint32(28, 96000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, byteLength, true);
  const output = new Uint8Array(buffer, 44);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

function statusIcon(state: string) {
  if (state === "completed") return <CheckCircle2 aria-hidden="true" size={17} />;
  if (state === "failed" || state === "cancelled") return <CircleOff aria-hidden="true" size={17} />;
  return <LoaderCircle aria-hidden="true" size={17} />;
}

function CoreStatus({ result, t, locale }: { result: SystemStatusResult; t: (key: string, options?: Record<string, unknown>) => string; locale: string }) {
  if (result.state === "loading") return <Alert role="status" aria-live="polite">{t("studioPage.coreChecking")}</Alert>;
  if (result.state === "error") return <Alert variant="error" role="alert">{t("studioPage.coreOffline")}</Alert>;
  const readyWorkers = result.value.workers.filter((worker) => worker.status === "ready").length;
  return <dl className="compact-status" aria-label={t("studioPage.localStatus")}><div><dt>{t("studioPage.core")}</dt><dd className={result.value.status === "healthy" ? "status-ready" : "status-unavailable"}>{result.value.status === "healthy" ? t("studioPage.coreOnline") : t("studioPage.coreUnavailable")}</dd></div><div><dt>{t("studioPage.workers")}</dt><dd className={readyWorkers > 0 ? "status-ready" : "status-neutral"}>{formatNumber(readyWorkers, locale)} {readyWorkers === 1 ? t("studioPage.workerReady") : t("studioPage.workersReady")}</dd></div></dl>;
}

function JobStatus({ job, t }: { job: GenerationJobResponse; t: (key: string, options?: Record<string, unknown>) => string }) {
  const label = t(`status.${job.state}`, { defaultValue: t("studioPage.unknownState") });
  return <div className={`generation-status generation-status-${job.state}`} role="status" aria-live="polite">{statusIcon(job.state)}<span>{label}</span>{job.state === "queued" ? <small>{t("studioPage.generationQueued")}</small> : null}{job.state === "generating" ? <small>{t("studioPage.workerGenerating")}</small> : null}{job.state === "finalizing" ? <small>{t("studioPage.preparingWav")}</small> : null}</div>;
}

export function StudioPage({ systemStatus }: StudioPageProps) {
  const { t, i18n } = useTranslation();
  const [models, setModels] = useState<ModelInstallationResponse[]>([]);
  const [runtimeWorkers, setRuntimeWorkers] = useState<WorkerRuntimeResponse[]>([]);
  const [modelId, setModelId] = useState("");
  const [options, setOptions] = useState(OPTION_DEFAULTS);
  const [changedOptions, setChangedOptions] = useState<Set<OptionName>>(new Set());
  const [voices, setVoices] = useState<VoiceResponse[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [text, setText] = useState("");
  const [retainArtifact, setRetainArtifact] = useState(true);
  const retentionResolved = useRef(false);
  const retentionOverridden = useRef(false);
  const [job, setJob] = useState<GenerationJobResponse | null>(null);
  const [loadError, setLoadError] = useState<LocalizedMessage | null>(null);
  const [voiceError, setVoiceError] = useState<LocalizedMessage | null>(null);
  const [formError, setFormError] = useState<LocalizedMessage | null>(null);
  const [busy, setBusy] = useState(false);
  const [livePcmBytes, setLivePcmBytes] = useState(0);
  const [livePcmError, setLivePcmError] = useState<LocalizedMessage | null>(null);
  const [pcmAnnouncement, setPcmAnnouncement] = useState<LocalizedMessage | null>(null);
  const [ephemeralAudioUrl, setEphemeralAudioUrl] = useState<string | null>(null);
  const [pcmStreamCompleteFor, setPcmStreamCompleteFor] = useState<string | null>(null);
  const pcmGenerationToken = useRef(0);
  const pcmChunks = useRef<Uint8Array[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([fetchModels(controller.signal), fetchGenerations(controller.signal)]).then(
      ([installedModels, generations]) => { const availableModels = Array.isArray(installedModels) ? installedModels : []; const availableGenerations = Array.isArray(generations) ? generations : []; const latestGeneration = availableGenerations.reduce<GenerationJobResponse | null>((latest, current) => !latest || current.created_at > latest.created_at ? current : latest, null); setModels(availableModels); setModelId((current) => current || availableModels[0]?.id || ""); setJob((current) => current || latestGeneration); },
      (reason: unknown) => { if (!controller.signal.aborted) setLoadError(localizedMessage(reason, "studioPage.coreOffline")); },
    );
    fetchRuntime(controller.signal).then(
      (value) => { if (!controller.signal.aborted) setRuntimeWorkers(Array.isArray(value.workers) ? value.workers : []); },
      () => undefined,
    );
    fetchSettings(controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) {
          retentionResolved.current = true;
          if (!retentionOverridden.current) {
            setRetainArtifact(value.retain_audio_by_default);
          }
        }
      },
      () => undefined,
    );
    return () => controller.abort();
  }, []);

  useEffect(() => {
    return () => {
      if (ephemeralAudioUrl) URL.revokeObjectURL(ephemeralAudioUrl);
    };
  }, [ephemeralAudioUrl]);

  useEffect(() => {
    if (!job || job.state !== "completed" || job.artifact_url || pcmStreamCompleteFor !== job.id || pcmChunks.current.length === 0) return;
    setEphemeralAudioUrl(URL.createObjectURL(pcmToWav(pcmChunks.current)));
    pcmChunks.current = [];
  }, [job?.id, job?.state, job?.artifact_url, pcmStreamCompleteFor]);

  useEffect(() => {
    setVoices([]);
    setVoiceId("");
    setVoiceError(null);
    if (!modelId) { setVoices([]); setVoiceId(""); return; }
    const controller = new AbortController();
    setVoiceError(null);
    fetchVoices(modelId, controller.signal).then(
      (items) => { if (!controller.signal.aborted) { setVoices(items); setVoiceId(items[0]?.id || ""); } },
      (error: unknown) => { if (!controller.signal.aborted) { setVoices([]); setVoiceId(""); setVoiceError(localizedMessage(error, "studioPage.voiceDiscoveryFailed")); } },
    );
    fetchSavedVoices(modelId, controller.signal).then(
      (saved) => { if (!controller.signal.aborted) setVoices((current) => [...current, ...saved.map((item) => ({ id: `saved:${item.id}`, label: item.label, capabilities: ["saved"] }))]); },
      () => undefined,
    );
    return () => controller.abort();
  }, [modelId]);

  useEffect(() => {
    if (!job || !POLLING_STATES.has(job.state)) return;
    const timer = window.setInterval(() => { fetchGenerations().then((items) => { const current = items.find((item) => item.id === job.id); if (current) setJob(current); }).catch(() => undefined); }, 700);
    return () => window.clearInterval(timer);
  }, [job]);

  useEffect(() => {
    if (!job || job.artifact_url || !POLLING_STATES.has(job.state)) return;
    const controller = new AbortController();
    const token = pcmGenerationToken.current + 1;
    pcmGenerationToken.current = token;
    const isCurrent = () => !controller.signal.aborted && pcmGenerationToken.current === token;
    setLivePcmBytes(0);
    setLivePcmError(null);
    setPcmStreamCompleteFor(null);
    pcmChunks.current = [];
    setPcmAnnouncement({ key: "studioPage.pcmStarted" });
    consumeGenerationPcm(job.id, (chunk) => {
      if (!isCurrent()) return;
      pcmChunks.current.push(new Uint8Array(chunk));
      setLivePcmBytes((current) => current + chunk.byteLength);
    }, controller.signal)
      .then(() => { if (isCurrent()) setPcmStreamCompleteFor(job.id); })
      .catch((error: unknown) => {
        if (isCurrent()) {
          const message = localizedMessage(error, "studioPage.pcmUnavailable");
          setLivePcmError(message);
          setPcmAnnouncement(message);
        }
      });
    return () => {
      pcmGenerationToken.current += 1;
      controller.abort();
    };
  }, [job?.id, job?.artifact_url]);

  const selectedModel = models.find((item) => item.id === modelId);
  const selectedWorker = selectedModel ? runtimeWorkers.find((worker) => worker.engine_id === engineIdForModel(selectedModel)) : undefined;
  const capabilities = new Set(selectedWorker?.capabilities ?? []);
  const optionSupported = (name: OptionName) => capabilities.has(name);
  const setOption = (name: OptionName, value: number) => {
    setOptions((current) => ({ ...current, [name]: value }));
    setChangedOptions((current) => new Set(current).add(name));
  };
  const resetOptions = () => { setOptions(OPTION_DEFAULTS); setChangedOptions(new Set()); };

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setFormError(null);
    if (!text.trim()) { setFormError({ key: "studioPage.textRequired" }); return; }
    if (!modelId || !voiceId) { setFormError({ key: "studioPage.modelFirst" }); return; }
    setBusy(true);
    setEphemeralAudioUrl(null);
    try {
      const optionPayload = Object.fromEntries([...changedOptions].filter((name) => optionSupported(name)).map((name) => [name, options[name]]));
      setJob(await createGeneration({ model_id: modelId, ...(voiceId.startsWith("saved:") ? { saved_voice_id: voiceId.slice(6) } : { voice_id: voiceId }), text: text.trim(), ...(Object.keys(optionPayload).length ? optionPayload : {}), ...(retentionResolved.current || retentionOverridden.current ? { retain_artifact: retainArtifact } : {}) }));
    }
    catch (error: unknown) { setFormError(localizedMessage(error, "studioPage.generationStartFailed")); }
    finally { setBusy(false); }
  }

  async function cancel() {
    if (!job) return;
    setBusy(true);
    try { setJob(await cancelGeneration(job.id)); }
    catch (error: unknown) { setFormError(localizedMessage(error, "studioPage.generationCancelFailed")); }
    finally { setBusy(false); }
  }

  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">{t("studioPage.eyebrow")}</p><h1>{t("studioPage.title")}</h1><p>{t("studioPage.description")}</p></div><CoreStatus result={systemStatus} t={t} locale={i18n.language} /></header>
    {loadError && systemStatus.state !== "error" ? <Alert variant="error" role="alert">{t(loadError.key, loadError.values)}</Alert> : null}
    <section aria-labelledby="composer-heading">
      <Card className="studio-panel">
        <div className="section-heading"><div><h2 id="composer-heading">{t("studioPage.setup")}</h2><p>{t("studioPage.setupDescription")}</p></div><Link className="text-link" to="/history">{t("studioPage.viewHistory")}</Link></div>
      <form onSubmit={submit} aria-label={t("studioPage.formLabel")} aria-describedby={formError ? "generation-form-error" : undefined}><fieldset disabled={busy}><legend className="sr-only">{t("studioPage.optionsLegend")}</legend>
        <Textarea label={t("studioPage.text")} id="speech-text" name="text" placeholder={t("studioPage.textPlaceholder")} rows={10} value={text} onChange={(event) => setText(event.target.value)} hint={`${t("studioPage.textLocal")} ${selectedWorker?.capabilities?.includes("inline_cues") ? t("studioPage.inlineCues") : t("studioPage.inlineCuesUnavailable")}`} />
        <div className="prosody-grid" aria-label={t("studioPage.voiceControls")}>
          {(["speed", "pitch", "volume"] as const).map((name) => {
            const supported = optionSupported(name);
            const min = name === "speed" ? 0.25 : name === "pitch" ? -1 : 0;
            const max = name === "speed" ? 4 : name === "pitch" ? 1 : 2;
            const hint = supported ? t("studioPage.range", { min: min.toFixed(2), max: max.toFixed(2) }) : t("studioPage.unavailableCapability");
            return <div className="range-field" key={name}>
              <div className="range-field-label"><label htmlFor={`option-${name}`}>{t(`studioPage.${name}`)}</label><span aria-live="polite" aria-atomic="true">{options[name].toFixed(2)}</span></div>
              <input id={`option-${name}`} name={name} type="range" step="0.05" min={min} max={max} value={options[name]} onChange={(event) => setOption(name, Number(event.target.value))} disabled={!selectedWorker || !supported} aria-describedby={`option-${name}-hint`} />
              <p className="component-field-hint" id={`option-${name}-hint`}>{hint}</p>
            </div>;
          })}
          <Button className="reset-controls" type="button" variant="outline" onClick={resetOptions}>{t("studioPage.resetControls")}</Button>
        </div>
        <div className="field-grid">
          <Combobox
            label={t("studioPage.model")}
            id="model"
            name="model"
            value={modelId}
            onValueChange={setModelId}
            placeholder={t("studioPage.modelPlaceholder")}
            options={models.map((modelOption) => ({
              value: modelOption.id,
              label: `${modelOption.repository_id} · ${modelOption.runtime_variant.toUpperCase()}`,
              group: modelOption.runtime_variant.toUpperCase(),
            }))}
            emptyText={t("studioPage.noModels")}
          />
          <Combobox
            label={t("studioPage.runtimeVoice")}
            id="voice"
            name="voice"
            value={voiceId}
            onValueChange={setVoiceId}
            disabled={!modelId || voices.length === 0}
            error={voiceError ? t(voiceError.key, voiceError.values) : undefined}
            placeholder={voiceError ? t("studioPage.voiceUnavailable") : modelId ? t("studioPage.voicePlaceholder") : t("studioPage.modelFirst")}
            options={voices.map((voice) => ({
              value: voice.id,
              label: voice.id.startsWith("saved:") ? `${voice.label} (${t("studioPage.saved")})` : voice.label,
              group: voice.id.startsWith("saved:") ? t("studioPage.savedVoices") : t("studioPage.runtimeVoices"),
            }))}
            emptyText={t("studioPage.noVoices")}
          />
        </div>
        <label className="checkbox-field"><input type="checkbox" checked={retainArtifact} onChange={(event) => { retentionOverridden.current = true; setRetainArtifact(event.target.checked); }} /> <span>{t("studioPage.keepHistory")}</span><small>{t("studioPage.keepHistoryHint")}</small></label>
        <div className="generate-row"><Button type="submit" disabled={busy || !modelId || !voiceId}><WandSparkles aria-hidden="true" size={18} /> {t("studioPage.generate")}</Button><p>{models.length === 0 ? t("studioPage.installModel") : t("studioPage.progress")}</p></div>
      </fieldset></form>
      {formError ? <Alert id="generation-form-error" variant="error" role="alert">{t(formError.key, formError.values)}</Alert> : null}
      </Card>
    </section>
    {job ? <section aria-labelledby="generation-heading"><Card className="generation-panel"><div className="section-heading"><div><h2 id="generation-heading">{t("studioPage.current")}</h2><p className="mono-value">{job.id}</p></div>{CANCELLABLE_STATES.has(job.state) ? <Button type="button" variant="outline" disabled={busy || job.cancellation_requested} onClick={cancel}>{job.cancellation_requested ? t("studioPage.cancellationRequested") : t("studioPage.cancel")}</Button> : null}</div><JobStatus job={job} t={t} />{job.state === "generating" ? <><div className="live-pcm-status"><strong>{t("studioPage.livePcm")}</strong><span>{livePcmError ? t(livePcmError.key, livePcmError.values) : t("studioPage.bytesReceived", { bytes: formatNumber(livePcmBytes, i18n.language) })}</span></div><div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{pcmAnnouncement ? t(pcmAnnouncement.key, pcmAnnouncement.values) : null}</div></> : null}{job.error ? <Alert variant="error" role="alert" title={t(errorMessageKey(String(job.error.code ?? "")), { defaultValue: t("studioPage.generationError") })}>{t(errorMessageKey(job.error.code ? String(job.error.code) : undefined), { defaultValue: t("studioPage.generationFailed") })}</Alert> : null}{job.state === "completed" && (job.artifact_url || ephemeralAudioUrl) ? <div className="audio-result"><audio controls preload="metadata" src={job.artifact_url ?? ephemeralAudioUrl ?? undefined} aria-label={t("studioPage.finalizedAudio")}>{t("studioPage.browserAudio")}</audio>{job.artifact_url ? <a className="button-link" href={job.artifact_url} download={`tts-studio-${job.artifact_id ?? job.id}.wav`}>{t("studioPage.downloadWav")}</a> : <span className="muted-copy">{t("studioPage.sessionOnly")}</span>}</div> : null}{job.state === "completed" && !job.artifact_url && !ephemeralAudioUrl ? <p className="muted-copy">{t("studioPage.notRetained")}</p> : null}</Card></section> : null}
  </div>;
}
