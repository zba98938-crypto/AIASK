from __future__ import annotations

from typing import Any


AIASK_SKILL_PACKS: dict[str, dict[str, Any]] = {
    "aiask-hermes-core": {
        "title": "AIASK Hermes Core Native Pack",
        "description": "AIASK-native operating skills for tool safety, delegation, browser/file/terminal work, and MCP use.",
        "skills": {
            "aiask-hermes-tool-safety": {
                "description": "Native tool safety workflow for AIASK full mode.",
                "content": """# AIASK Hermes Tool Safety

Use this skill when operating AIASK full mode tools. Classify each action as read_only, filesystem_write, process_execution, external_message, stateful, or trade-risk before calling it. Prefer dry-run or preview actions when available. For filesystem, process, external-message, MCP stateful, or trade-risk actions, require an explicit durable intent or control-token path and keep an audit trail of the target, rationale, and rollback option.
""",
            },
            "aiask-hermes-delegation": {
                "description": "Native delegation protocol for bounded sub-agent work.",
                "content": """# AIASK Hermes Delegation

Delegate only bounded tasks with a clear output contract, toolset, and maximum iterations. Keep finance_safe for financial review by default. Use general_full only when the user has enabled full mode and the delegated task truly needs file, terminal, browser, plugin, gateway, or code execution tools.
""",
            },
        },
    },
    "aiask-financial-modeling": {
        "title": "AIASK Financial Modeling Pack",
        "description": "AIASK-native finance templates for DCF, comps, three-statement modeling, LBO, M&A, and outputs.",
        "skills": {
            "aiask-finance-dcf-model": {
                "description": "DCF model workflow with formula-first outputs and audit trail.",
                "content": """# AIASK Finance DCF Model

Build DCF work in phases: scope, source collection, historical normalization, forecast drivers, WACC, terminal value, sensitivity, and audit review. Prefer MCP market and financial data before external web sources. Every assumption must carry a source comment, timestamp, and confidence note. Do not emit final valuation ranges without checking formula consistency and downside cases.
""",
            },
            "aiask-finance-comps-model": {
                "description": "Comparable company analysis workflow.",
                "content": """# AIASK Finance Comps Model

Define peer criteria before collecting multiples. Separate operating metrics, valuation metrics, outlier handling, and currency/market normalization. Keep raw source values distinct from adjusted values and preserve an audit trail for exclusions.
""",
            },
            "aiask-finance-three-statement-model": {
                "description": "Three-statement model construction guardrails.",
                "content": """# AIASK Finance Three Statement Model

Model income statement, balance sheet, and cash flow statement with explicit links between working capital, debt, depreciation, capex, taxes, and cash. Use formula-first construction. Flag imbalance, circularity, and unsupported assumptions before presenting outputs.
""",
            },
            "aiask-finance-lbo-model": {
                "description": "LBO model workflow with financing and exit checks.",
                "content": """# AIASK Finance LBO Model

Separate transaction assumptions, sources and uses, debt schedule, operating case, exit assumptions, returns bridge, and covenant stress. Require staged confirmation before treating outputs as investment advice.
""",
            },
            "aiask-finance-ma-model": {
                "description": "M&A model workflow for accretion/dilution and synergy cases.",
                "content": """# AIASK Finance M&A Model

Track standalone buyer/seller baselines, purchase price, consideration mix, financing, synergies, integration costs, tax effects, and pro forma EPS. Present base/upside/downside cases and preserve source comments for every market and financial input.
""",
            },
            "aiask-finance-excel-pptx-output": {
                "description": "Excel/PPTX output standards for financial analysis artifacts.",
                "content": """# AIASK Finance Excel PPTX Output

Separate inputs, calculations, checks, outputs, and audit sheets. Inputs need source comments. Formulas should be inspectable and avoid hardcoded outputs. Slides should distinguish facts, assumptions, model outputs, and recommendations.
""",
            },
        },
    },
}


class SkillPackManager:
    def __init__(self, *, skill_store: Any) -> None:
        self.skill_store = skill_store

    def list(self) -> list[dict[str, Any]]:
        installed = {str(item.get("name")) for item in self.skill_store.list()}
        packs: list[dict[str, Any]] = []
        for name, spec in AIASK_SKILL_PACKS.items():
            skill_names = sorted(spec["skills"])
            packs.append(
                {
                    "name": name,
                    "title": spec["title"],
                    "description": spec["description"],
                    "skill_count": len(skill_names),
                    "skills": skill_names,
                    "installed_count": sum(1 for skill in skill_names if skill in installed),
                    "source": "aiask_native_rewrite",
                }
            )
        return packs

    def status(self) -> dict[str, Any]:
        packs = self.list()
        return {
            "object": "aiask.skill_pack_status",
            "packs": packs,
            "pack_count": len(packs),
            "installed_skill_count": sum(int(item.get("installed_count") or 0) for item in packs),
            "source": "aiask_native_rewrite",
            "vendor_text_copied": False,
            "status": "implemented",
        }

    def install(self, name: str, *, overwrite: bool = False) -> dict[str, Any]:
        pack_name = str(name or "").strip()
        if pack_name not in AIASK_SKILL_PACKS:
            raise ValueError(f"unknown AIASK skill pack: {pack_name}")
        spec = AIASK_SKILL_PACKS[pack_name]
        installed: list[dict[str, Any]] = []
        skipped: list[str] = []
        for skill_name, skill in spec["skills"].items():
            exists = any(item.get("name") == skill_name for item in self.skill_store.list())
            if exists and not overwrite:
                skipped.append(skill_name)
                continue
            installed.append(
                self.skill_store.save(
                    skill_name,
                    str(skill["content"]),
                    description=str(skill["description"]),
                )
            )
        return {
            "pack": pack_name,
            "installed": installed,
            "skipped": skipped,
            "installed_count": len(installed),
            "source": "aiask_native_rewrite",
        }

    def audit(self) -> dict[str, Any]:
        installed = {str(item.get("name")) for item in self.skill_store.list()}
        missing: list[dict[str, Any]] = []
        for pack_name, spec in AIASK_SKILL_PACKS.items():
            for skill_name in spec["skills"]:
                if skill_name not in installed:
                    missing.append({"pack": pack_name, "skill": skill_name, "severity": "info", "code": "skill_pack_item_not_installed"})
        return {
            "packs": self.list(),
            "issues": missing,
            "issue_count": len(missing),
            "vendor_text_copied": False,
        }

