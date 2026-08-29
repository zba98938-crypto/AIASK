import {
  Activity,
  BarChart3,
  HeartPulse,
  LayoutDashboard,
  PlugZap,
  Settings
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type ViewId = "workbench" | "financial-manager" | "readiness-health" | "mcp-connectors" | "settings";

export interface ViewDefinition {
  id: ViewId;
  label: string;
  shortLabel: string;
  description: string;
  group: "agent" | "finance" | "ops";
  icon: LucideIcon;
}

export const VIEWS: ViewDefinition[] = [
  {
    id: "workbench",
    label: "Agent Workbench",
    shortLabel: "Workbench",
    description: "Agent HTTP status, recent run context, and product readiness at a glance.",
    group: "agent",
    icon: LayoutDashboard
  },
  {
    id: "financial-manager",
    label: "Financial Manager",
    shortLabel: "Finance",
    description: "Read-only financial queries, guarded intents, and blocked live-trading visibility.",
    group: "finance",
    icon: BarChart3
  },
  {
    id: "readiness-health",
    label: "Readiness & Health",
    shortLabel: "Readiness",
    description: "Backend/product readiness gates, next actions, and financial safety evidence.",
    group: "ops",
    icon: HeartPulse
  },
  {
    id: "mcp-connectors",
    label: "MCP / Connectors",
    shortLabel: "MCP",
    description: "Default safe financial MCP visibility plus full-mode gate status.",
    group: "ops",
    icon: PlugZap
  },
  {
    id: "settings",
    label: "Settings / Mode",
    shortLabel: "Settings",
    description: "Endpoint, API token, control token, user, and mock/live mode.",
    group: "ops",
    icon: Settings
  }
];

export const DEFAULT_VIEW: ViewId = "workbench";

export function viewById(id: ViewId): ViewDefinition {
  return VIEWS.find((view) => view.id === id) ?? VIEWS[0];
}

export function groupLabel(group: ViewDefinition["group"]): string {
  if (group === "finance") return "Finance";
  if (group === "ops") return "Operations";
  return "Agent";
}

export function viewsByGroup(): Array<{ group: ViewDefinition["group"]; label: string; views: ViewDefinition[] }> {
  const groups: ViewDefinition["group"][] = ["agent", "finance", "ops"];
  return groups.map((group) => ({
    group,
    label: groupLabel(group),
    views: VIEWS.filter((view) => view.group === group)
  }));
}
