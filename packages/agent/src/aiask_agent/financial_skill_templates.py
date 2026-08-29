from __future__ import annotations


FINANCIAL_SKILL_TEMPLATES: dict[str, dict[str, str]] = {
    "aiask-finance-excel-author": {
        "description": "Auditable headless Excel authoring for financial models.",
        "content": """---
name: aiask-finance-excel-author
description: Auditable headless Excel authoring for financial models.
---

# AIASK Finance Excel Author

Use this skill when a financial analysis must produce an `.xlsx` artifact.

## Non-Negotiable Rules
- Use formulas for all derived cells; only raw historical inputs, market data, and assumption drivers may be hardcoded.
- Add a source comment for every hardcoded input with provider, date, reference, and retrieval path.
- Use named ranges for assumptions that feed other sheets.
- Include a Checks sheet for balance, tie-out, formula error, and source coverage checks.
- Prefer MCP or repository data sources before web search. If a required field is missing, emit a gap report instead of filling placeholders.

## Workflow
1. Map workbook tabs and inputs.
2. Confirm historical data and assumptions.
3. Build formulas section by section.
4. Run recalculation and formula-error checks.
5. Return artifact path plus audit summary.
""",
    },
    "aiask-finance-dcf-model": {
        "description": "DCF valuation model template with source lineage and sensitivity checks.",
        "content": """---
name: aiask-finance-dcf-model
description: DCF valuation model template with source lineage and sensitivity checks.
---

# AIASK DCF Model

Use for intrinsic equity valuation where the output must be auditable.

## Required Sections
- Historical revenue, margin, tax, capex, D&A, working capital, net debt, share count.
- Projection drivers for Bear/Base/Bull cases.
- FCF build, WACC, terminal value, enterprise value to equity bridge.
- Sensitivity tables with the base case centered.

## Controls
- Do not compute valuation outputs in Python and paste values; write formulas.
- Every assumption must have an evidence id or source comment.
- Stop and ask for missing market data, WACC assumptions, or share count instead of inventing them.
""",
    },
    "aiask-finance-comps-analysis": {
        "description": "Comparable company analysis template for institutional peer benchmarking.",
        "content": """---
name: aiask-finance-comps-analysis
description: Comparable company analysis template for institutional peer benchmarking.
---

# AIASK Comparable Company Analysis

Use for valuation multiples, peer benchmarking, IPO pricing, and sector comparison.

## Data Priority
1. AIASK MCP data sources and local research evidence.
2. Exchange filings or institutional sources.
3. Web search only as a last resort and never as sole evidence.

## Required Outputs
- Peer selection rationale and exclusions.
- Operating metrics, valuation multiples, growth, margin, leverage, liquidity.
- Median, mean, quartiles, min/max, and outlier notes.
- Source comments for every raw input.
""",
    },
    "aiask-finance-three-statement-model": {
        "description": "Integrated income statement, balance sheet, and cash flow model template.",
        "content": """---
name: aiask-finance-three-statement-model
description: Integrated income statement, balance sheet, and cash flow model template.
---

# AIASK Three Statement Model

Use when building linked IS, BS, and CF projections.

## Required Checks
- Balance sheet balances in every period.
- Cash flow ending cash ties to balance sheet cash.
- Retained earnings roll-forward ties.
- Debt, D&A, working capital, and tax schedules link into the statements.

## Workflow
Build and verify one statement or schedule at a time. Do not present a finished model until tie-outs pass.
""",
    },
    "aiask-finance-lbo-model": {
        "description": "LBO model template for sponsor-case screening and return analysis.",
        "content": """---
name: aiask-finance-lbo-model
description: LBO model template for sponsor-case screening and return analysis.
---

# AIASK LBO Model

Use for leveraged buyout screening, sponsor case valuation, and illustrative debt capacity analysis.

## Required Sections
- Sources and uses.
- Operating forecast.
- Debt schedule with cash sweep.
- Exit valuation, IRR, MOIC, and sensitivity tables.

## Controls
- Follow provided templates when available.
- Keep formulas dynamic and sign conventions consistent.
- Document financing assumptions, fees, rates, amortization, and exit multiple sources.
""",
    },
    "aiask-finance-merger-model": {
        "description": "Accretion/dilution merger model template.",
        "content": """---
name: aiask-finance-merger-model
description: Accretion/dilution merger model template.
---

# AIASK Merger Model

Use for M&A accretion/dilution analysis and deal consequence review.

## Required Inputs
- Acquirer and target share price, shares, EPS or net income, net debt, tax rate.
- Consideration mix, offer premium, financing assumptions, synergies, fees, close timing.

## Required Outputs
- Purchase price, sources and uses, pro forma EPS, accretion/dilution.
- Synergy and financing sensitivities.
- Source comments for all hardcoded numbers.
""",
    },
    "aiask-finance-pptx-author": {
        "description": "Model-backed PowerPoint authoring template for IC and pitch materials.",
        "content": """---
name: aiask-finance-pptx-author
description: Model-backed PowerPoint authoring template for IC and pitch materials.
---

# AIASK Finance PPTX Author

Use when turning model-backed analysis into a deck artifact.

## Rules
- One takeaway per slide.
- Every number must trace to a workbook cell, evidence id, or MCP source.
- Use a provided firm template when available.
- Do not email, upload, or send externally; only create the deck artifact.
""",
    },
}


__all__ = ["FINANCIAL_SKILL_TEMPLATES"]

