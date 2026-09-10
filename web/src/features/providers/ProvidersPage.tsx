import { CheckCircle2, CircleOff, Plus, Trash2, X } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { ProviderRequest, ProviderResponse } from "../../generated/api";
import { createProvider, deleteProvider, fetchProviders, validateProvider } from "../../lib/api";
import { localizedMessage, type LocalizedMessage } from "../../lib/i18n";

const emptyForm: ProviderRequest = { kind: "openai_compatible", label: "", base_url: "", model: "", api_key_env: "" };

export function ProvidersPage() {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderResponse[]>([]);
  const [form, setForm] = useState<ProviderRequest>(emptyForm);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<LocalizedMessage | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  useEffect(() => {
    fetchProviders().then((value) => { setProviders(value); setState("ready"); }).catch((reason: unknown) => { setError(localizedMessage(reason, "providersPage.unavailable")); setState("error"); });
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy("create"); setError(null);
    try { const created = await createProvider(form); setProviders((current) => [...current, created]); setForm(emptyForm); }
    catch (reason: unknown) { setError(localizedMessage(reason, "providersPage.createFailed")); }
    finally { setBusy(null); }
  }

  async function check(provider: ProviderResponse) {
    setBusy(provider.id); setError(null);
    try { await validateProvider(provider.id); }
    catch (reason: unknown) { setError(localizedMessage(reason, "providersPage.validationFailed")); }
    finally { setBusy(null); }
  }

  async function remove(provider: ProviderResponse) {
    setBusy(provider.id); setError(null);
    try { await deleteProvider(provider.id); setProviders((current) => current.filter((item) => item.id !== provider.id)); }
    catch (reason: unknown) { setError(localizedMessage(reason, "providersPage.deleteFailed")); }
    finally { setBusy(null); }
  }

  const update = (field: keyof ProviderRequest, value: string) => setForm((current) => ({ ...current, [field]: value }));

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t("providersPage.eyebrow")}</p>
          <h1>{t("providersPage.title")}</h1>
          <p>{t("providersPage.description")}</p>
        </div>
      </header>
      {state === "loading" ? <Alert role="status" aria-live="polite">{t("providersPage.loading")}</Alert> : null}
      {state === "error" && error ? <Alert variant="error" role="alert">{t(error.key, error.values)}</Alert> : null}
      {error && state !== "error" ? <Alert variant="error" role="alert">{t(error.key, error.values)}</Alert> : null}
      <Card
        className={`provider-create-card${showForm ? " provider-create-card-open" : ""}`}
        header={
          <div className="provider-create-header">
            <div>
              <h2>{providers.length === 0 && !showForm ? t("providersPage.noConfigured") : t("providersPage.add")}</h2>
              <p>{providers.length === 0 && !showForm ? t("providersPage.noConfiguredDescription") : t("providersPage.keyEnvironment")}</p>
            </div>
            <Button
              type="button"
              variant="outline"
              size="small"
              aria-expanded={showForm}
              aria-controls="provider-form"
              onClick={() => setShowForm((current) => !current)}
            >
              {showForm ? <X aria-hidden="true" size={16} /> : <Plus aria-hidden="true" size={16} />}
              {showForm ? t("providersPage.close") : t("providersPage.add")}
            </Button>
          </div>
        }
      >
        {showForm ? (
          <form id="provider-form" className="provider-form" onSubmit={(event) => void submit(event)}>
            <div className="provider-form-fields">
              <Input label={t("providersPage.label")} id="provider-label" required value={form.label} onChange={(event) => update("label", event.target.value)} />
              <Input label={t("providersPage.baseUrl")} id="provider-base-url" required type="url" placeholder={t("providersPage.baseUrlPlaceholder")} hint={t("providersPage.baseUrlHint")} value={form.base_url} onChange={(event) => update("base_url", event.target.value)} />
              <Input label={t("providersPage.model")} id="provider-model" required value={form.model} onChange={(event) => update("model", event.target.value)} />
              <Input label={t("providersPage.apiKeyEnv")} id="provider-api-key-env" required hint={t("providersPage.apiKeyHint")} value={form.api_key_env} onChange={(event) => update("api_key_env", event.target.value)} />
            </div>
            <div className="provider-form-actions"><Button type="submit" disabled={busy === "create"}>{busy === "create" ? t("providersPage.adding") : t("providersPage.add")}</Button></div>
          </form>
        ) : null}
      </Card>
      {providers.length > 0 ? (
        <ul className="history-list" aria-label={t("providersPage.configured")}>
          {providers.map((provider) => (
            <li key={provider.id}>
              <Card className="history-card">
                <div className="model-card-header"><div><h2>{provider.label}</h2><p className="mono-value">{provider.base_url}</p></div><span className="model-status model-status-completed">{provider.kind}</span></div>
              <dl className="model-facts compact-facts"><div><dt>{t("providersPage.model")}</dt><dd>{provider.model}</dd></div><div><dt>{t("providersPage.credential")}</dt><dd>{provider.api_key_env}</dd></div></dl>
              <div className="card-actions"><Button type="button" variant="outline" disabled={busy === provider.id} onClick={() => void check(provider)}><CheckCircle2 aria-hidden="true" size={17} />{busy === provider.id ? t("providersPage.checking") : t("providersPage.validate")}</Button><Button type="button" variant="outline" disabled={busy === provider.id} onClick={() => void remove(provider)}><Trash2 aria-hidden="true" size={17} />{t("providersPage.delete")}</Button></div>
              </Card>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
