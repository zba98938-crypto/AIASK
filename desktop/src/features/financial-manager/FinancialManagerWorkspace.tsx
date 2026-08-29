import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  ClipboardCheck,
  DatabaseZap,
  Loader2,
  LockKeyhole,
  Play,
  RefreshCw,
  Search,
  ShieldCheck
} from "lucide-react";
import { useMemo, useState } from "react";

import { AiaskApi, asList, asRecord, getPathValue, type ApiProblem, type UnknownRecord } from "../../services/aiaskApi";

export interface FinancialManagerWorkspaceProps {
  api: AiaskApi;
  controlAvailable: boolean;
  catalog: ResourceState<UnknownRecord>;
  status: ResourceState<UnknownRecord>;
  broker: ResourceState<UnknownRecord>;
  onRefresh: () => void;
}

export interface ResourceState<T> {
  data: T | null;
  loading: boolean;
  error: ApiProblem | null;
  reload: () => Promise<void>;
}

type ActionMode = "read_only" | "stateful_intent" | "blocked" | string;

interface FinancialAction extends UnknownRecord {
  capability_id?: string;
  action_id?: string;
  label?: string;
  name?: string;
  mode?: ActionMode;
  status?: string;
  available?: boolean;
  blocked_reason?: string;
}

function actionLabel(action: FinancialAction): string {
  return String(action.label || action.name || `${action.capability_id || "capability"}.${action.action_id || "action"}`);
}

function actionKey(action: FinancialAction): string {
  return `${action.capability_id || "unknown"}.${action.action_id || "unknown"}`;
}

function actionTone(action: FinancialAction): "success" | "warning" | "danger" | "neutral" {
  if (action.mode === "blocked" || action.status === "blocked") return "danger";
  if (action.mode === "stateful_intent") return "warning";
  if (action.status === "ready" || action.available) return "success";
  return "neutral";
}

function modeLabel(mode: ActionMode | undefined): string {
  if (mode === "read_only") return "Read-only";
  if (mode === "stateful_intent") return "Intent";
  if (mode === "blocked") return "Blocked";
  return String(mode || "Unknown");
}

function resultTitle(value: unknown): string {
  const record = asRecord(value);
  if (record.error_code) return String(record.error_code);
  if (record.success === true) return "Success";
  if (record.success === false) return "Blocked";
  return "Result";
}

function statusTone(status: unknown): "success" | "warning" | "danger" | "neutral" {
  const text = String(status || "").toLowerCase();
  if (["ready", "ok", "pass", "completed", "success", "true"].includes(text)) return "success";
  if (["blocked", "fail", "failed", "error", "danger"].includes(text)) return "danger";
  if (["degraded", "warn", "warning", "missing_mcp_tool", "intent_ready"].includes(text)) return "warning";
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

function EvidencePanel({ title, data }: { title: string; data: unknown }) {
  return (
    <section className="panel evidence-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Evidence</span>
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

function ActionTable({
  actions,
  selected,
  onSelect
}: {
  actions: FinancialAction[];
  selected: FinancialAction | null;
  onSelect: (action: FinancialAction) => void;
}) {
  return (
    <div className="table-wrap action-table">
      <table>
        <thead>
          <tr>
            <th>Capability</th>
            <th>Action</th>
            <th>Mode</th>
            <th>Status</th>
            <th>Route</th>
          </tr>
        </thead>
        <tbody>
          {actions.map((action) => {
            const selectedRow = selected ? actionKey(selected) === actionKey(action) : false;
            const availability = asRecord(action.availability);
            return (
              <tr key={actionKey(action)} className={selectedRow ? "selected" : ""} onClick={() => onSelect(action)}>
                <td>{String(action.capability_id || "-")}</td>
                <td>
                  <button className="link-button" type="button" onClick={() => onSelect(action)}>
                    {actionLabel(action)}
                  </button>
                </td>
                <td>
                  <StatusPill tone={actionTone(action)}>{modeLabel(action.mode)}</StatusPill>
                </td>
                <td>{String(action.status || availability.reason_code || "-")}</td>
                <td>{action.mode === "stateful_intent" ? "/intent" : action.mode === "read_only" ? "/query" : "blocked"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function FinancialManagerWorkspace({
  api,
  controlAvailable,
  catalog,
  status,
  broker,
  onRefresh
}: FinancialManagerWorkspaceProps) {
  const actions = useMemo(() => asList<FinancialAction>(catalog.data), [catalog.data]);
  const firstReadOnly = actions.find((action) => action.mode === "read_only") ?? actions[0] ?? null;
  const firstIntent = actions.find((action) => action.mode === "stateful_intent") ?? null;
  const [selectedAction, setSelectedAction] = useState<FinancialAction | null>(firstReadOnly);
  const [queryText, setQueryText] = useState("Review current portfolio risk using read-only evidence only.");
  const [intentName, setIntentName] = useState("Desktop guarded portfolio book");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const effectiveAction = selectedAction ?? firstReadOnly;
  const statusRecord = asRecord(status.data);
  const safetyMatrix = asRecord(getPathValue(catalog.data, "safety_matrix") ?? getPathValue(status.data, "safety_matrix"));
  const safetySummary = asRecord(safetyMatrix.summary);
  const brokerRecord = asRecord(broker.data);
  const blockedActions = asList<string>(brokerRecord.blocked_actions);

  async function runReadOnly(action: FinancialAction | null = effectiveAction): Promise<void> {
    if (!action) return;
    setRunning(true);
    try {
      const response = await api.financialManagerQuery({
        capability_id: action.capability_id,
        action_id: action.action_id,
        params: {
          query: queryText,
          codes: ["600519", "000001"],
          weights: [0.6, 0.4]
        }
      });
      setResult(response);
    } catch (error) {
      setResult({ success: false, error: error instanceof Error ? error.message : String(error) });
    } finally {
      setRunning(false);
    }
  }

  async function createIntent(action: FinancialAction | null = firstIntent): Promise<void> {
    if (!action || !controlAvailable) return;
    setRunning(true);
    try {
      const response = await api.financialManagerIntent({
        capability_id: action.capability_id,
        action_id: action.action_id,
        params: {
          name: intentName,
          code: "600519",
          quantity: 100
        },
        rationale: queryText
      });
      setResult(response);
    } catch (error) {
      setResult({ success: false, error: error instanceof Error ? error.message : String(error) });
    } finally {
      setRunning(false);
    }
  }

  const selectedMode = String(effectiveAction?.mode || "");
  const selectedBlocked = selectedMode === "blocked";
  const selectedIntent = selectedMode === "stateful_intent";
  const selectedReadOnly = selectedMode === "read_only";

  return (
    <div className="workspace-stack">
      <section className="page-band finance-hero">
        <div>
          <span className="eyebrow">Financial Manager</span>
          <h2>金融级 Agent 控制台</h2>
          <p>
            Direct execution is limited to read-only analysis. Stateful actions create durable ActionIntents, while live broker order and
            cancel paths stay blocked in this surface.
          </p>
        </div>
        <div className="hero-actions">
          <button className="button secondary" type="button" onClick={onRefresh}>
            <RefreshCw size={16} />
            Refresh
          </button>
          <button className="button primary" type="button" disabled={running || !firstReadOnly} onClick={() => void runReadOnly(firstReadOnly)}>
            {running ? <Loader2 className="spin" size={16} /> : <Search size={16} />}
            Read-only Query
          </button>
        </div>
      </section>

      <ErrorBanner error={catalog.error || status.error || broker.error} />

      <section className="metric-grid">
        <MetricCell label="Manager status" value={String(statusRecord.status || "unknown")} tone={statusTone(statusRecord.status)} />
        <MetricCell
          label="Live trading"
          value={String(safetyMatrix.live_trading_enabled) === "true" ? "Enabled" : "Blocked"}
          tone={String(safetyMatrix.live_trading_enabled) === "true" ? "danger" : "success"}
        />
        <MetricCell label="Read-only actions" value={Number(safetySummary.read_only || 0)} tone="success" />
        <MetricCell label="Intent actions" value={Number(safetySummary.stateful_intent || 0)} tone="warning" />
        <MetricCell label="Trade-risk blocked" value={Number(safetySummary.trade_risk_blocked || 0)} tone="danger" />
      </section>

      <div className="split-layout">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Catalog</span>
              <h3>Action surface</h3>
            </div>
            {catalog.loading ? <Loader2 className="spin muted-icon" size={18} /> : <StatusPill tone="neutral">{actions.length} actions</StatusPill>}
          </div>
          <ActionTable actions={actions} selected={effectiveAction} onSelect={setSelectedAction} />
        </section>

        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Selected action</span>
              <h3>{effectiveAction ? actionLabel(effectiveAction) : "No action selected"}</h3>
            </div>
            <StatusPill tone={effectiveAction ? actionTone(effectiveAction) : "neutral"}>
              {effectiveAction ? modeLabel(effectiveAction.mode) : "Empty"}
            </StatusPill>
          </div>

          <div className="form-grid">
            <label className="field field-wide">
              <span>Query / rationale</span>
              <textarea value={queryText} onChange={(event) => setQueryText(event.target.value)} />
            </label>
            <label className="field">
              <span>Intent name</span>
              <input value={intentName} onChange={(event) => setIntentName(event.target.value)} />
            </label>
          </div>

          <div className="action-row">
            <button className="button primary" type="button" disabled={!selectedReadOnly || running} onClick={() => void runReadOnly()}>
              {running && selectedReadOnly ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
              Run Query
            </button>
            <button
              className="button warning"
              type="button"
              disabled={!selectedIntent || !controlAvailable || running}
              onClick={() => void createIntent(effectiveAction)}
            >
              <ClipboardCheck size={16} />
              Create Intent
            </button>
          </div>

          {!controlAvailable ? (
            <div className="notice warning">
              <LockKeyhole size={16} />
              <span>Control token is required before stateful financial intents can be created.</span>
            </div>
          ) : null}

          {selectedBlocked ? (
            <div className="notice danger">
              <Ban size={16} />
              <span>{String(effectiveAction?.blocked_reason || "This action is blocked in the Desktop Financial Manager.")}</span>
            </div>
          ) : null}

          {result ? (
            <div className={`result-box ${asRecord(result).success === false ? "danger" : "success"}`}>
              <div className="result-heading">
                {asRecord(result).success === false ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}
                <strong>{resultTitle(result)}</strong>
              </div>
              <pre>{JSON.stringify(result, null, 2)}</pre>
            </div>
          ) : (
            <div className="empty-block">
              <Search size={18} />
              <span>Select an action, then run a read-only query or create a guarded intent.</span>
            </div>
          )}
        </section>
      </div>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Broker boundary</span>
            <h3>Read-only broker posture</h3>
          </div>
          <StatusPill tone={String(getPathValue(broker.data, "live_trading_enabled")) === "true" ? "danger" : "success"}>
            {String(getPathValue(broker.data, "live_trading_enabled")) === "true" ? "Live enabled" : "Live blocked"}
          </StatusPill>
        </div>
        <div className="risk-list">
          <div className="risk-item success">
            <ShieldCheck size={18} />
            <div>
              <strong>Broker data is displayed as read-only evidence.</strong>
              <span>Desktop does not show place-order or cancel-order controls.</span>
            </div>
          </div>
          <div className="risk-item danger">
            <Ban size={18} />
            <div>
              <strong>Guarded live MCP tools stay blocked here.</strong>
              <span>{blockedActions.length ? blockedActions.join(", ") : "ths_place_order, ths_cancel_order, qmt_place_order, qmt_cancel_order"}</span>
            </div>
          </div>
          <div className="risk-item neutral">
            <DatabaseZap size={18} />
            <div>
              <strong>Secret presence only.</strong>
              <span>Broker token values are never rendered by this workbench.</span>
            </div>
          </div>
        </div>
      </section>

      <div className="split-layout">
        <EvidencePanel title="Financial status" data={status.data} />
        <EvidencePanel title="Safety matrix" data={safetyMatrix} />
      </div>
    </div>
  );
}
