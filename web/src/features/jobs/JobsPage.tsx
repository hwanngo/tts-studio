import { CircleOff, LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { GenerationJobResponse } from "../../generated/api";
import { cancelGeneration, fetchGenerations } from "../../lib/api";
import { errorMessageKey, formatDate, formatNumber, localizedMessage, type LocalizedMessage } from "../../lib/i18n";

const POLLABLE_STATES = new Set(["queued", "loading", "generating", "finalizing"]);
const CANCELLABLE_STATES = new Set(["queued", "loading", "generating"]);
const KNOWN_JOB_STATES = new Set(["queued", "loading", "generating", "finalizing", "completed", "failed", "cancelled"]);

export function JobsPage() {
  const { t, i18n } = useTranslation();
  const [jobs, setJobs] = useState<GenerationJobResponse[]>([]); const [state, setState] = useState<"loading" | "ready" | "error">("loading"); const [error, setError] = useState<LocalizedMessage | null>(null); const polling = state === "ready" && jobs.some((item) => POLLABLE_STATES.has(item.state)); const [cancelBusy, setCancelBusy] = useState<string | null>(null); const [cancelError, setCancelError] = useState<{ jobId: string; message: LocalizedMessage } | null>(null);
  const load = useCallback(() => fetchGenerations().then((items) => { setJobs(items); setState("ready"); }).catch((reason: unknown) => { setError(localizedMessage(reason, "errors.unknown")); setState("error"); }), []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (!polling) return; const timer = window.setInterval(() => { void load(); }, 1000); return () => window.clearInterval(timer); }, [load, polling]);
  async function cancel(job: GenerationJobResponse) { setCancelBusy(job.id); setCancelError(null); try { const updated = await cancelGeneration(job.id); setJobs((items) => items.map((item) => item.id === updated.id ? updated : item)); } catch (reason: unknown) { setCancelError({ jobId: job.id, message: localizedMessage(reason, "errors.unknown") }); } finally { setCancelBusy(null); } }
  const stateLabel = (value: string) => KNOWN_JOB_STATES.has(value) ? t(`status.${value}`) : t("status.unknown");
  return <div className="page-stack"><header className="page-header"><div><p className="eyebrow">{t("jobsPage.eyebrow")}</p><h1>{t("jobsPage.title")}</h1><p>{t("jobsPage.description")}</p></div></header>
    {state === "loading" ? <Alert role="status" aria-live="polite">{t("jobsPage.loading")}</Alert> : null}
    {state === "error" ? <Alert variant="error" role="alert" title={t("jobsPage.errorTitle")}>{error ? t(error.key, error.values) : t("errors.unknown")}</Alert> : null}
    {state === "ready" && jobs.length === 0 ? <Card className="empty-state panel-state"><span className="empty-state-icon"><CircleOff aria-hidden="true" size={22} /></span><div className="empty-state-content"><h2>{t("jobsPage.emptyTitle")}</h2><p>{t("jobsPage.emptyDescription")}</p><a className="button-link" href="/">{t("jobsPage.openStudio")}</a></div></Card> : null}
    {state === "ready" && jobs.length > 0 ? <ul className="job-list" aria-label={t("jobsPage.listLabel")}>{jobs.map((job) => <li key={job.id}><Card className="job-card"><div className="model-card-header"><div><h2>{job.text.length > 72 ? `${job.text.slice(0, 72)}…` : job.text}</h2><p className="mono-value">{job.id} · {job.engine_id} · {job.voice_id}</p></div><span className={`model-status model-status-${job.state}`}><span aria-hidden="true">{job.state === "failed" || job.state === "cancelled" ? <CircleOff size={17} /> : <LoaderCircle size={17} />}</span>{stateLabel(job.state)}</span></div><div role="status" aria-live="polite" aria-atomic="true" className="sr-only">{job.id}: {stateLabel(job.state)}</div><dl className="model-facts compact-facts"><div><dt>{t("jobsPage.model")}</dt><dd>{job.model_id}</dd></div><div><dt>{t("jobsPage.audio")}</dt><dd>{job.artifact_id ? t("jobsPage.finalized") : job.state === "completed" ? t("jobsPage.notRetained") : t("jobsPage.notAvailable")}</dd></div><div><dt>{t("jobsPage.progress")}</dt><dd>{t("jobsPage.bytesFrames", { bytes: formatNumber(job.bytes_written, i18n.language), frames: formatNumber(job.frame_count, i18n.language) })}</dd></div><div><dt>{t("jobsPage.updated")}</dt><dd>{formatDate(job.updated_at, i18n.language)}</dd></div></dl>{job.error ? <Alert variant="error" role="alert" title={t(errorMessageKey(String(job.error.code ?? "")), { defaultValue: t("errors.unknown") })}>{t(errorMessageKey(String(job.error.code ?? "")), { defaultValue: t("errors.unknown") })}</Alert> : null}{cancelError?.jobId === job.id ? <Alert variant="error" role="alert">{t(cancelError.message.key, cancelError.message.values)}</Alert> : null}{CANCELLABLE_STATES.has(job.state) ? <Button type="button" variant="outline" disabled={cancelBusy === job.id || job.cancellation_requested} onClick={() => void cancel(job)}>{cancelBusy === job.id ? t("jobsPage.cancelling") : job.cancellation_requested ? t("jobsPage.cancellationRequested") : t("jobsPage.cancel")}</Button> : null}</Card></li>)}</ul> : null}
  </div>;
}
