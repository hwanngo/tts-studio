import { CheckCircle2, CircleOff, Cpu, Folder, Gauge } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import type { SystemStatusResult, WorkerStatus } from "../../lib/api";
import { formatNumber } from "../../lib/i18n";

function workerMessage(message: string, t: (key: string) => string) {
  if (!message) return t("overview.noWorkerMessage");
  if (message === "Worker is ready.") return t("overview.workerReadyMessage");
  if (message === "Worker unavailable.") return t("overview.workerUnavailableMessage");
  return t("overview.unknownWorkerMessage");
}

type OverviewPageProps = { systemStatus: SystemStatusResult };

function WorkerRow({ worker, t }: { worker: WorkerStatus; t: (key: string) => string }) {
  const ready = worker.status === "ready";
  return <li className="worker-row"><span className="worker-icon" aria-hidden="true"><Cpu size={18} strokeWidth={1.8} /></span><span className="worker-name"><strong>{worker.engine_id}</strong><small>{workerMessage(worker.message, t)}</small></span><span className={ready ? "status-ready" : "status-unavailable"}>{ready ? <CheckCircle2 aria-hidden="true" size={17} /> : <CircleOff aria-hidden="true" size={17} />}{ready ? t("overview.ready") : t("overview.unhealthy")}</span></li>;
}

export function OverviewPage({ systemStatus }: OverviewPageProps) {
  const { t, i18n } = useTranslation();
  return <div className="page-stack"><header className="page-header"><div><p className="eyebrow">{t("overview.eyebrow")}</p><h1>{t("overview.title")}</h1><p>{t("overview.description")}</p></div></header>
    {systemStatus.state === "loading" ? <Alert role="status" aria-live="polite">{t("overview.checking")}</Alert> : null}
    {systemStatus.state === "error" ? <Alert variant="error" role="alert" title={t("overview.errorTitle")}>{t("overview.errorDescription")}</Alert> : null}
    {systemStatus.state === "ready" ? <><section className="fact-grid" aria-label={t("overview.coreDetails")}><Card className="fact-card"><span className="fact-icon" aria-hidden="true"><Gauge size={20} strokeWidth={1.8} /></span><div><p>{t("overview.coreVersion")}</p><strong>{systemStatus.value.version}</strong></div></Card><Card className="fact-card fact-card-wide"><span className="fact-icon" aria-hidden="true"><Folder size={20} strokeWidth={1.8} /></span><div><p>{t("overview.dataDirectory")}</p><strong className="path-value">{systemStatus.value.data_dir}</strong></div></Card></section><Card className="overview-panel" aria-labelledby="worker-heading"><div className="section-heading"><div><h2 id="worker-heading">{t("overview.workerReadiness")}</h2><p>{t("overview.workerDescription")}</p></div><span className={systemStatus.value.workers.length === 0 ? "status-neutral" : systemStatus.value.workers.some((worker) => worker.status === "unhealthy") ? "status-unavailable" : "status-ready"}>{systemStatus.value.workers.length === 0 || systemStatus.value.workers.some((worker) => worker.status === "unhealthy") ? <CircleOff aria-hidden="true" size={17} /> : <CheckCircle2 aria-hidden="true" size={17} />}{t("overview.supervised", { count: formatNumber(systemStatus.value.workers.length, i18n.language) })}</span></div>{systemStatus.value.workers.length > 0 ? <ul className="worker-list">{systemStatus.value.workers.map((worker) => <WorkerRow key={worker.engine_id} worker={worker} t={t} />)}</ul> : <div className="empty-state"><CircleOff aria-hidden="true" size={20} /><p>{t("overview.noWorkers")}</p></div>}</Card></> : null}
  </div>;
}
