import { CircleOff, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { AudioArtifactResponse } from "../../generated/api";
import { createAuthenticatedMediaUrl, deleteHistory, fetchHistory, hasBrowserApiToken } from "../../lib/api";
import { errorMessageKey, formatBytes, formatDuration, formatNumber, localizedMessage, type LocalizedMessage } from "../../lib/i18n";
type BulkFailure = { id: string; message: LocalizedMessage };

function ArtifactMedia({ item }: { item: AudioArtifactResponse }) {
  const { t } = useTranslation();
  const authenticated = hasBrowserApiToken();
  const [objectUrl, setObjectUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!authenticated) return;
    const controller = new AbortController();
    let createdUrl: string | null = null;
    createAuthenticatedMediaUrl(item.audio_url, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) {
          createdUrl = value;
          setObjectUrl(value);
        } else {
          URL.revokeObjectURL(value);
        }
      },
      () => undefined,
    );
    return () => {
      controller.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [authenticated, item.audio_url]);

  const mediaUrl = authenticated ? objectUrl : item.audio_url;
  if (!mediaUrl) return null;
  return (
    <>
      <audio controls preload="metadata" src={mediaUrl} aria-label={t("historyPage.audioArtifact", { id: item.id })}>{t("historyPage.browserAudio")}</audio>
      <a className="button-link" href={mediaUrl} download={`tts-studio-${item.id}.wav`}>{t("historyPage.downloadWav")}</a>
    </>
  );
}

export function HistoryPage() {
  const { t, i18n } = useTranslation();
  const [items, setItems] = useState<AudioArtifactResponse[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<LocalizedMessage | null>(null);
  const [deleteError, setDeleteError] = useState<LocalizedMessage | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [bulkFailures, setBulkFailures] = useState<BulkFailure[]>([]);

  useEffect(() => { fetchHistory().then((value) => { setItems(value); setState("ready"); }).catch((reason: unknown) => { setError(localizedMessage(reason, "errors.unknown")); setState("error"); }); }, []);

  async function remove(item: AudioArtifactResponse) {
    if (!window.confirm(t("historyPage.confirmOne", { id: item.id }))) return;
    setDeleting(item.id);
    setDeleteError(null);
    setBulkFailures([]);
    try {
      await deleteHistory(item.id);
      setItems((current) => current.filter((entry) => entry.id !== item.id));
      setSelectedIds((current) => { const next = new Set(current); next.delete(item.id); return next; });
    }
    catch (reason: unknown) { setDeleteError(localizedMessage(reason, "historyPage.deleteFailed")); }
    finally { setDeleting(null); }
  }

  function toggleSelected(id: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelectedIds((current) => current.size === items.length ? new Set() : new Set(items.map((item) => item.id)));
  }

  async function removeSelected() {
    const selectedItems = items.filter((item) => selectedIds.has(item.id));
    if (selectedItems.length === 0 || !window.confirm(t("historyPage.confirmMany", { count: selectedItems.length }))) return;

    setBulkDeleting(true);
    setDeleteError(null);
    setBulkFailures([]);
    const results = await Promise.allSettled(selectedItems.map((item) => deleteHistory(item.id)));
    const deletedIds = selectedItems.filter((_, index) => results[index].status === "fulfilled").map((item) => item.id);
    const failures = selectedItems.flatMap((item, index) => {
      const result = results[index];
      if (result.status === "fulfilled") return [];
      return [{ id: item.id, message: localizedMessage(result.reason, "errors.unknown") }];
    });
    setItems((current) => current.filter((item) => !deletedIds.includes(item.id)));
    setSelectedIds((current) => { const next = new Set(current); deletedIds.forEach((id) => next.delete(id)); return next; });
    if (failures.length > 0) {
      setBulkFailures(failures);
      setDeleteError({ key: "historyPage.partialFailure" });
    }
    setBulkDeleting(false);
  }

  return (
    <div className="page-stack">
      <header className="page-header"><div><p className="eyebrow">{t("historyPage.eyebrow")}</p><h1>{t("historyPage.title")}</h1><p>{t("historyPage.description")}</p></div></header>
      {state === "loading" ? <Alert role="status" aria-live="polite">{t("historyPage.loading")}</Alert> : null}
      {state === "error" && error ? <Alert variant="error" role="alert" title={t("historyPage.unavailable")}>{t(error.key, error.values)}</Alert> : null}
      {state === "ready" && items.length === 0 ? (
        <Card className="empty-state panel-state">
          <span className="empty-state-icon"><CircleOff aria-hidden="true" size={22} /></span>
          <div className="empty-state-content">
            <h2>{t("historyPage.emptyTitle")}</h2>
            <p>{t("historyPage.emptyDescription")}</p>
            <a className="button-link" href="/">{t("historyPage.openStudio")}</a>
          </div>
        </Card>
      ) : null}
      {deleteError ? <Alert variant="error" role="alert"><div>{t(deleteError.key, deleteError.values)}</div>{bulkFailures.length > 0 ? <ul>{bulkFailures.map((failure) => <li key={failure.id}>{t("historyPage.bulkFailureItem", { id: failure.id, message: t(failure.message.key, failure.message.values) })}</li>)}</ul> : null}</Alert> : null}
      {state === "ready" && items.length > 0 ? (
        <>
          <section className="history-toolbar" aria-label={t("historyPage.actions")}>
            <label className="history-select history-select-all">
              <input type="checkbox" aria-label={t("historyPage.selectAll")} checked={selectedIds.size === items.length} onChange={toggleAll} disabled={bulkDeleting} />
              <span>{t("historyPage.selectAllText")}</span>
            </label>
            <span className="history-selected-count" aria-live="polite">{t("historyPage.selected", { count: selectedIds.size })}</span>
            <Button type="button" variant="outline" disabled={selectedIds.size === 0 || bulkDeleting} onClick={() => void removeSelected()}>
              <Trash2 aria-hidden="true" size={17} />{bulkDeleting ? t("historyPage.deleting") : t("historyPage.deleteSelected")}
            </Button>
          </section>
          <ul className="history-list" aria-label={t("historyPage.list")}>
          {items.map((item) => (
            <li key={item.id}>
              <Card className="history-card">
                <div className="model-card-header"><div><h2>{t("historyPage.generation", { id: item.job_id })}</h2><p className="mono-value">{item.id}</p></div><span className="model-status model-status-completed">{t("historyPage.finalized")}</span></div>
                <label className="history-select"><input type="checkbox" aria-label={t("historyPage.selectArtifact", { id: item.id })} checked={selectedIds.has(item.id)} onChange={() => toggleSelected(item.id)} disabled={bulkDeleting} /><span>{t("historyPage.selectThis")}</span></label>
              <ArtifactMedia item={item} />
              <dl className="model-facts compact-facts"><div><dt>{t("historyPage.duration")}</dt><dd>{formatDuration(item.duration_ms, i18n.language)}</dd></div><div><dt>{t("historyPage.format")}</dt><dd>{t("historyPage.hzChannel", { rate: formatNumber(item.sample_rate, i18n.language), count: item.channel_count })}</dd></div><div><dt>{t("historyPage.size")}</dt><dd>{formatBytes(item.byte_size, i18n.language)}</dd></div></dl>
              <div className="card-actions"><Button type="button" variant="outline" disabled={deleting === item.id || bulkDeleting} onClick={() => void remove(item)}><Trash2 aria-hidden="true" size={17} />{deleting === item.id ? t("historyPage.deleting") : t("historyPage.delete")}</Button></div>
              </Card>
            </li>
          ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
