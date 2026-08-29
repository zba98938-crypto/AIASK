from __future__ import annotations

from typing import Any

from ..intents import ALLOWED_ACTIONS
from .schema_general_full import GENERAL_FULL_TOOL_SCHEMAS
from .schema_helpers import schema


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "agent_tool_catalog": schema({}),
    "agent_analyze_stock": schema(
        {
            "code": {"type": "string", "description": "Stock code, symbol, or ticker."},
            "symbol": {"type": "string", "description": "Alias for code."},
            "as_of": {"type": "string", "description": "Point-in-time cutoff date."},
            "include_decision": {
                "type": "boolean",
                "default": False,
                "description": "Include upstream decision context. Defaults to false; returned context is never a trade instruction.",
            },
        },
        required=["code"],
    ),
    "agent_stock_live_quote": schema(
        {
            "code": {"type": "string", "description": "Stock code, symbol, or ticker."},
            "stock_code": {"type": "string", "description": "Alias for code."},
            "symbol": {"type": "string", "description": "Alias for code."},
            "ticker": {"type": "string", "description": "Alias for code."},
            "include_source_chain": {"type": "boolean", "default": True},
        },
        required=["code"],
    ),
    "agent_stock_news_digest": schema(
        {
            "code": {"type": "string", "description": "Stock code, symbol, or ticker. Omit for market news."},
            "stock_code": {"type": "string", "description": "Alias for code."},
            "symbol": {"type": "string", "description": "Alias for code."},
            "ticker": {"type": "string", "description": "Alias for code."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "prefer_db": {"type": "boolean", "default": True},
            "include_links": {"type": "boolean", "default": True},
        },
    ),
    "agent_governance_check": schema(
        {
            "target_type": {"type": "string", "enum": ["factor", "model", "strategy", "system"]},
            "target_id": {"type": "string"},
            "ic_history": {"type": "array", "items": {"type": "number"}},
            "current_metrics": {"type": "object"},
            "baseline_metrics": {"type": "object"},
            "as_of": {"type": "string"},
        }
    ),
    "agent_data_validation": schema(
        {
            "action": {"type": "string", "enum": ["validate", "backend", "checkpoint"], "default": "validate"},
            "records": {"type": "array", "items": {"type": "object"}},
            "expectations": {"type": "object"},
            "dataset_id": {"type": "string"},
            "minimum_quality_threshold": {"type": "number", "default": 0.95},
        }
    ),
    "agent_quant_data_gate": schema(
        {
            "codes": {"type": "array", "items": {"type": "string"}},
            "universe": {"type": "array", "items": {"type": "string"}},
            "max_stale_days": {"type": "integer", "minimum": 0, "default": 5},
        }
    ),
    "agent_factor_validation": schema(
        {
            "codes": {"type": "array", "items": {"type": "string"}},
            "universe": {"type": "array", "items": {"type": "string"}},
            "factors": {"type": "array", "items": {"type": "string"}},
            "period": {"type": "integer", "minimum": 1, "default": 20},
            "groups": {"type": "integer", "minimum": 2, "default": 5},
            "holding_days": {"type": "integer", "minimum": 1, "default": 20},
            "cost_bps": {"type": "number", "default": 3},
            "slippage_bps": {"type": "number", "default": 1},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "include_oos": {"type": "boolean", "default": True},
            "include_robustness": {"type": "boolean", "default": True},
        }
    ),
    "agent_backtest_suite": schema(
        {
            "codes": {"type": "array", "items": {"type": "string"}},
            "universe": {"type": "array", "items": {"type": "string"}},
            "strategy": {"type": "string", "default": "ma_cross"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "benchmark": {"type": "string", "default": "000300"},
            "rebalance_frequency": {"type": "string", "default": "monthly"},
            "cost_bps": {"type": "number", "default": 3},
            "slippage_bps": {"type": "number", "default": 1},
            "initial_capital": {"type": "number", "default": 100000},
            "use_parallel": {"type": "boolean", "default": True},
            "fallback_to_single": {"type": "boolean", "default": True},
        }
    ),
    "agent_portfolio_risk": schema(
        {
            "codes": {"type": "array", "items": {"type": "string"}},
            "universe": {"type": "array", "items": {"type": "string"}},
            "weights": {"type": "array", "items": {"type": "number"}},
            "method": {"type": "string", "default": "equal_weight"},
            "lookback_days": {"type": "integer", "minimum": 2, "default": 252},
            "risk_limits": {"type": "object"},
            "stress_scenarios": {"type": "array", "items": {"type": "string"}},
            "include_barra": {"type": "boolean", "default": True},
        }
    ),
    "agent_quant_research_run": schema(
        {
            "research_id": {"type": "string"},
            "universe": {"type": "array", "items": {"type": "string"}},
            "codes": {"type": "array", "items": {"type": "string"}},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "benchmark": {"type": "string", "default": "000300"},
            "rebalance_frequency": {"type": "string", "default": "monthly"},
            "cost_bps": {"type": "number", "default": 3},
            "slippage_bps": {"type": "number", "default": 1},
            "factors": {"type": "array", "items": {"type": "string"}},
            "risk_limits": {"type": "object"},
            "strategy_id": {"type": "string"},
            "include_strategy_review": {"type": "boolean", "default": True},
        }
    ),
    "agent_market_temperature_snapshot": schema(
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 300},
            "top_n": {"type": "integer", "minimum": 0, "maximum": 50, "default": 8},
            "as_of": {"type": "string"},
            "min_bars": {"type": "integer", "minimum": 2, "maximum": 120, "default": 20},
            "use_cache": {"type": "boolean", "default": True},
        }
    ),
    "agent_market_temperature_cache_readiness": schema(
        {
            "as_of": {"type": "string"},
            "max_stale_days": {"type": "integer", "minimum": 0, "default": 1},
        }
    ),
    "agent_market_temperature_cache_history": schema(
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
            "include_snapshot": {"type": "boolean", "default": False},
        }
    ),
    "agent_market_temperature_industry_history": schema(
        {
            "industry": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 365, "default": 120},
            "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "match_mode": {"type": "string", "enum": ["exact", "contains"], "default": "exact"},
            "include_source_chain": {"type": "boolean", "default": False},
        }
    ),
    "agent_market_temperature_industry_constituents": schema(
        {
            "industry": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            "offset": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0},
            "match_mode": {"type": "string", "enum": ["exact", "contains"], "default": "contains"},
            "include_source_chain": {"type": "boolean", "default": False},
        },
        required=["industry"],
    ),
    "agent_market_temperature_forward_validation": schema(
        {
            "limit": {"type": "integer", "minimum": 2, "maximum": 365, "default": 180},
            "horizons": {"type": ["array", "string", "integer", "null"], "default": [1, 3, 5]},
            "target_field": {
                "type": "string",
                "enum": ["weighted_pct_change", "avg_pct_change", "temperature_delta", "benchmark_return"],
                "default": "weighted_pct_change",
            },
            "benchmark_code": {"type": "string", "default": "000300"},
            "min_samples": {"type": "integer", "minimum": 1, "maximum": 100, "default": 3},
            "neutral_band_pct": {"type": "number", "minimum": 0.0, "maximum": 5.0, "default": 0.2},
            "include_samples": {"type": "boolean", "default": False},
        }
    ),
    "agent_factory_status": schema(
        {
            "recent_run_limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "factory": {
                "type": "string",
                "enum": ["strategy_factory", "factor_mining", "incubation", "market_event_ingest"],
                "description": "Optional unified control-plane factory. Omit for the legacy detailed Strategy Factory view.",
            },
        }
    ),
    "agent_factory_runs": schema(
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "factory": {"type": "string"},
            "status": {"type": "string"},
            "run_id": {"type": "string"},
        }
    ),
    "agent_factory_readiness": schema({"factory": {"type": "string"}}),
    "agent_strategy_review_snapshot": schema(
        {
            "strategy_id": {"type": "string"},
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        }
    ),
    "agent_strategy_domain_events": schema(
        {
            "event_type": {"type": "string"},
            "strategy_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_factory_event_list": schema(
        {
            "event_id": {"type": "string"},
            "source": {"type": "string"},
            "status": {"type": "string"},
            "event_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_factory_event_preview_tasks": schema(
        {
            "event_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        required=["event_id"],
    ),
    "agent_factory_event_lineage": schema(
        {
            "event_id": {"type": "string"},
            "strategy_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_factory_theme_exposure_status": schema(
        {
            "theme": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_factory_event_outbox_status": schema(
        {
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_incubation_factory_status": schema({}),
    "agent_trade_prediction_status": schema(
        {
            "strategy_id": {"type": "string"},
            "stock_code": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
        }
    ),
    "agent_trade_prediction_outcomes": schema(
        {
            "prediction_id": {"type": "string"},
            "strategy_id": {"type": "string"},
            "stock_code": {"type": "string"},
            "score_version": {"type": "string"},
            "score_status": {"type": "string"},
            "data_quality_status": {"type": "string"},
            "actual_trading_date_lte": {"type": "string"},
            "actual_trading_date_gte": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    ),
    "agent_trade_prediction_matrix": schema(
        {
            "strategy_id": {"type": "string"},
            "stock_code": {"type": "string"},
            "score_version": {"type": "string"},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
        }
    ),
    "agent_stock_radar_status": schema(
        {
            "run_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_stock_radar_candidates": schema(
        {
            "run_id": {"type": "string"},
            "tier": {"type": "string", "enum": ["", "alert", "watch", "observe", "reject"]},
            "symbol": {"type": "string"},
            "stock_code": {"type": "string"},
            "min_score": {"type": "number", "minimum": 0, "maximum": 100},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        }
    ),
    "agent_stock_radar_digest": schema(
        {
            "run_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "channels": {"type": "array", "items": {"type": "string"}},
            "record_preview": {"type": "boolean", "default": False},
        }
    ),
    "agent_action_intent_create": schema(
        {
            "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
            "params": {"type": "object"},
            "user_id": {"type": "string"},
            "rationale": {"type": "string"},
            "ttl_seconds": {"type": "integer", "minimum": 60},
        },
        required=["action"],
    ),
    "agent_action_intent_get": schema(
        {
            "intent_id": {"type": "string"},
        },
        required=["intent_id"],
    ),
    **GENERAL_FULL_TOOL_SCHEMAS,
}
