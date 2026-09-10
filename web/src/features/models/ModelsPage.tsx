import {
  CheckCircle2,
  CircleOff,
  Download,
  HardDrive,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import i18nInstance from "../../i18n";
import { errorMessageKey, formatBytes, formatNumber } from "../../lib/i18n";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Combobox } from "@/components/ui/combobox";
import type {
  AdapterCompatibilityResponse,
  DownloadJobResponse,
  ModelInstallationResponse,
  ModelValidationResponse,
} from "../../generated/api";
import {
  ApiError,
  cancelDownload,
  fetchDownloads,
  fetchModels,
  removeModel,
  startDownload,
  subscribeToModelEvents,
  updateModelReplicas,
  validateModel,
} from "../../lib/api";
import {
  isActiveDownload,
  jobError,
  modelError,
  modelEvidence,
  progressPercent,
  replicaSummary,
  safeManagedPath,
  safeMessage,
} from "./model-state";

type TranslationDescriptor = { key: string; values?: Record<string, unknown> };
type PageError = {
  title: TranslationDescriptor;
  message: TranslationDescriptor;
  actionLabel?: TranslationDescriptor;
  action?: () => void;
  fieldLink?: { href: string; label: TranslationDescriptor };
};

type SnapshotResource = "models" | "downloads";

function StatusIcon({ status }: { status: string }) {
  if (status === "completed" || status === "compatible") {
    return <CheckCircle2 aria-hidden="true" size={17} />;
  }
  if (status === "failed" || status === "cancelled" || status === "incompatible") {
    return <CircleOff aria-hidden="true" size={17} />;
  }
  return <LoaderCircle aria-hidden="true" size={17} />;
}

function CompatibilityCard({ result }: { result: AdapterCompatibilityResponse }) {
  const { t, i18n } = useTranslation();
  const status = result.compatible
    ? "compatible"
    : result.available
      ? "incompatible"
      : "unavailable";

  return (
    <article aria-label={t("modelsPage.compatibilityAria", { engine: result.engine_id })}>
      <Card className="model-card">
        <div className="model-card-header">
        <div>
          <h3>{result.engine_id}</h3>
          <p>{t("modelsPage.engineAdapter", { version: result.engine_version })}</p>
        </div>
        <span className={`model-status model-status-${status}`}>
          <StatusIcon status={status} />
          {result.compatible ? t("modelsPage.compatible") : result.available ? t("modelsPage.incompatible") : t("modelsPage.unavailable")}
        </span>
      </div>

      <dl className="model-facts compact-facts">
        <div>
          <dt>{t("modelsPage.resolvedCommit")}</dt>
          <dd className="mono-value">{result.resolved_commit?.slice(0, 12) ?? t("modelsPage.notResolved")}</dd>
        </div>
        <div>
          <dt>{t("modelsPage.estimatedDownload")}</dt>
          <dd>{result.estimated_bytes === null ? t("modelsPage.sizeUnavailable") : formatBytes(result.estimated_bytes, i18n.language)}</dd>
        </div>
        <div>
          <dt>{t("modelsPage.variants")}</dt>
          <dd>
            {result.available_variants.length > 0
              ? result.available_variants.map((item) => item.label).join(", ")
              : t("modelsPage.noneReported")}
          </dd>
        </div>
        <div>
          <dt>{t("modelsPage.requiredFiles")}</dt>
          <dd>{result.required_files.length > 0 ? formatNumber(result.required_files.length, i18n.language) : t("modelsPage.noneReported")}</dd>
        </div>
      </dl>

      {result.evidence.length > 0 ? (
        <ul className="evidence-list" aria-label={t("modelsPage.evidenceAria", { engine: result.engine_id })}>
          {result.evidence.map((item) => (
            <li key={`${item.code}-${item.message}`}>
              {safeMessage(item.message, t("modelsPage.evidenceUnavailable"))}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted-copy">{t("modelsPage.noEvidence")}</p>
      )}
      </Card>
    </article>
  );
}

type DownloadCardProps = {
  job: DownloadJobResponse;
  busy: boolean;
  onCancel: (job: DownloadJobResponse) => void;
  onRetry: (job: DownloadJobResponse) => void;
};

function DownloadCard({ job, busy, onCancel, onRetry }: DownloadCardProps) {
  const { t, i18n } = useTranslation();
  const percent = progressPercent(job);
  const error = jobError(job, t("modelsPage.downloadError"));
  const status = job.error?.code === "recovery_required" ? t("modelsPage.recoveryRequired") : t(`status.${job.state || job.phase}`, { defaultValue: t("status.unknown") });
  const statusKind = job.state === "failed" || job.state === "cancelled" ? job.state : job.state;

  return (
    <li>
      <Card className="download-card">
        <div className="model-card-header">
        <div>
          <h3>{job.repository_id}</h3>
          <p>{job.requested_revision ? t("modelsPage.revisionLabel", { revision: job.requested_revision }) : t("modelsPage.defaultRevision")}</p>
        </div>
        <span className={`model-status model-status-${statusKind}`}>
          <StatusIcon status={statusKind} />
          {status}
        </span>
      </div>

      <div className="progress-stack">
        <progress
          aria-label={t("modelsPage.downloadProgress", { repository: job.repository_id })}
          aria-valuetext={
            percent === null
              ? t("modelsPage.downloadedTotalUnknown", { bytes: formatBytes(job.bytes_downloaded, i18n.language) })
              : t("modelsPage.percentDownloaded", { percent })
          }
          max={job.total_bytes ?? undefined}
          value={job.total_bytes === null ? undefined : job.bytes_downloaded}
        />
        <p>
          {percent === null
            ? t("modelsPage.downloadedTotalUnknownInline", { bytes: formatBytes(job.bytes_downloaded, i18n.language) })
            : t("modelsPage.downloadedOf", { downloaded: formatBytes(job.bytes_downloaded, i18n.language), total: formatBytes(job.total_bytes ?? 0, i18n.language), percent })}
        </p>
        <p className="phase-copy">{t("modelsPage.phase", { phase: t(`status.${job.phase}`, { defaultValue: t("status.unknown") }) })}</p>
      </div>

      {error ? (
        <Alert variant="error" role="alert" title={t(errorMessageKey(error.code), { defaultValue: t("errors.unknown") })}>{t(errorMessageKey(error.code), { defaultValue: t("errors.unknown") })}</Alert>
      ) : null}

      <div className="card-actions">
        {isActiveDownload(job) ? (
          <Button
            type="button"
            variant="outline"
            disabled={busy || job.cancellation_requested}
            onClick={() => onCancel(job)}
          >
            <CircleOff aria-hidden="true" size={17} />
            {job.cancellation_requested ? t("modelsPage.cancellationRequested") : t("modelsPage.cancelDownload", { repository: job.repository_id })}
          </Button>
        ) : null}
        {(job.state === "cancelled" || (job.state === "failed" && error?.retryable)) ? (
          <Button type="button" variant="outline" onClick={() => onRetry(job)}>
            <RotateCcw aria-hidden="true" size={17} />
            {t("modelsPage.tryAgain", { repository: job.repository_id })}
          </Button>
        ) : null}
        </div>
      </Card>
    </li>
  );
}

type InstallationCardProps = {
  model: ModelInstallationResponse;
  busy: boolean;
  onUpdate: (model: ModelInstallationResponse) => void;
  onRemove: (model: ModelInstallationResponse) => void;
  onReplicaUpdate: (model: ModelInstallationResponse, count: number) => void;
};

function InstallationCard({ model, busy, onUpdate, onRemove, onReplicaUpdate }: InstallationCardProps) {
  const { t, i18n } = useTranslation();
  const evidence = modelEvidence(model, t("modelsPage.evidenceUnavailable"));
  const error = modelError(model, t("modelsPage.modelError"));

  return (
    <article aria-label={t("modelsPage.installationAria", { repository: model.repository_id })}>
      <Card className="model-card">
        <div className="model-card-header">
        <div>
          <h3>{model.repository_id}</h3>
          <p>{model.engine_installation_id}</p>
        </div>
        <span className="model-status model-status-completed">
          <HardDrive aria-hidden="true" size={17} />
          {t("modelsPage.installed")}
        </span>
      </div>

      <dl className="model-facts">
        <div>
          <dt>{t("modelsPage.resolvedCommit")}</dt>
          <dd className="mono-value">{model.resolved_commit.slice(0, 12)}</dd>
        </div>
        <div>
          <dt>{t("modelsPage.runtimeVariant")}</dt>
          <dd>{model.runtime_variant.toUpperCase()}</dd>
        </div>
        <div>
          <dt>{t("modelsPage.installedSize")}</dt>
          <dd>{formatBytes(model.byte_size, i18n.language)}</dd>
        </div>
        <div>
          <dt>{t("modelsPage.managedCache")}</dt>
          <dd className="mono-value path-value">{safeManagedPath(model.cache_path, t("modelsPage.managedUnavailable"))}</dd>
        </div>
        <div>
          <dt>{t("modelsPage.loadState")}</dt>
          <dd>
            <span>{t("modelsPage.desired", { value: t(`status.${model.desired_load_state}`, { defaultValue: t("status.unknown") }) })}</span>
            <span>{t("modelsPage.observed", { value: t(`status.${model.observed_load_state}`, { defaultValue: t("status.unknown") }) })}</span>
          </dd>
        </div>
        <div>
          <dt>{t("modelsPage.workerReplicas")}</dt>
          <dd>{(() => { const summary = replicaSummary(model); return <><span>{t(summary.active === 1 ? "modelsPage.replicaSummaryOne" : "modelsPage.replicaSummaryMany", { ready: formatNumber(summary.ready, i18n.language), active: formatNumber(summary.active, i18n.language) })}</span><span>{t("modelsPage.desired", { value: formatNumber(model.desired_replicas, i18n.language) })}</span></>; })()}</dd>
        </div>
      </dl>

      {evidence.length > 0 ? (
        <div className="model-evidence">
          <strong>{t("modelsPage.compatibilityEvidence")}</strong>
          <ul className="evidence-list">
            {evidence.map((message) => <li key={message}>{message}</li>)}
          </ul>
        </div>
      ) : null}

      {error ? (
        <Alert variant="error" role="alert" title={t(errorMessageKey(error.code), { defaultValue: t("errors.unknown") })}>{t(errorMessageKey(error.code), { defaultValue: t("errors.unknown") })}</Alert>
      ) : null}

      <div className="card-actions">
        <Button type="button" variant="outline" onClick={() => onUpdate(model)}>
          <RefreshCw aria-hidden="true" size={17} />
          {t("modelsPage.checkUpdate")}
        </Button>
        <Button type="button" variant="outline" disabled={busy} onClick={() => onRemove(model)}>
          <Trash2 aria-hidden="true" size={17} />
          {t("modelsPage.removeInstallation")}
        </Button>
      </div>
      <form className="installation-replica-form" onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); onReplicaUpdate(model, Number(form.get("desired-replicas"))); }}>
        <Input label={t("modelsPage.desiredReplicas")} id={`desired-replicas-${model.id}`} name="desired-replicas" type="number" min={1} max={8} defaultValue={model.desired_replicas} disabled={busy} />
        <div className="form-action">
          <span className="form-action-label" aria-hidden="true" />
          <Button type="submit" variant="outline" disabled={busy}>{t("modelsPage.applyReplicas")}</Button>
        </div>
      </form>
      </Card>
    </article>
  );
}

export function ModelsPage() {
  const { t, i18n } = useTranslation();
  const [models, setModels] = useState<ModelInstallationResponse[]>([]);
  const [downloads, setDownloads] = useState<DownloadJobResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [repositoryId, setRepositoryId] = useState("");
  const [revision, setRevision] = useState("");
  const [variant, setVariant] = useState("");
  const [validation, setValidation] = useState<ModelValidationResponse | null>(null);
  const [validating, setValidating] = useState(false);
  const [startingDownload, setStartingDownload] = useState(false);
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [repositoryError, setRepositoryError] = useState<TranslationDescriptor | null>(null);
  const [pageError, setPageError] = useState<PageError | null>(null);
  const [snapshotErrors, setSnapshotErrors] = useState<Record<SnapshotResource, TranslationDescriptor | null>>({
    models: null,
    downloads: null,
  });
  const repositoryRef = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const eventRefreshTimer = useRef<number | null>(null);
  const snapshotGeneration = useRef(0);
  const validationGeneration = useRef(0);

  const refreshSnapshots = useCallback(async () => {
    const generation = snapshotGeneration.current + 1;
    snapshotGeneration.current = generation;
    const [modelsResult, downloadsResult] = await Promise.allSettled([
      fetchModels(),
      fetchDownloads(),
    ]);
    if (generation !== snapshotGeneration.current) return;

    const nextErrors: Record<SnapshotResource, TranslationDescriptor | null> = {
      models: modelsResult.status === "rejected" ? { key: "modelsPage.installedUnavailable" } : null,
      downloads: downloadsResult.status === "rejected" ? { key: "modelsPage.downloadsUnavailable" } : null,
    };
    const failureMessages: TranslationDescriptor[] = [
      modelsResult.status === "rejected" ? { key: "modelsPage.installedDataFailed" } : null,
      downloadsResult.status === "rejected" ? { key: "modelsPage.downloadDataFailed" } : null,
    ].filter((message): message is TranslationDescriptor => message !== null);
    if (modelsResult.status === "fulfilled") setModels(modelsResult.value);
    if (downloadsResult.status === "fulfilled") setDownloads(downloadsResult.value);
    setSnapshotErrors(nextErrors);

    if (failureMessages.length > 0) {
      setPageError({
        title: { key: "modelsPage.modelDataUnavailable" },
        message: failureMessages.length === 1 ? failureMessages[0] : { key: "modelsPage.bothModelDataFailed" },
        actionLabel: { key: "modelsPage.refreshModelData" },
        action: () => void refreshSnapshots(),
      });
    } else {
      setPageError((current) => current?.title.key === "modelsPage.modelDataUnavailable" ? null : current);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void refreshSnapshots();
  }, [refreshSnapshots]);

  useEffect(() => {
    const unsubscribe = subscribeToModelEvents(() => {
      if (eventRefreshTimer.current !== null) return;
      eventRefreshTimer.current = window.setTimeout(() => {
        eventRefreshTimer.current = null;
        void refreshSnapshots();
      }, 25);
    });
    return () => {
      unsubscribe();
      if (eventRefreshTimer.current !== null) window.clearTimeout(eventRefreshTimer.current);
    };
  }, [refreshSnapshots]);

  useEffect(() => {
    if (pageError) errorRef.current?.focus({ preventScroll: true });
  }, [pageError]);

  const compatibleResult = useMemo(() => {
    if (!validation?.compatible) return null;
    return validation.results.find(
      (result) => result.compatible && result.engine_id === validation.selected_engine_id,
    ) ?? validation.results.find((result) => result.compatible) ?? null;
  }, [validation]);

  const availableVariants = compatibleResult?.available_variants ?? [];
  const replacingInstallation = validation
    ? models.some((model) => model.repository_id === validation.repository_id)
    : false;

  const setBusy = (id: string, value: boolean) => {
    setBusyIds((current) => {
      const next = new Set(current);
      if (value) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const showActionError = (title: TranslationDescriptor, error: unknown, actionLabel: TranslationDescriptor, action: () => void) => {
    setPageError({
      title,
      message: { key: errorMessageKey(error instanceof ApiError ? error.code : undefined) },
      actionLabel,
      action,
    });
  };

  const runValidation = async () => {
    const normalizedRepository = repositoryId.trim();
    if (!normalizedRepository) {
      setRepositoryError({ key: "modelsPage.enterRepository" });
      setPageError({
        title: { key: "modelsPage.problem" },
        message: { key: "modelsPage.correctRepository" },
        fieldLink: { href: "#repository-id", label: { key: "modelsPage.enterRepository" } },
      });
      return;
    }

    setRepositoryError(null);
    setPageError(null);
    setValidating(true);
    const requestedRevision = revision.trim() || null;
    const generation = validationGeneration.current + 1;
    validationGeneration.current = generation;
    try {
      const result = await validateModel({
        repository_id: normalizedRepository,
        requested_revision: requestedRevision,
      });
      if (
        generation !== validationGeneration.current
        || normalizedRepository !== repositoryId.trim()
        || requestedRevision !== (revision.trim() || null)
      ) return;
      setValidation(result);
      const selected = result.results.find(
        (item) => item.compatible && item.engine_id === result.selected_engine_id,
      ) ?? result.results.find((item) => item.compatible);
      const variants = selected?.available_variants ?? [];
      setVariant((current) =>
        variants.some((item) => item.id === current) ? current : variants[0]?.id ?? "",
      );
    } catch (error) {
      if (generation !== validationGeneration.current) return;
      showActionError({ key: "modelsPage.validationFailed" }, error, { key: "modelsPage.tryValidation" }, () => void runValidation());
    } finally {
      if (generation === validationGeneration.current) setValidating(false);
    }
  };

  const invalidateValidation = () => {
    validationGeneration.current += 1;
    setValidation(null);
    setVariant("");
    setValidating(false);
  };

  const handleDownload = async () => {
    if (!validation || !compatibleResult) return;
    setStartingDownload(true);
    setPageError(null);
    try {
      const job = await startDownload({
        repository_id: validation.repository_id,
        requested_revision: validation.requested_revision,
        variant: variant || null,
      });
      setDownloads((current) => [job, ...current.filter((item) => item.id !== job.id)]);
    } catch (error) {
      showActionError({ key: "modelsPage.downloadStartFailed" }, error, { key: "modelsPage.tryDownload" }, () => void handleDownload());
    } finally {
      setStartingDownload(false);
    }
  };

  const handleCancel = async (job: DownloadJobResponse) => {
    setBusy(job.id, true);
    try {
      const cancelled = await cancelDownload(job.id);
      setDownloads((current) => current.map((item) => item.id === cancelled.id ? cancelled : item));
    } catch (error) {
      showActionError({ key: "modelsPage.cancellationFailed" }, error, { key: "modelsPage.tryCancellation" }, () => void handleCancel(job));
    } finally {
      setBusy(job.id, false);
    }
  };

  const prepareRepository = (repository: string, requestedRevision: string | null) => {
    setRepositoryId(repository);
    setRevision(requestedRevision ?? "");
    invalidateValidation();
    setRepositoryError(null);
    setPageError(null);
    repositoryRef.current?.focus({ preventScroll: true });
  };

  const handleRemove = async (model: ModelInstallationResponse) => {
    if (!window.confirm(
      t("modelsPage.removeConfirm", { repository: model.repository_id }),
    )) return;
    setBusy(model.id, true);
    try {
      await removeModel(model.id);
      setModels((current) => current.filter((item) => item.id !== model.id));
    } catch (error) {
      showActionError({ key: "modelsPage.removalFailed" }, error, { key: "modelsPage.tryRemoval" }, () => void handleRemove(model));
    } finally {
      setBusy(model.id, false);
    }
  };

  const handleReplicaUpdate = async (model: ModelInstallationResponse, count: number) => {
    if (!Number.isInteger(count) || count < 1 || count > 8) {
      setPageError({ title: { key: "modelsPage.replicaInvalid" }, message: { key: "modelsPage.replicaRange" } });
      return;
    }
    setBusy(model.id, true);
    try {
      const updated = await updateModelReplicas(model.id, count);
      setModels((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (error) {
      showActionError({ key: "modelsPage.replicaFailed" }, error, { key: "modelsPage.tryReplica" }, () => void handleReplicaUpdate(model, count));
    } finally {
      setBusy(model.id, false);
    }
  };

  return (
    <div className="page-stack models-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t("modelsPage.library")}</p>
          <h1>{t("modelsPage.title")}</h1>
          <p>{t("modelsPage.description")}</p>
        </div>
      </header>

      {pageError ? (
        <Alert
          variant="error"
          role="alert"
          aria-label={t(pageError.title.key, pageError.title.values)}
          tabIndex={-1}
          ref={errorRef}
          title={t(pageError.title.key, pageError.title.values)}
          action={pageError.action && pageError.actionLabel ? <Button type="button" variant="outline" onClick={pageError.action}><RefreshCw aria-hidden="true" size={17} />{t(pageError.actionLabel.key, pageError.actionLabel.values)}</Button> : undefined}
        >
          <p>{t(pageError.message.key, pageError.message.values)}</p>
          {pageError.fieldLink ? <a href={pageError.fieldLink.href}>{t(pageError.fieldLink.label.key, pageError.fieldLink.label.values)}</a> : null}
        </Alert>
      ) : null}

      <section aria-labelledby="repository-heading">
        <Card className="model-panel">
          <div className="section-heading">
          <div>
            <h2 id="repository-heading">{t("modelsPage.findCompatible")}</h2>
            <p>{t("modelsPage.validationDescription")}</p>
          </div>
        </div>

        <form
          aria-label={t("modelsPage.validateForm")}
          onSubmit={(event) => {
            event.preventDefault();
            void runValidation();
          }}
        >
          <div className="model-form-grid">
            <Input
              label={t("modelsPage.repositoryId")}
              id="repository-id"
              ref={repositoryRef}
              name="repository_id"
              value={repositoryId}
              onChange={(event) => {
                setRepositoryId(event.target.value);
                invalidateValidation();
                if (repositoryError) setRepositoryError(null);
              }}
              error={repositoryError ? t(repositoryError.key, repositoryError.values) : undefined}
              hint={t("modelsPage.repositoryHint")}
              autoComplete="off"
              placeholder={t("modelsPage.repositoryPlaceholder")}
            />
            <Input
              label={t("modelsPage.revision")}
              id="model-revision"
              name="requested_revision"
              value={revision}
              onChange={(event) => {
                setRevision(event.target.value);
                invalidateValidation();
              }}
              hint={t("modelsPage.revisionHint")}
              placeholder={t("modelsPage.defaultBranch")}
              autoComplete="off"
            />
            <Combobox
              label={t("modelsPage.variant")}
              id="model-variant"
              name="variant"
              value={variant}
              onValueChange={setVariant}
              disabled={availableVariants.length === 0}
              hint={t("modelsPage.variantHint")}
              placeholder={t("modelsPage.validateFirst")}
              options={availableVariants.map((item) => ({ value: item.id, label: item.label }))}
              emptyText={t("modelsPage.noVariants")}
            />
            <div className="form-action">
              <span className="form-action-label" aria-hidden="true" />
              <Button type="submit" disabled={validating}>
                {validating ? <LoaderCircle className="status-spinner" aria-hidden="true" size={17} /> : <CheckCircle2 aria-hidden="true" size={17} />}
                {validating ? t("modelsPage.validating") : t("modelsPage.validate")}
              </Button>
            </div>
          </div>
        </form>

        {validation ? (
          <div className="validation-results" aria-live="polite">
            <div className="result-summary">
              <strong>{validation.compatible ? t("modelsPage.compatibleFound") : t("modelsPage.noCompatible")}</strong>
              <span>{validation.repository_id}</span>
            </div>
            <div className="model-card-grid">
              {validation.results.map((result) => (
                <CompatibilityCard key={`${result.engine_id}-${result.engine_version}`} result={result} />
              ))}
            </div>
            {compatibleResult ? (
              <div className="download-action">
                <Button type="button" disabled={startingDownload} onClick={() => void handleDownload()}>
                  <Download aria-hidden="true" size={18} />
                  {startingDownload
                    ? t("modelsPage.startingDownload")
                    : replacingInstallation
                      ? t("modelsPage.replaceRevision")
                      : t("modelsPage.downloadModel")}
                </Button>
                <p>
                  {replacingInstallation
                    ? t("modelsPage.replacementNote")
                    : t("modelsPage.activationNote")}
                </p>
              </div>
            ) : null}
          </div>
        ) : null}
        </Card>
      </section>

      <div className="models-layout">
        <section aria-labelledby="downloads-heading">
          <Card className="model-panel">
            <div className="section-heading">
            <div>
              <h2 id="downloads-heading">{t("modelsPage.downloadQueue")}</h2>
              <p>{t("modelsPage.downloadDescription")}</p>
            </div>
            <span className="phase-badge">{t("modelsPage.total", { count: formatNumber(downloads.length, i18n.language) })}</span>
          </div>
          {loading ? (
            <div className="models-empty" role="status" aria-live="polite">
              <LoaderCircle className="status-spinner" aria-hidden="true" size={20} />
              <p>{t("modelsPage.loadingDownloads")}</p>
            </div>
          ) : snapshotErrors.downloads ? (
            <Alert
              variant="error"
              role="alert"
              title={t(snapshotErrors.downloads.key, snapshotErrors.downloads.values)}
              action={<Button type="button" variant="outline" onClick={() => void refreshSnapshots()}><RefreshCw aria-hidden="true" size={17} />{t("modelsPage.retryDownloads")}</Button>}
            >
              <p>{t("modelsPage.retryDownloadDescription")}</p>
            </Alert>
          ) : downloads.length === 0 ? (
            <div className="models-empty">
              <Download aria-hidden="true" size={20} />
              <div>
                <strong>{t("modelsPage.noDownloads")}</strong>
                <p>{t("modelsPage.noDownloadsDescription")}</p>
              </div>
            </div>
          ) : (
            <ul className="download-list" aria-label={t("modelsPage.downloadJobs")}>
              {downloads.map((job) => (
                <DownloadCard
                  key={job.id}
                  job={job}
                  busy={busyIds.has(job.id)}
                  onCancel={(item) => void handleCancel(item)}
                  onRetry={(item) => prepareRepository(item.repository_id, item.requested_revision)}
                />
              ))}
            </ul>
          )}
          </Card>
        </section>

        <section aria-labelledby="installations-heading">
          <Card className="model-panel">
            <div className="section-heading">
            <div>
              <h2 id="installations-heading">{t("modelsPage.installedModels")}</h2>
              <p>{t("modelsPage.installedDescription")}</p>
            </div>
            <span className="phase-badge">{t("modelsPage.installedCount", { count: formatNumber(models.length, i18n.language) })}</span>
          </div>
          {loading ? (
            <div className="models-empty" role="status" aria-live="polite">
              <LoaderCircle className="status-spinner" aria-hidden="true" size={20} />
              <p>{t("modelsPage.loadingInstallations")}</p>
            </div>
          ) : snapshotErrors.models ? (
            <Alert
              variant="error"
              role="alert"
              title={t(snapshotErrors.models.key, snapshotErrors.models.values)}
              action={<Button type="button" variant="outline" onClick={() => void refreshSnapshots()}><RefreshCw aria-hidden="true" size={17} />{t("modelsPage.retryInstalled")}</Button>}
            >
              <p>{t("modelsPage.retryInstalledDescription")}</p>
            </Alert>
          ) : models.length === 0 ? (
            <div className="models-empty">
              <HardDrive aria-hidden="true" size={20} />
              <div>
                <strong>{t("modelsPage.noInstalled")}</strong>
                <p>{t("modelsPage.noInstalledDescription")}</p>
              </div>
            </div>
          ) : (
            <div className="model-card-grid installation-grid">
              {models.map((model) => (
                <InstallationCard
                  key={model.id}
                  model={model}
                  busy={busyIds.has(model.id)}
                  onUpdate={(item) => prepareRepository(item.repository_id, item.requested_revision)}
                  onRemove={(item) => void handleRemove(item)}
                  onReplicaUpdate={(item, count) => void handleReplicaUpdate(item, count)}
                />
              ))}
            </div>
          )}
          </Card>
        </section>
      </div>
    </div>
  );
}
