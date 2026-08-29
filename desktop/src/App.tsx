import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  Loader2,
  MessageSquareText,
  RefreshCw,
  Save,
  Send,
  ShieldCheck,
  Wifi,
  WifiOff
} from "lucide-react";
import type { DependencyList } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { FinancialManagerWorkspace } from "./features/financial-manager/FinancialManagerWorkspace";
import { AiaskApi, asList, asRecord, getPathValue, type ApiMode, type ApiProblem, type ConnectionSettings, type UnknownRecord } from "./services/aiaskApi";
import { DEFAULT_VIEW, VIEWS, viewById, viewsByGroup, type ViewId } from "./views";

type ResourceFactory<T> = () => Promise<T>;

interface ResourceState<T> {
  data: T | null;
  loading: boolean;
  error: ApiProblem | null;
  reload: () => Promise<void>;
}

const DEFAULT_SETTINGS: ConnectionSettings = {
  baseUrl: "http://127.0.0.1:8765",
  apiToken: "",
  controlToken: "",
  userId: "local",
  mode: "live"
};

function storedSettings(): ConnectionSettings {
  try {
    const raw = window.localStorage.getItem("aiask.desktop.settings");
    if (!raw) return DEFAULT_SETTINGS;
    return { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<ConnectionSettings>) };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function toProblem(error: unknown): ApiProblem {
  if (error && typeof error === "object" && "problem" in error) {
    return (error as { problem: ApiProblem }).problem;
  }
  return {
    status: 0,
    title: error instanceof Error ? error.name : "Request failed",
    detail: error instanceof Error ? error.message : String(error)
  };
}

function useResource<T>(factory: ResourceFactory<T>, deps: DependencyList): ResourceState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiProblem | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await factory());
    } catch (caught) {
      setError(toProblem(caught));
    } finally {
      setLoading(false);
    }
  }, deps);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, loading, error, reload };
}

function statusTone(value: unknown): "success" | "warning" | "danger" | "neutral" {
  const text = String(value || "").toLowerCase();
  if (["ok", "ready", "pass", "true", "online", "completed"].includes(text)) return "success";
  if (["blocked", "fail", "failed", "error", "false"].includes(text)) return "danger";
  if (["degraded", "warn", "warning", "missing"].includes(text)) return "warning";
  return "neutral";
}

function StatusPill({ tone, children }: { tone: "success" | "warning" | "danger" | "neutral"; children: React.ReactNode }) {
  return <span className={`status-pill ${tone}`}>{children}</span>;
}

function ErrorBanner({ error }: { error: ApiProblem | null }) {
  if (!error) return null;
  return (
    <div className="notice danger" role="alert">
      <AlertTriangle size={16} />
      <div>
        <strong>{error.title}</strong>
        <span>{error.detail}</span>
      </div>
    </div>
  );
}

function JsonPanel({ title, data }: { title: string; data: unknown }) {
  return (
    <section className="panel evidence-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">JSON</span>
          <h3>{title}</h3>
        </div>
      </div>
      <pre>{JSON.stringify(data ?? {}, null, 2)}</pre>
    </section>
  );
}

function MetricCell({
  label,
  value,
  tone
}: {
  label: string;
  value: string | number;
  tone?: "success" | "warning" | "danger" | "neutral";
}) {
  return (
    <div className={`metric-cell ${tone || "neutral"}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function SettingsView({
  settings,
  onSave
}: {
  settings: ConnectionSettings;
  onSave: (next: ConnectionSettings) => void;
}) {
  const [draft, setDraft] = useState(settings);

  useEffect(() => {
    setDraft(settings);
  }, [settings]);

  function update<K extends keyof ConnectionSettings>(key: K, value: ConnectionSettings[K]): void {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  return (
    <div className="workspace-stack">
      <section className="page-band">
        <div>
          <span className="eyebrow">Settings</span>
          <h2>Connection and mode</h2>
          <p>Desktop keeps credentials local to the browser session and sends them only as Agent HTTP headers.</p>
        </div>
        <button className="button primary" type="button" onClick={() => onSave(draft)}>
          <Save size={16} />
          Save
        </button>
      </section>

      <section className="panel">
        <div className="form-grid">
          <label className="field field-wide">
            <span>Agent endpoint</span>
            <input value={draft.baseUrl} onChange={(event) => update("baseUrl", event.target.value)} />
          </label>
          <label className="field">
            <span>API token</span>
            <input
              type="password"
              value={draft.apiToken}
              autoComplete="off"
              onChange={(event) => update("apiToken", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Control token</span>
            <input
              type="password"
              value={draft.controlToken}
              autoComplete="off"
              onChange={(event) => update("controlToken", event.target.value)}
            />
          </label>
          <label className="field">
            <span>User id</span>
            <input value={draft.userId} onChange={(event) => update("userId", event.target.value)} />
          </label>
          <div className="field">
            <span>Mode</span>
            <div className="segmented-control">
              {(["mock", "live"] as ApiMode[]).map((mode) => (
                <button key={mode} className={draft.mode === mode ? "active" : ""} type="button" onClick={() => update("mode", mode)}>
                  {mode === "mock" ? "Mock" : "Live"}
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

function WorkbenchView({
  api,
  health,
  summary,
  readiness,
  onRefresh
}: {
  api: AiaskApi;
  health: ResourceState<UnknownRecord>;
  summary: ResourceState<UnknownRecord>;
  readiness: ResourceState<UnknownRecord>;
  onRefresh: () => void;
}) {
  const [prompt, setPrompt] = useState("Summarize AIASK financial readiness and list the next safe read-only validation.");
  const [sending, setSending] = useState(false);
  const [response, setResponse] = useState<unknown>(null);
  const sessions = asList<UnknownRecord>(summary.data?.recent_sessions ?? getPathValue(summary.data, "data.sessions"));
  const runs = asList<UnknownRecord>(summary.data?.recent_runs ?? getPathValue(summary.data, "data.runs"));
  const pendingIntentCount = Number(getPathValue(summary.data, "queues.pending_intents") || 0);
  const unknownExecutionCount = Number(getPathValue(summary.data, "queues.unknown_execution_intents") || 0);

  async function sendPrompt(): Promise<void> {
    setSending(true);
    try {
      setResponse(await api.response({ input: prompt }));
    } catch (caught) {
      setResponse({ error: toProblem(caught) });
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="workspace-stack">
      <section className="page-band">
        <div>
          <span className="eyebrow">Workbench</span>
          <h2>Agent HTTP product surface</h2>
          <p>Use this as the first screen for readiness, financial workflow smoke checks, and response API validation.</p>
        </div>
        <button className="button secondary" type="button" onClick={onRefresh}>
          <RefreshCw size={16} />
          Refresh
        </button>
      </section>

      <ErrorBanner error={health.error || summary.error || readiness.error} />

      <section className="metric-grid">
        <MetricCell label="Health" value={String(health.data?.status || "unknown")} tone={statusTone(health.data?.status)} />
        <MetricCell label="Backend" value={String(readiness.data?.backend_ready ?? "unknown")} tone={statusTone(readiness.data?.backend_ready)} />
        <MetricCell label="Product" value={String(readiness.data?.product_ready ?? "unknown")} tone={statusTone(readiness.data?.product_ready)} />
        <MetricCell label="Pending intents" value={pendingIntentCount} tone={pendingIntentCount > 0 ? "warning" : "neutral"} />
        <MetricCell label="Unknown executions" value={unknownExecutionCount} tone={unknownExecutionCount > 0 ? "danger" : "success"} />
      </section>

      <div className="split-layout">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Response API</span>
              <h3>Finance-safe prompt</h3>
            </div>
            <StatusPill tone="neutral">/v1/responses</StatusPill>
          </div>
          <label className="field field-wide">
            <span>Prompt</span>
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} />
          </label>
          <div className="action-row">
            <button className="button primary" type="button" disabled={sending} onClick={() => void sendPrompt()}>
              {sending ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
              Send
            </button>
          </div>
          {response ? (
            <div className="result-box success">
              <div className="result-heading">
                <MessageSquareText size={17} />
                <strong>Response</strong>
              </div>
              <pre>{JSON.stringify(response, null, 2)}</pre>
            </div>
          ) : null}
        </section>

        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Recent context</span>
              <h3>Sessions and runs</h3>
            </div>
            {summary.loading ? <Loader2 className="spin muted-icon" size={18} /> : null}
          </div>
          <div className="compact-list">
            {sessions.slice(0, 4).map((session, index) => (
              <div className="list-row" key={String(session.session_id || session.id || index)}>
                <div>
                  <strong>{String(session.title || session.session_id || `Session ${index + 1}`)}</strong>
                  <span>{String(session.session_id || session.id || "-")}</span>
                </div>
                <StatusPill tone={session.has_pending_approval ? "warning" : "neutral"}>
                  {session.has_pending_approval ? "approval" : `${Number(session.message_count || 0)} msg`}
                </StatusPill>
              </div>
            ))}
            {runs.slice(0, 3).map((run, index) => (
              <div className="list-row" key={String(run.run_id || run.id || index)}>
                <div>
                  <strong>{String(run.run_id || run.id || `Run ${index + 1}`)}</strong>
                  <span>{String(run.session_id || "-")}</span>
                </div>
                <StatusPill tone={statusTone(run.status)}>{String(run.status || "unknown")}</StatusPill>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

function ReadinessView({ readiness, dataStatus, onRefresh }: { readiness: ResourceState<UnknownRecord>; dataStatus: ResourceState<UnknownRecord>; onRefresh: () => void }) {
  const required = asList<UnknownRecord>(readiness.data?.required_gates);
  const optional = asList<UnknownRecord>(readiness.data?.optional_gates);
  const nextActions = asList<UnknownRecord>(readiness.data?.next_actions);
  const unknownExecutionCount = Number(getPathValue(readiness.data, "summary.unknown_execution_intents") || 0);

  return (
    <div className="workspace-stack">
      <section className="page-band">
        <div>
          <span className="eyebrow">Readiness</span>
          <h2>Financial-grade gate review</h2>
          <p>Readiness separates backend gates from product readiness and keeps live trading outside this acceptance path.</p>
        </div>
        <button className="button secondary" type="button" onClick={onRefresh}>
          <RefreshCw size={16} />
          Refresh
        </button>
      </section>

      <ErrorBanner error={readiness.error || dataStatus.error} />

      <section className="metric-grid">
        <MetricCell label="Status" value={String(readiness.data?.status || "unknown")} tone={statusTone(readiness.data?.status)} />
        <MetricCell label="Backend ready" value={String(readiness.data?.backend_ready ?? "unknown")} tone={statusTone(readiness.data?.backend_ready)} />
        <MetricCell label="Product ready" value={String(readiness.data?.product_ready ?? "unknown")} tone={statusTone(readiness.data?.product_ready)} />
        <MetricCell label="Data status" value={String(dataStatus.data?.status || "unknown")} tone={statusTone(dataStatus.data?.status)} />
        <MetricCell label="Unknown executions" value={unknownExecutionCount} tone={unknownExecutionCount > 0 ? "danger" : "success"} />
      </section>

      <div className="split-layout">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Required gates</span>
              <h3>Production blockers</h3>
            </div>
            {readiness.loading ? <Loader2 className="spin muted-icon" size={18} /> : null}
          </div>
          <div className="gate-list">
            {[...required, ...optional].map((gate) => (
              <div className={`gate-row ${statusTone(gate.status)}`} key={String(gate.name)}>
                {statusTone(gate.status) === "success" ? <CheckCircle2 size={18} /> : <CircleDot size={18} />}
                <div>
                  <strong>{String(gate.name || "gate")}</strong>
                  <span>{String(gate.detail || "")}</span>
                </div>
                <StatusPill tone={statusTone(gate.status)}>{String(gate.status || "unknown")}</StatusPill>
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Next actions</span>
              <h3>Operator queue</h3>
            </div>
          </div>
          <div className="compact-list">
            {nextActions.map((action, index) => (
              <div className="list-row" key={String(action.action_id || index)}>
                <div>
                  <strong>{String(action.title || action.action_id || `Action ${index + 1}`)}</strong>
                  <span>{String(action.detail || "")}</span>
                </div>
                <StatusPill tone={action.priority === "critical" ? "danger" : "warning"}>{String(action.priority || "recommended")}</StatusPill>
              </div>
            ))}
          </div>
        </section>
      </div>

      <JsonPanel title="Readiness evidence" data={readiness.data} />
    </div>
  );
}

function McpView({ servers, tools, onRefresh }: { servers: ResourceState<UnknownRecord>; tools: ResourceState<UnknownRecord>; onRefresh: () => void }) {
  const serverRows = asList<UnknownRecord>(servers.data);
  const toolRows = asList<UnknownRecord>(tools.data);

  return (
    <div className="workspace-stack">
      <section className="page-band">
        <div>
          <span className="eyebrow">MCP / Connectors</span>
          <h2>Safe financial aggregation</h2>
          <p>Default visibility is the safe financial surface. Full raw diagnostics stay behind full-mode/control policy.</p>
        </div>
        <button className="button secondary" type="button" onClick={onRefresh}>
          <RefreshCw size={16} />
          Refresh
        </button>
      </section>

      <ErrorBanner error={servers.error || tools.error} />

      <div className="split-layout">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Servers</span>
              <h3>Registered MCP endpoints</h3>
            </div>
            {servers.loading ? <Loader2 className="spin muted-icon" size={18} /> : <StatusPill tone="neutral">{serverRows.length}</StatusPill>}
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Domain</th>
                  <th>Transport</th>
                  <th>Tools</th>
                  <th>Auth</th>
                </tr>
              </thead>
              <tbody>
                {serverRows.map((server, index) => (
                  <tr key={String(server.name || server.id || index)}>
                    <td>{String(server.name || server.id || "-")}</td>
                    <td>{String(server.domain || "-")}</td>
                    <td>{String(server.transport || "-")}</td>
                    <td>{asList(server.tools).length || String(server.tools || "-")}</td>
                    <td>{String(server.auth_configured ?? "unknown")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Tools</span>
              <h3>Visible safe tools</h3>
            </div>
            {tools.loading ? <Loader2 className="spin muted-icon" size={18} /> : <StatusPill tone="neutral">{toolRows.length}</StatusPill>}
          </div>
          <div className="compact-list">
            {toolRows.map((tool, index) => (
              <div className="list-row" key={String(tool.wrapped_name || tool.name || index)}>
                <div>
                  <strong>{String(tool.name || "-")}</strong>
                  <span>{String(tool.wrapped_name || tool.description || "")}</span>
                </div>
                <StatusPill tone={String(getPathValue(tool, "side_effect.level")) === "read_only" ? "success" : "warning"}>
                  {String(getPathValue(tool, "side_effect.level") || "unknown")}
                </StatusPill>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

function Sidebar({ activeView, onSelect }: { activeView: ViewId; onSelect: (view: ViewId) => void }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">AI</div>
        <div>
          <strong>AIASK</strong>
          <span>Financial Agent</span>
        </div>
      </div>
      <nav className="nav-groups" aria-label="AIASK views">
        {viewsByGroup().map((group) => (
          <section className="nav-group" key={group.group}>
            <span>{group.label}</span>
            {group.views.map((view) => {
              const Icon = view.icon;
              return (
                <button
                  key={view.id}
                  className={view.id === activeView ? "active" : ""}
                  type="button"
                  onClick={() => onSelect(view.id)}
                >
                  <Icon size={18} />
                  <strong>{view.shortLabel}</strong>
                  <ChevronRight size={15} />
                </button>
              );
            })}
          </section>
        ))}
      </nav>
      <div className="sidebar-note">
        <ShieldCheck size={16} />
        <span>Desktop uses Agent HTTP only.</span>
      </div>
    </aside>
  );
}

export function App() {
  const [activeView, setActiveView] = useState<ViewId>(DEFAULT_VIEW);
  const [settings, setSettings] = useState<ConnectionSettings>(() => storedSettings());
  const api = useMemo(() => new AiaskApi(settings), [settings]);
  const active = viewById(activeView);
  const controlAvailable = Boolean(settings.controlToken.trim()) || settings.mode === "mock";

  const health = useResource(() => api.health(), [api]);
  const summary = useResource(() => api.workbenchSummary(), [api]);
  const readiness = useResource(() => api.financialReadiness(), [api]);
  const dataStatus = useResource(() => api.dataStatus(), [api]);
  const catalog = useResource(() => api.financialManagerCatalog(), [api]);
  const financeStatus = useResource(() => api.financialManagerStatus(), [api]);
  const broker = useResource(() => api.brokerReadiness(), [api]);
  const mcpServers = useResource(() => api.mcpServers(false), [api]);
  const mcpTools = useResource(() => (controlAvailable ? api.mcpTools(false) : Promise.resolve({ object: "list", data: [] })), [api, controlAvailable]);

  function saveSettings(next: ConnectionSettings): void {
    setSettings(next);
    window.localStorage.setItem("aiask.desktop.settings", JSON.stringify(next));
  }

  async function refreshAll(): Promise<void> {
    await Promise.all([
      health.reload(),
      summary.reload(),
      readiness.reload(),
      dataStatus.reload(),
      catalog.reload(),
      financeStatus.reload(),
      broker.reload(),
      mcpServers.reload(),
      mcpTools.reload()
    ]);
  }

  return (
    <div className="app-shell">
      <Sidebar activeView={activeView} onSelect={setActiveView} />

      <div className="main-frame">
        <header className="topbar">
          <div>
            <span className="eyebrow">{active.group}</span>
            <h1>{active.label}</h1>
            <p>{active.description}</p>
          </div>
          <div className="topbar-controls">
            <StatusPill tone={settings.mode === "mock" ? "warning" : "success"}>{settings.mode === "mock" ? "Mock" : "Live"}</StatusPill>
            <StatusPill tone={health.error ? "danger" : statusTone(health.data?.status)}>
              {health.error ? <WifiOff size={14} /> : <Wifi size={14} />}
              {health.error ? "Offline" : String(health.data?.status || "Unknown")}
            </StatusPill>
            <button className="icon-button" type="button" aria-label="Refresh all" onClick={() => void refreshAll()}>
              <RefreshCw size={18} />
            </button>
          </div>
        </header>

        <main className="page-host">
          {activeView === "workbench" ? (
            <WorkbenchView api={api} health={health} summary={summary} readiness={readiness} onRefresh={() => void refreshAll()} />
          ) : null}

          {activeView === "financial-manager" ? (
            <FinancialManagerWorkspace
              api={api}
              controlAvailable={controlAvailable}
              catalog={catalog}
              status={financeStatus}
              broker={broker}
              onRefresh={() => void Promise.all([catalog.reload(), financeStatus.reload(), broker.reload()])}
            />
          ) : null}

          {activeView === "readiness-health" ? (
            <ReadinessView readiness={readiness} dataStatus={dataStatus} onRefresh={() => void Promise.all([readiness.reload(), dataStatus.reload()])} />
          ) : null}

          {activeView === "mcp-connectors" ? (
            <McpView servers={mcpServers} tools={mcpTools} onRefresh={() => void Promise.all([mcpServers.reload(), mcpTools.reload()])} />
          ) : null}

          {activeView === "settings" ? <SettingsView settings={settings} onSave={saveSettings} /> : null}
        </main>
      </div>
    </div>
  );
}
