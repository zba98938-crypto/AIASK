export interface MockRequest {
  path: string;
  method: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, unknown>;
}

export type UnknownRecord = Record<string, unknown>;

function nowIso(): string {
  return new Date().toISOString();
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function objectData(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as UnknownRecord) : {};
}

const safetyMatrix = {
  object: "aiask.desktop.financial_manager.safety_matrix",
  live_trading_enabled: false,
  live_trading_policy: "blocked_in_financial_manager_v1",
  stateful_execution: "allowlisted_confirmed_actions",
  control_token_configured: true,
  action_intent_required: true,
  dry_run_only_actions: ["paper.submit_order", "paper.cancel_order"],
  broker_token_policy: {
    explicit_token_required_for_live_mcp: true,
    guarded_tools: ["ths_place_order", "ths_cancel_order", "qmt_place_order", "qmt_cancel_order"],
    env_presence: {
      tonghuashun: { env_var: "AIASK_FINANCE_THS_BROKER_TOKEN", configured: false },
      qmt: { env_var: "AIASK_FINANCE_QMT_BROKER_TOKEN", configured: false }
    }
  },
  summary: {
    total_actions: 6,
    read_only: 2,
    stateful_intent: 2,
    trade_risk_blocked: 2,
    dry_run_only: 1,
    broker_token_env_configured: 0
  },
  actions: [
    {
      operation_id: "portfolio.risk",
      mode: "read_only",
      read_only: true,
      direct_execution_allowed: true,
      action_intent_required: false,
      risk_level: "read_only"
    },
    {
      operation_id: "portfolio.create",
      mode: "stateful_intent",
      direct_execution_allowed: false,
      action_intent_required: true,
      control_token_required: true,
      endpoint: "/v1/desktop/financial-manager/intent",
      risk_level: "stateful"
    },
    {
      operation_id: "paper.submit_order",
      mode: "stateful_intent",
      direct_execution_allowed: false,
      action_intent_required: true,
      control_token_required: true,
      dry_run_required: true,
      execution_mode: "confirmed_execute_dry_run_only",
      endpoint: "/v1/desktop/financial-manager/intent",
      risk_level: "stateful"
    },
    {
      operation_id: "broker-live.place_order",
      mode: "blocked",
      direct_execution_allowed: false,
      live_trading_blocked: true,
      broker_token_required: true,
      risk_level: "trade_risk"
    },
    {
      operation_id: "broker-live.cancel_order",
      mode: "blocked",
      direct_execution_allowed: false,
      live_trading_blocked: true,
      broker_token_required: true,
      risk_level: "trade_risk"
    }
  ],
  secrets_redacted: true
};

const financialActions = [
  {
    capability_id: "portfolio",
    action_id: "risk",
    label: "Portfolio risk review",
    mode: "read_only",
    status: "ready",
    available: true,
    tool: "agent_portfolio_risk",
    side_effect: {
      level: "read_only",
      target: "agent_portfolio_risk",
      confirmation_required: false,
      idempotent: true
    },
    availability: { required_tool: "agent_portfolio_risk", reason_code: "agent_tool_ready", agent_registry_has_tool: true }
  },
  {
    capability_id: "research",
    action_id: "reports",
    label: "Research manager reports",
    mode: "read_only",
    status: "missing_mcp_tool",
    available: false,
    mcp_tool: "research_manager",
    side_effect: {
      level: "read_only",
      target: "research_manager",
      confirmation_required: false,
      idempotent: true
    },
    availability: {
      required_mcp_tool: "research_manager",
      reason_code: "no_financial_mcp_server_registered",
      financial_servers: []
    }
  },
  {
    capability_id: "portfolio",
    action_id: "create",
    label: "Create portfolio book",
    mode: "stateful_intent",
    status: "intent_ready",
    available: true,
    intent_action: "portfolio_manager.create",
    side_effect: {
      level: "stateful",
      target: "portfolio_manager.create",
      confirmation_required: true,
      idempotent: false
    },
    availability: {
      required_tool: "agent_action_intent_create",
      reason_code: "action_intent_ready",
      agent_registry_has_tool: true
    }
  },
  {
    capability_id: "paper",
    action_id: "submit_order",
    label: "Submit paper order preview",
    mode: "stateful_intent",
    status: "intent_ready",
    available: true,
    intent_action: "paper_trading_manager.submit_order",
    default_params: { dry_run: true },
    side_effect: {
      level: "stateful",
      target: "paper_trading_manager.submit_order",
      confirmation_required: true,
      idempotent: false
    },
    availability: {
      required_tool: "agent_action_intent_create",
      reason_code: "action_intent_ready",
      agent_registry_has_tool: true
    }
  },
  {
    capability_id: "broker-live",
    action_id: "place_order",
    label: "Live broker place order",
    mode: "blocked",
    status: "blocked",
    available: false,
    blocked_reason: "Live broker order placement is blocked from Financial Manager V1.",
    side_effect: {
      level: "trade_risk",
      target: "ths_place_order/qmt_place_order",
      confirmation_required: true,
      idempotent: false
    },
    availability: { reason_code: "blocked", blocked_reason: "live_trading_blocked" }
  },
  {
    capability_id: "broker-live",
    action_id: "cancel_order",
    label: "Live broker cancel order",
    mode: "blocked",
    status: "blocked",
    available: false,
    blocked_reason: "Live broker cancellation is blocked from Financial Manager V1.",
    side_effect: {
      level: "trade_risk",
      target: "ths_cancel_order/qmt_cancel_order",
      confirmation_required: true,
      idempotent: false
    },
    availability: { reason_code: "blocked", blocked_reason: "live_trading_blocked" }
  }
];

function mockFinancialCatalog() {
  return {
    object: "aiask.desktop.financial_manager.catalog",
    groups: [
      { id: "portfolio", label: "Portfolio" },
      { id: "research", label: "Research" },
      { id: "paper", label: "Paper" },
      { id: "broker-live", label: "Live Broker" }
    ],
    actions: financialActions,
    summary: {
      ready: 1,
      missing_mcp_tool: 1,
      intent_ready: 2,
      blocked: 2
    },
    safety: {
      mode: "read_only_plus_intents",
      live_trading_enabled: false,
      stateful_execution: "allowlisted_confirmed_actions",
      matrix: safetyMatrix,
      secrets_redacted: true
    },
    safety_matrix: safetyMatrix,
    stateful_execution: "allowlisted_confirmed_actions",
    dry_run_only_actions: ["paper.submit_order", "paper.cancel_order"],
    secrets_redacted: true
  };
}

function mockFinancialStatus() {
  return {
    object: "aiask.desktop.financial_manager.status",
    status: "ready",
    catalog_summary: mockFinancialCatalog().summary,
    mcp: {
      registration: {
        configured: false,
        registration_status: "not_registered",
        discovery_status: "not_registered",
        missing_auth_env_vars: []
      },
      servers: []
    },
    broker: {
      live_trading_enabled: false,
      read_only_surfaces: [
        "ths_query_position",
        "ths_query_orders",
        "ths_query_deals",
        "qmt_query_account",
        "qmt_query_position",
        "qmt_query_orders"
      ],
      blocked_actions: ["ths_place_order", "ths_cancel_order", "qmt_place_order", "qmt_cancel_order"],
      token_env_presence: safetyMatrix.broker_token_policy.env_presence
    },
    safety_matrix: safetyMatrix,
    recent_intents: [],
    secrets_redacted: true
  };
}

function mockReadiness() {
  return {
    object: "aiask.financial_system_readiness",
    status: "ready",
    backend_ready: true,
    product_ready: true,
    production_ready: true,
    product: {
      backend_ready: true,
      product_ready: true,
      blockers: [],
      desktop_workbench: { included: true, missing: [] }
    },
    required_gates: [
      {
        name: "finance_safe_toolset",
        status: "ready",
        required: true,
        detail: "Default financial tool surface is registered with durable intents.",
        evidence: { toolset: "finance_safe", tool_count: 18 }
      },
      {
        name: "financial_action_safety",
        status: "ready",
        required: true,
        detail: "Financial Manager exposes read-only direct calls, intent-only stateful actions, and blocked live trading.",
        evidence: {
          live_trading_enabled: false,
          stateful_actions: 2,
          blocked_trade_actions: 2,
          secrets_redacted: true
        }
      },
      {
        name: "action_intent_reconciliation",
        status: "ready",
        required: true,
        detail: "No ActionIntent execution outcomes are awaiting reconciliation.",
        evidence: {
          execution_unknown: 0,
          executing: 0,
          sample: [],
          endpoint: "/intents?status=execution_unknown",
          auto_replay: false,
          secrets_redacted: true
        }
      },
      {
        name: "control_plane",
        status: "ready",
        required: true,
        detail: "Control token is configured for confirm/deny and full-mode actions.",
        evidence: { loopback_only: true, control_token_configured: true }
      },
      {
        name: "database",
        status: "ready",
        required: true,
        detail: "SQLite database is ready for quant and strategy factory runs.",
        evidence: { backend: "sqlite", writable: true }
      }
    ],
    optional_gates: [
      {
        name: "mcp_aggregation",
        status: "degraded",
        required: false,
        detail: "MCP aggregation is optional until external financial tool aggregation is required.",
        evidence: { configured: false, registration_status: "not_registered" }
      }
    ],
    next_actions: [
      {
        action_id: "run_live_financial_workflow",
        title: "Run one live read-only financial workflow",
        detail: "Start with Financial Manager read-only risk/query, then confirm evidence and data freshness.",
        priority: "recommended",
        target_page: "financial-manager",
        endpoint: "/v1/desktop/financial-manager/query"
      }
    ],
    safety_matrix: safetyMatrix,
    financial_safety_matrix: safetyMatrix,
    summary: {
      required_total: 5,
      required_ready: 5,
      required_degraded: 0,
      required_blocked: 0,
      product_blockers: 0,
      product_ready: true,
      backend_ready: true,
      unknown_execution_intents: 0,
      executing_intents: 0
    },
    disclaimer:
      "Production readiness excludes brokerage credentials and real trading enablement; all stateful strategy and financial actions still require durable intent confirmation."
  };
}

function mockQuery(body: unknown) {
  const payload = objectData(body);
  const capabilityId = String(payload.capability_id || "");
  const actionId = String(payload.action_id || "");
  if (capabilityId === "broker-live") {
    return {
      object: "aiask.desktop.financial_manager.query",
      success: false,
      data: { reason: "live_trading_blocked" },
      error: "Live broker actions are blocked from Financial Manager V1.",
      error_code: "FINANCIAL_ACTION_BLOCKED",
      secrets_redacted: true
    };
  }
  if (capabilityId === "portfolio" && actionId === "create") {
    return {
      object: "aiask.desktop.financial_manager.query",
      success: false,
      data: { required_endpoint: "/v1/desktop/financial-manager/intent" },
      error: "stateful financial actions must be created as ActionIntent",
      error_code: "FINANCIAL_ACTION_REQUIRES_INTENT",
      secrets_redacted: true
    };
  }
  return {
    object: "aiask.desktop.financial_manager.query",
    capability_id: capabilityId || "portfolio",
    action_id: actionId || "risk",
    tool: "agent_portfolio_risk",
    success: true,
    data: {
      summary: "Mock read-only risk review completed.",
      risk_score: 0.34,
      concentration: "moderate",
      params: objectData(payload.params)
    },
    error: null,
    error_code: null,
    meta: { side_effect: { level: "read_only" }, mock: true },
    secrets_redacted: true
  };
}

function mockIntent(body: unknown) {
  const payload = objectData(body);
  const capabilityId = String(payload.capability_id || "");
  const actionId = String(payload.action_id || "");
  if (capabilityId === "broker-live") {
    return {
      object: "aiask.desktop.financial_manager.intent",
      success: false,
      data: { reason: "live_trading_blocked" },
      error: "Live broker actions are blocked from Financial Manager V1.",
      error_code: "FINANCIAL_ACTION_BLOCKED",
      secrets_redacted: true
    };
  }
  const intentId = `intent_mock_${Date.now()}`;
  const action =
    capabilityId === "paper" && actionId === "submit_order"
      ? "paper_trading_manager.submit_order"
      : "portfolio_manager.create";
  return {
    object: "aiask.desktop.financial_manager.intent",
    capability_id: capabilityId || "portfolio",
    action_id: actionId || "create",
    success: true,
    data: {
      intent: {
        intent_id: intentId,
        action,
        status: "awaiting_confirmation",
        params: {
          ...objectData(payload.params),
          ...(action.startsWith("paper_") ? { dry_run: true } : {})
        },
        rationale: String(payload.rationale || "Mock Financial Manager intent")
      }
    },
    error: null,
    error_code: null,
    meta: { side_effect: { level: "stateful", confirmation_required: true }, mock: true },
    secrets_redacted: true
  };
}

export async function mockRequest<T = unknown>({ path, method, body, query }: MockRequest): Promise<T> {
  const normalized = path.split("?")[0];
  if (normalized === "/health") {
    return clone({
      status: "ok",
      service: "aiask-agent",
      mode: "mock",
      timestamp: nowIso()
    }) as T;
  }
  if (normalized === "/v1/desktop/workbench/summary") {
    return clone({
      object: "aiask.desktop.workbench.summary",
      access: { full_mode_active: false, sessions_admin_available: true },
      queues: { pending_intents: 1, unknown_execution_intents: 0, pending_approvals: 0, gateway_failed: 0, mcp_degraded: 1 },
      recent_sessions: [
        {
          session_id: "sess_mock_finance",
          title: "Financial risk review",
          message_count: 4,
          updated_at: nowIso(),
          has_pending_approval: true,
          has_unknown_execution: false
        }
      ],
      recent_runs: [
        {
          run_id: "run_mock_finance",
          session_id: "sess_mock_finance",
          status: "completed",
          event_count: 3,
          has_errors: false
        }
      ]
    }) as T;
  }
  if (normalized === "/v1/hermes/readiness" || normalized === "/v1/financial-system/readiness") {
    return clone(mockReadiness()) as T;
  }
  if (normalized === "/v1/desktop/financial-manager/catalog") {
    return clone(mockFinancialCatalog()) as T;
  }
  if (normalized === "/v1/desktop/financial-manager/status") {
    return clone(mockFinancialStatus()) as T;
  }
  if (normalized === "/v1/desktop/financial-manager/query" && method === "POST") {
    return clone(mockQuery(body)) as T;
  }
  if (normalized === "/v1/desktop/financial-manager/intent" && method === "POST") {
    return clone(mockIntent(body)) as T;
  }
  if (normalized === "/v1/desktop/broker-readiness") {
    return clone({
      object: "aiask.desktop.broker_readiness",
      success: true,
      read_only: true,
      live_trading_enabled: false,
      providers: [
        { provider: "tonghuashun", configured: false, read_only: true },
        { provider: "qmt", configured: false, read_only: true }
      ],
      blocked_actions: safetyMatrix.broker_token_policy.guarded_tools,
      secrets_redacted: true
    }) as T;
  }
  if (normalized === "/v1/mcp/servers") {
    return clone({
      object: "list",
      data: [
        {
          id: "akshare-local",
          name: "akshare-local",
          domain: "financial",
          transport: "streamable_http",
          enabled: false,
          tools: ["quote"],
          auth_configured: true,
          missing_auth_env_vars: []
        }
      ],
      secrets_redacted: true
    }) as T;
  }
  if (normalized === "/v1/mcp/tools") {
    return clone({
      object: "list",
      data: [
        {
          server: "akshare-local",
          domain: "financial",
          name: "quote",
          wrapped_name: "agent_mcp_akshare_local_quote",
          description: "Safe read-only financial quote surface",
          side_effect: { level: "read_only" }
        }
      ],
      secrets_redacted: true
    }) as T;
  }
  if (normalized === "/v1/responses" && method === "POST") {
    const payload = objectData(body);
    return clone({
      id: `resp_mock_${Date.now()}`,
      object: "response",
      status: "completed",
      output_text: `Mock Agent response for: ${String(payload.input || "").slice(0, 160)}`,
      model: String(payload.model || "mock-finance-safe"),
      metadata: { mode: "finance_safe", tool_calls: [], mock: true }
    }) as T;
  }
  if (normalized === "/v1/desktop/data/status") {
    return clone({
      object: "aiask.desktop.data.status",
      status: "ready",
      database: { configured: true, writable: true },
      codes: String(query?.codes || "600519,000001")
        .split(",")
        .filter(Boolean),
      missing_count: 0,
      stale_count: 0,
      secrets_redacted: true
    }) as T;
  }
  throw new Error(`Mock route is not implemented: ${method} ${path}`);
}
