import { mockRequest, type UnknownRecord } from "../mockApi";

export type ApiMode = "mock" | "live";

export interface ConnectionSettings {
  baseUrl: string;
  apiToken: string;
  controlToken: string;
  userId: string;
  mode: ApiMode;
}

export interface ApiProblem {
  status: number;
  title: string;
  detail: string;
  code?: string;
  raw?: unknown;
}

export class ApiError extends Error {
  readonly problem: ApiProblem;

  constructor(problem: ApiProblem) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
    this.problem = problem;
  }
}

export type { UnknownRecord };

function normalizeBaseUrl(baseUrl: string): string {
  const trimmed = String(baseUrl || "").trim();
  return trimmed ? trimmed.replace(/\/+$/, "") : "http://127.0.0.1:8765";
}

function appendQuery(path: string, query?: Record<string, unknown>): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function redactSecrets(value: unknown): unknown {
  if (typeof value === "string") {
    return value
      .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [redacted]")
      .replace(/(token|password|api[_-]?key|secret)=([^&\s]+)/gi, "$1=[redacted]");
  }
  if (Array.isArray(value)) return value.map(redactSecrets);
  if (value && typeof value === "object") {
    const result: UnknownRecord = {};
    for (const [key, item] of Object.entries(value as UnknownRecord)) {
      if (/token|password|api[_-]?key|secret|credential/i.test(key)) {
        result[key] = "[REDACTED]";
      } else {
        result[key] = redactSecrets(item);
      }
    }
    return result;
  }
  return value;
}

function jsonHeaders(settings: ConnectionSettings, control: boolean): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/json"
  };
  const apiToken = settings.apiToken.trim();
  const controlToken = settings.controlToken.trim();
  const userId = settings.userId.trim();
  if (apiToken) {
    headers.Authorization = `Bearer ${apiToken}`;
    headers["X-AIASK-Agent-Token"] = apiToken;
  }
  if (control && controlToken) {
    headers["X-AIASK-Agent-Control-Token"] = controlToken;
    headers["X-AIASK-Local-Control-Token"] = controlToken;
    if (!apiToken) {
      headers.Authorization = `Bearer ${controlToken}`;
    }
  }
  if (userId) {
    headers["X-AIASK-User-Id"] = userId;
  }
  return headers;
}

async function parseError(response: Response): Promise<never> {
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    raw = await response.text().catch(() => "");
  }
  const record = raw && typeof raw === "object" ? (raw as UnknownRecord) : {};
  throw new ApiError({
    status: response.status,
    title: String(record.title || response.statusText || "Request failed"),
    detail: String(record.detail || record.error || raw || response.statusText || "Request failed"),
    code: typeof record.error_code === "string" ? record.error_code : undefined,
    raw: redactSecrets(raw)
  });
}

async function requestJson<T>(
  settings: ConnectionSettings,
  path: string,
  options: {
    method?: "GET" | "POST" | "PATCH" | "DELETE";
    query?: Record<string, unknown>;
    body?: unknown;
    control?: boolean;
    timeoutMs?: number;
  } = {}
): Promise<T> {
  const method = options.method ?? "GET";
  const finalPath = appendQuery(path, options.query);
  if (settings.mode === "mock") {
    return mockRequest<T>({ path: finalPath, method, body: options.body, query: options.query });
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 30_000);
  try {
    const response = await fetch(`${normalizeBaseUrl(settings.baseUrl)}${finalPath}`, {
      method,
      headers: jsonHeaders(settings, Boolean(options.control)),
      body: method === "GET" ? undefined : JSON.stringify(options.body ?? {}),
      signal: controller.signal
    });
    if (!response.ok) {
      await parseError(response);
    }
    const parsed = (await response.json()) as T;
    return redactSecrets(parsed) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

export class AiaskApi {
  private settings: ConnectionSettings;

  constructor(settings: ConnectionSettings) {
    this.settings = settings;
  }

  updateSettings(settings: ConnectionSettings): void {
    this.settings = settings;
  }

  health = () => requestJson<UnknownRecord>(this.settings, "/health");

  workbenchSummary = () =>
    requestJson<UnknownRecord>(this.settings, "/v1/desktop/workbench/summary", {
      query: { user_id: this.settings.userId, session_limit: 8, run_limit: 8 }
    });

  hermesReadiness = () => requestJson<UnknownRecord>(this.settings, "/v1/hermes/readiness");

  financialReadiness = () => requestJson<UnknownRecord>(this.settings, "/v1/financial-system/readiness", { timeoutMs: 60_000 });

  dataStatus = () =>
    requestJson<UnknownRecord>(this.settings, "/v1/desktop/data/status", {
      query: { codes: "600519,000001", max_stale_days: 5 }
    });

  mcpServers = (includeAll = false) =>
    requestJson<UnknownRecord>(this.settings, "/v1/mcp/servers", { query: includeAll ? { all: true } : undefined });

  mcpTools = (includeAll = false) =>
    requestJson<UnknownRecord>(this.settings, "/v1/mcp/tools", {
      query: includeAll ? { all: true } : undefined,
      control: true
    });

  response = (body: unknown) =>
    requestJson<UnknownRecord>(this.settings, "/v1/responses", {
      method: "POST",
      body: { user_id: this.settings.userId, mode: "finance_safe", ...objectOrEmpty(body) },
      timeoutMs: 120_000
    });

  financialManagerCatalog = () => requestJson<UnknownRecord>(this.settings, "/v1/desktop/financial-manager/catalog");

  financialManagerStatus = () => requestJson<UnknownRecord>(this.settings, "/v1/desktop/financial-manager/status", { timeoutMs: 60_000 });

  financialManagerQuery = (body: unknown) =>
    requestJson<UnknownRecord>(this.settings, "/v1/desktop/financial-manager/query", {
      method: "POST",
      body: { user_id: this.settings.userId, ...objectOrEmpty(body) },
      timeoutMs: 90_000
    });

  financialManagerIntent = (body: unknown) =>
    requestJson<UnknownRecord>(this.settings, "/v1/desktop/financial-manager/intent", {
      method: "POST",
      body: { user_id: this.settings.userId, ...objectOrEmpty(body) },
      control: true,
      timeoutMs: 90_000
    });

  brokerReadiness = () => requestJson<UnknownRecord>(this.settings, "/v1/desktop/broker-readiness");
}

function objectOrEmpty(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as UnknownRecord) : {};
}

export function getPathValue(value: unknown, path: string): unknown {
  let current = value;
  for (const key of path.split(".")) {
    if (!current || typeof current !== "object") return undefined;
    current = (current as UnknownRecord)[key];
  }
  return current;
}

export function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as UnknownRecord) : {};
}

export function asList<T = UnknownRecord>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[];
  const record = asRecord(value);
  if (Array.isArray(record.data)) return record.data as T[];
  if (Array.isArray(record.actions)) return record.actions as T[];
  if (Array.isArray(record.recent_sessions)) return record.recent_sessions as T[];
  return [];
}
