"""Modularized loader for research_510300_v3.py."""
from __future__ import annotations

# --- research_510300_v3_parts/data_loading.py ---
import importlib.util
import json
import math
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

# NOTE: Direct `import tushare as ts` was removed in PR-9 (PROJECT_FIX_PLAN.md).
# All Tushare access in this module already routes through the
# DataSourceManager singleton via `data_source.get_tushare_pro()` below,
# which honours the TDX-local-first failover chain plus the Tushare
# whitelist. The bare top-level import was dead code that violated
# AGENT.md S5.6 (single-source data access).

from strategy_factory.api.contracts import FactoryBacktestAssumptions
from strategy_factory.api.semantic_contract import (
    build_research_validation_contract,
)

# Statistics come from the one implementation, not a local copy. This module
# used to import a byte-identical 848-line duplicate that lived beside it as
# ``services/_validation_support.py``; the two had already drifted apart by a
# BOM, and any fix applied to one would silently miss the other.
from aiask_quant_core._validation_support import (
    hansen_spa_test,
    probability_of_backtest_overfitting,
    white_reality_check,
)
from .backtest.engine import BacktestEngine
from .validation import PurgedKFoldCV, WalkForwardValidator
import logging

logger = logging.getLogger(__name__)


MONTHLY_CONTRIBUTION = 10_000.0
MAIN_SCENARIO_SLIPPAGE_BPS = 5.0
CONTROL_SCENARIO_SLIPPAGE_BPS = 0.0
DEFAULT_END_DATE = "2026-04-10"
DEFAULT_TAKE_PROFIT_GRID = (0.15, 0.20, 0.25, 0.30)
DEFAULT_ROTATION_LOOKBACK_MONTHS = (3, 6, 9)
DEFAULT_ROTATION_TREND_WINDOWS = (100, 150)
DEFAULT_LEVERAGE_GRID = (1.5, 2.0)
FUTURES_FALLBACK_COST = {
    "slippage_bps": 2.0,
    "fixed_fee_per_contract": 20.0,
    "margin_rate": 0.12,
    "contract_multiplier": 300,
}
ROOT_REPORT_MD = "510300沪深300ETF定投回测正式报告.md"
ROOT_REPORT_PDF = "510300沪深300ETF定投回测正式报告_图表版.pdf"


def _discover_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "packages").exists() and (parent / "scripts").exists():
            return parent
    return current.parents[6]


def _runtime_home() -> Path:
    override = os.getenv("AIASK_RUNTIME_HOME")
    if override:
        return Path(override).expanduser().resolve()
    localapp = os.getenv("LOCALAPPDATA")
    if localapp:
        return (Path(localapp) / "hermes-aiask").resolve()
    xdg_state = os.getenv("XDG_STATE_HOME")
    if xdg_state:
        return (Path(xdg_state).expanduser() / "hermes-aiask").resolve()
    return (Path.home() / ".aiask").resolve()


REPO_ROOT = _discover_repo_root()
REPORTS_ROOT = _runtime_home() / "reports" / "backtests"
V3_REPORT_ROOT = REPORTS_ROOT / "510300_v3"
V3_RUNS_ROOT = V3_REPORT_ROOT / "runs"
LEGACY_SCRIPT_PATH = REPO_ROOT / "scripts" / "backtest_510300_etf_dca.py"
BASELINE_REFERENCE_DIR = REPORTS_ROOT


@dataclass(frozen=True)
class ResearchProtocol:
    end_date: str = DEFAULT_END_DATE
    baseline_slippage_bps: float = MAIN_SCENARIO_SLIPPAGE_BPS
    stress_slippage_bps: float = 10.0
    train_months: int = 60
    test_months: int = 12
    step_months: int = 12
    enable_cash_sleeves: bool = False
    enable_enhancements: bool = False
    schedule_clock: str = "prev_close_signal_next_open_execute_same_close_cash_rebuild"
    baseline_reference: str = ""


@dataclass(frozen=True)
class ResolvedInstrument:
    category: str
    keyword_group: str
    code: str
    symbol: str
    name: str
    source: str
    history_rows: int
    first_trade_date: str
    last_trade_date: str
    median_amount_60d: float
    candidates: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None


@dataclass(frozen=True)
class CostScenarioResult:
    scenario: str
    slippage_bps: float
    commission_rate: float
    sell_tax_rate: float
    strategy_metrics: dict[str, dict[str, Any]]
    summary_source: str


@dataclass(frozen=True)
class FoldResult:
    fold_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    selected_candidates: dict[str, dict[str, Any]]
    oos_metrics: dict[str, dict[str, Any]]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CashSleeveResult:
    family: str
    selected_instrument: dict[str, Any] | None
    metrics: dict[str, Any]
    scheduler_audit: dict[str, Any]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnhancementFamilyResult:
    family: str
    selected_candidate: dict[str, Any] | None
    aggregate_oos: dict[str, Any]
    validation: dict[str, Any]
    candidate_count: int
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SelectionGateResult:
    family: str
    passed: bool
    oos_cagr: float
    benchmark_oos_cagr: float
    oos_max_drawdown: float
    benchmark_oos_max_drawdown: float
    selected_candidate: dict[str, Any] | None
    reason: str


@dataclass(frozen=True)
class FinalRecommendation:
    decision: str
    selected_family: str | None
    selected_candidate: dict[str, Any] | None
    summary: str
    passed_gate: bool


@dataclass
class StrategyArtifact:
    name: str
    metrics: dict[str, Any]
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    daily_returns: pd.Series
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BundleArtifacts:
    bundle_dir: Path
    summary_path: Path
    markdown_path: Path
    csv_dir: Path
    latest_json_path: Path
    latest_markdown_path: Path
    latest_pdf_path: Path


def build_default_research_validation_contract(
    protocol: ResearchProtocol | None = None,
) -> dict[str, Any]:
    resolved = protocol or ResearchProtocol()
    return build_research_validation_contract(
        walk_forward_config={
            "train_months": int(resolved.train_months),
            "test_months": int(resolved.test_months),
            "step_months": int(resolved.step_months),
        },
        baseline_reference={
            "name": str(resolved.baseline_reference or "510300_baseline_reference"),
            "end_date": str(resolved.end_date),
            "baseline_slippage_bps": float(resolved.baseline_slippage_bps),
            "stress_slippage_bps": float(resolved.stress_slippage_bps),
        },
        cash_sleeve_policy={
            "enabled": bool(resolved.enable_cash_sleeves),
            "schedule_clock": str(resolved.schedule_clock),
        },
        cost_sensitivity_grid={
            "base_slippage_bps": float(resolved.baseline_slippage_bps),
            "stress_slippage_bps": float(resolved.stress_slippage_bps),
            "main_scenario_slippage_bps": float(MAIN_SCENARIO_SLIPPAGE_BPS),
            "control_scenario_slippage_bps": float(CONTROL_SCENARIO_SLIPPAGE_BPS),
        },
        capacity_execution={
            "futures_fallback_cost": dict(FUTURES_FALLBACK_COST),
            "schedule_clock": str(resolved.schedule_clock),
        },
        multiple_testing={
            "mode": "formal_runtime",
            "white_reality_check_enabled": True,
            "hansen_spa_enabled": True,
            "pbo_enabled": True,
        },
        admission_thresholds={
            "validation_profile": {
                "profile": "trade_rule_validation",
                "validation_focus": "target_plus_representative",
                "primary_validation_layer": "target",
            },
            "business_admission_gate": {
                "benchmark_return_multiple_min": 2.0,
                "benchmark_drawdown_mode": "lte",
                "cost_sensitivity_required_bps": [0.0, 5.0, 10.0],
                "cash_sleeve_required": True,
            },
            "take_profit_grid": list(DEFAULT_TAKE_PROFIT_GRID),
            "rotation_lookback_months": list(DEFAULT_ROTATION_LOOKBACK_MONTHS),
            "rotation_trend_windows": list(DEFAULT_ROTATION_TREND_WINDOWS),
            "leverage_grid": list(DEFAULT_LEVERAGE_GRID),
        },
        family_holding_bucket={
            "family": "510300_default",
            "holding_bucket": "medium",
            "enable_enhancements": bool(resolved.enable_enhancements),
        },
        field_provenance={
            "walk_forward_config": "derived",
            "baseline_reference": "derived",
            "cash_sleeve_policy": "derived",
            "cost_sensitivity_grid": "derived",
            "capacity_execution": "derived",
            "multiple_testing": "derived",
            "admission_thresholds": "derived",
            "family_holding_bucket": "derived",
        },
    )


class _ProxyBypass:
    def __enter__(self):
        self._backup = {
            "HTTP_PROXY": os.getenv("HTTP_PROXY"),
            "HTTPS_PROXY": os.getenv("HTTPS_PROXY"),
            "ALL_PROXY": os.getenv("ALL_PROXY"),
            "http_proxy": os.getenv("http_proxy"),
            "https_proxy": os.getenv("https_proxy"),
            "all_proxy": os.getenv("all_proxy"),
            "NO_PROXY": os.getenv("NO_PROXY"),
            "no_proxy": os.getenv("no_proxy"),
        }
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(key, None)
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self._backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _ccy(value: float) -> str:
    return f"{value:,.2f}"


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value).date())
    if isinstance(value, np.generic):
        return value.item()
    if dataclass_is_instance(value):
        return asdict(value)
    raise TypeError(f"unsupported type: {type(value)!r}")


def dataclass_is_instance(value: Any) -> bool:
    return hasattr(value, "__dataclass_fields__")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


@lru_cache(maxsize=1)
def _load_legacy_module():
    spec = importlib.util.spec_from_file_location("backtest_510300_etf_dca_legacy", LEGACY_SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load legacy module from {LEGACY_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _load_fund_universe() -> pd.DataFrame:
    """加载基金列表（Tushare Pro fund_basic）。"""
    from ...data_source import data_source
    pro = data_source.get_tushare_pro()
    if pro is None:
        return pd.DataFrame(columns=["基金代码", "基金简称", "基金类型"])
    try:
        df = pro.fund_basic(market='E', fields='ts_code,name,fund_type')
        if df is None or df.empty:
            return pd.DataFrame(columns=["基金代码", "基金简称", "基金类型"])
        df["基金代码"] = df["ts_code"].str.split(".").str[0].str.zfill(6)
        df["基金简称"] = df["name"].astype(str)
        df["基金类型"] = df["fund_type"].astype(str)
        return df[["基金代码", "基金简称", "基金类型"]].copy()
    except Exception:
        return pd.DataFrame(columns=["基金代码", "基金简称", "基金类型"])


def _normalize_symbol(code: str) -> str:
    normalized = str(code or "").strip()
    if normalized.startswith(("sh", "sz")):
        return normalized
    if normalized.startswith(("5", "6", "9")):
        return f"sh{normalized}"
    return f"sz{normalized}"


def _normalize_price_frame(frame: pd.DataFrame, *, end_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    mapping = {
        "日期": "date",
        "date": "date",
        "开盘价": "open",
        "open": "open",
        "最高价": "high",
        "high": "high",
        "最低价": "low",
        "low": "low",
        "收盘价": "close",
        "close": "close",
        "成交量": "volume",
        "volume": "volume",
        "成交额": "amount",
        "amount": "amount",
    }
    normalized = frame.rename(columns=mapping).copy()
    normalized["date"] = pd.to_datetime(normalized["date"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in normalized:
            normalized[column] = 0.0
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.loc[normalized["date"] <= pd.Timestamp(end_date)]
    normalized = normalized.loc[:, ["date", "open", "high", "low", "close", "volume", "amount"]]
    normalized = normalized.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if normalized["amount"].eq(0).all():
        normalized["amount"] = normalized["close"].fillna(0.0) * normalized["volume"].fillna(0.0)
    return normalized


def _normalize_dividend_frame(frame: pd.DataFrame, *, end_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["ex_date", "per_share_dividend"])
    normalized = frame.copy()
    if "日期" in normalized.columns:
        normalized["ex_date"] = pd.to_datetime(normalized["日期"])
    elif "ex_date" in normalized.columns:
        normalized["ex_date"] = pd.to_datetime(normalized["ex_date"])
    else:
        return pd.DataFrame(columns=["ex_date", "per_share_dividend"])
    dividend_column = "累计分红" if "累计分红" in normalized.columns else "per_share_dividend"
    normalized = normalized.loc[normalized["ex_date"] <= pd.Timestamp(end_date), ["ex_date", dividend_column]].copy()
    normalized[dividend_column] = pd.to_numeric(normalized[dividend_column], errors="coerce").fillna(0.0)
    if dividend_column == "累计分红":
        normalized["per_share_dividend"] = normalized[dividend_column].diff().fillna(normalized[dividend_column])
    else:
        normalized["per_share_dividend"] = normalized[dividend_column]
    return normalized.loc[:, ["ex_date", "per_share_dividend"]].sort_values("ex_date").reset_index(drop=True)


def _fetch_etf_history(code: str, *, end_date: str) -> pd.DataFrame:
    """获取 ETF 历史数据（Tushare Pro fund_daily）。"""
    from ...data_source import data_source
    pro = data_source.get_tushare_pro()
    if pro is None:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    try:
        ts_code = f"{code}.SH" if code.startswith(("5", "6")) else f"{code}.SZ"
        df = pro.fund_daily(ts_code=ts_code, start_date='20100101', end_date=end_date.replace('-', ''))
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df = df.rename(columns={
            "trade_date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "amount",
        })
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        df = df.loc[df["date"] <= pd.Timestamp(end_date)]
        return df[["date", "open", "high", "low", "close", "volume", "amount"]]
    except Exception:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])


def _fetch_etf_dividends(code: str, *, end_date: str) -> pd.DataFrame:
    """获取 ETF 分红数据（Tushare Pro fund_div）。"""
    from ...data_source import data_source
    pro = data_source.get_tushare_pro()
    if pro is None:
        return pd.DataFrame(columns=["ex_date", "per_share_dividend"])
    try:
        ts_code = f"{code}.SH" if code.startswith(("5", "6")) else f"{code}.SZ"
        df = pro.fund_div(ts_code=ts_code, fields='ts_code,ex_date,per_share_div_after_tax')
        if df is None or df.empty:
            return pd.DataFrame(columns=["ex_date", "per_share_dividend"])
        df["ex_date"] = pd.to_datetime(df["ex_date"], format="%Y%m%d")
        df["per_share_dividend"] = pd.to_numeric(df["per_share_div_after_tax"], errors="coerce").fillna(0.0)
        df = df.loc[df["ex_date"] <= pd.Timestamp(end_date)]
        return df[["ex_date", "per_share_dividend"]].sort_values("ex_date").reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=["ex_date", "per_share_dividend"])


def _fetch_futures_history(symbol: str, *, end_date: str) -> pd.DataFrame:
    """获取期货主力合约历史数据（Tushare Pro fut_daily）。"""
    from ...data_source import data_source
    pro = data_source.get_tushare_pro()
    if pro is None:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    try:
        # 尝试获取期货日线
        df = pro.fut_daily(ts_code=symbol, start_date='20100101', end_date=end_date.replace('-', ''))
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df = df.rename(columns={
            "trade_date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "amount",
        })
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            else:
                df[col] = 0.0
        df = df.sort_values("date").reset_index(drop=True)
        df = df.loc[df["date"] <= pd.Timestamp(end_date)]
        return df[["date", "open", "high", "low", "close", "volume", "amount"]]
    except Exception:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])


def _median_amount_60(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    window = frame.tail(60)
    return float(pd.to_numeric(window["amount"], errors="coerce").fillna(0.0).median())


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, float, str]:
    return (
        -_safe_float(candidate.get("history_rows")),
        -_safe_float(candidate.get("median_amount_60d")),
        str(candidate.get("code") or ""),
    )

# --- research_510300_v3_parts/feature_engineering.py ---
def _fund_candidates_for_keywords(keywords: Sequence[str], code_hints: Sequence[str]) -> list[dict[str, Any]]:
    universe = _load_fund_universe()
    pattern = "|".join(keywords)
    matched = universe[
        universe["基金简称"].str.contains(pattern, na=False)
        | universe["基金代码"].isin([str(code).zfill(6) for code in code_hints])
    ].copy()
    matched = matched.loc[
        matched["基金代码"].str.match(r"^[15]\d{5}$", na=False)
        | matched["基金代码"].isin([str(code).zfill(6) for code in code_hints])
    ]
    rows: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for row in matched.itertuples(index=False):
        code = str(getattr(row, "基金代码", "")).zfill(6)
        if code in seen_codes:
            continue
        seen_codes.add(code)
        rows.append(
            {
                "code": code,
                "name": str(getattr(row, "基金简称", code)),
                "fund_type": str(getattr(row, "基金类型", "")),
            }
        )
    return rows


def resolve_etf_instrument(
    *,
    category: str,
    keyword_group: str,
    keywords: Sequence[str],
    code_hints: Sequence[str],
    end_date: str,
    locked_code: str | None = None,
) -> tuple[ResolvedInstrument, pd.DataFrame, pd.DataFrame]:
    attempted: list[dict[str, Any]] = []
    resolved_history = pd.DataFrame()
    resolved_dividends = pd.DataFrame()
    selected: dict[str, Any] | None = None
    failures: list[str] = []
    if locked_code:
        normalized_code = str(locked_code).zfill(6)
        universe = _load_fund_universe()
        matched = universe.loc[universe["基金代码"] == normalized_code]
        if matched.empty:
            candidates = [{"code": normalized_code, "name": normalized_code, "fund_type": "unknown"}]
        else:
            row = matched.iloc[0]
            candidates = [
                {
                    "code": normalized_code,
                    "name": str(row.get("基金简称", normalized_code)),
                    "fund_type": str(row.get("基金类型", "")),
                }
            ]
    else:
        candidates = _fund_candidates_for_keywords(keywords, code_hints)
    for candidate in candidates:
        code = candidate["code"]
        try:
            history = _fetch_etf_history(code, end_date=end_date)
        except Exception as exc:
            attempted.append({**candidate, "status": "history_error", "reason": str(exc)})
            failures.append(f"{code}:history_error")
            continue
        if len(history) < 60:
            attempted.append({**candidate, "status": "insufficient_history", "history_rows": int(len(history))})
            failures.append(f"{code}:insufficient_history")
            continue
        dividends = _fetch_etf_dividends(code, end_date=end_date)
        record = {
            **candidate,
            "symbol": _normalize_symbol(code),
            "status": "ok",
            "history_rows": int(len(history)),
            "first_trade_date": history["date"].iloc[0].strftime("%Y-%m-%d"),
            "last_trade_date": history["date"].iloc[-1].strftime("%Y-%m-%d"),
            "median_amount_60d": _median_amount_60(history),
            "dividend_rows": int(len(dividends)),
        }
        attempted.append(record)
        if selected is None or _candidate_sort_key(record) < _candidate_sort_key(selected):
            selected = record
            resolved_history = history
            resolved_dividends = dividends
    if selected is None:
        unresolved = ResolvedInstrument(
            category=category,
            keyword_group=keyword_group,
            code="",
            symbol="",
            name="",
            source="fund_name_em",
            history_rows=0,
            first_trade_date="",
            last_trade_date="",
            median_amount_60d=0.0,
            candidates=attempted,
            failure_reason=";".join(failures) or "no_candidate",
        )
        return unresolved, resolved_history, resolved_dividends
    resolved = ResolvedInstrument(
        category=category,
        keyword_group=keyword_group,
        code=str(selected["code"]),
        symbol=str(selected["symbol"]),
        name=str(selected["name"]),
        source="locked_code+fund_etf_hist_sina" if locked_code else "fund_name_em+fund_etf_hist_sina",
        history_rows=int(selected["history_rows"]),
        first_trade_date=str(selected["first_trade_date"]),
        last_trade_date=str(selected["last_trade_date"]),
        median_amount_60d=float(selected["median_amount_60d"]),
        candidates=attempted,
    )
    return resolved, resolved_history, resolved_dividends


def resolve_if_futures(end_date: str) -> tuple[ResolvedInstrument, pd.DataFrame, dict[str, Any]]:
    attempted: list[dict[str, Any]] = []
    fee_info = dict(FUTURES_FALLBACK_COST)
    fee_info["fallback_triggered"] = True
    try:
        history = _fetch_futures_history("IF0", end_date=end_date)
        attempted.append(
            {
                "symbol": "IF0",
                "status": "ok" if not history.empty else "empty",
                "history_rows": int(len(history)),
                "first_trade_date": history["date"].iloc[0].strftime("%Y-%m-%d") if not history.empty else "",
                "last_trade_date": history["date"].iloc[-1].strftime("%Y-%m-%d") if not history.empty else "",
            }
        )
    except Exception as exc:
        history = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        attempted.append({"symbol": "IF0", "status": "history_error", "reason": str(exc)})
    try:
        with _ProxyBypass():
            fees = ak.futures_fees_info()
        filtered = fees.loc[fees["品种代码"].astype(str) == "IF"].copy()
        if not filtered.empty:
            row = filtered.iloc[0]
            fee_info = {
                "slippage_bps": FUTURES_FALLBACK_COST["slippage_bps"],
                "fixed_fee_per_contract": _safe_float(row.get("1手开仓费用"), FUTURES_FALLBACK_COST["fixed_fee_per_contract"]),
                "margin_rate": _safe_float(row.get("做多保证金率"), FUTURES_FALLBACK_COST["margin_rate"]),
                "contract_multiplier": _safe_int(row.get("合约乘数"), FUTURES_FALLBACK_COST["contract_multiplier"]),
                "fallback_triggered": False,
            }
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/research_510300_v3.py:695: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    resolved = ResolvedInstrument(
        category="futures",
        keyword_group="IF0",
        code="IF0",
        symbol="IF0",
        name="沪深300股指期货连续主力",
        source="futures_main_sina",
        history_rows=int(len(history)),
        first_trade_date=history["date"].iloc[0].strftime("%Y-%m-%d") if not history.empty else "",
        last_trade_date=history["date"].iloc[-1].strftime("%Y-%m-%d") if not history.empty else "",
        median_amount_60d=_median_amount_60(history),
        candidates=attempted,
        failure_reason=None if not history.empty else "missing_futures_history",
    )
    return resolved, history, fee_info


def required_summary_fields() -> set[str]:
    return {
        "research_protocol",
        "instrument_resolution",
        "cost_scenarios",
        "oos_folds",
        "cash_sleeve_results",
        "enhancement_results",
        "selection_gate",
        "final_recommendation",
    }


def build_monthly_windows(
    trade_dates: Iterable[pd.Timestamp] | pd.Index | pd.Series,
    *,
    train_months: int,
    test_months: int,
    step_months: int,
) -> list[dict[str, pd.Timestamp]]:
    trade_index = pd.Index(pd.to_datetime(list(trade_dates)))
    if trade_index.empty:
        return []
    frame = pd.DataFrame({"date": trade_index}).sort_values("date")
    frame["month"] = frame["date"].dt.to_period("M")
    grouped = frame.groupby("month")["date"]
    month_first = grouped.min().tolist()
    month_last = grouped.max().tolist()
    windows: list[dict[str, pd.Timestamp]] = []
    cursor = train_months
    while cursor + test_months <= len(month_last):
        train_start_idx = cursor - train_months
        train_end_idx = cursor - 1
        test_start_idx = cursor
        test_end_idx = cursor + test_months - 1
        windows.append(
            {
                "train_start": pd.Timestamp(month_first[train_start_idx]),
                "train_end": pd.Timestamp(month_last[train_end_idx]),
                "test_start": pd.Timestamp(month_first[test_start_idx]),
                "test_end": pd.Timestamp(month_last[test_end_idx]),
            }
        )
        cursor += step_months
    return windows


def _slice_frame(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame.loc[(frame["date"] >= start) & (frame["date"] <= end)].copy().reset_index(drop=True)


def _slice_dividends(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame.loc[(frame["ex_date"] >= start) & (frame["ex_date"] <= end)].copy().reset_index(drop=True)


def _legacy_assumptions(slippage_bps: float) -> FactoryBacktestAssumptions:
    return FactoryBacktestAssumptions(
        commission_rate=0.00025,
        slippage_bps=float(slippage_bps),
        slippage_model="fixed",
        min_trade_lot=100,
        sell_tax_rate=0.0,
        market_ruleset="cn_equity",
    )


def _metrics_rank_key(metrics: Mapping[str, Any]) -> tuple[float, float, float]:
    return (
        -_safe_float(metrics.get("cagr")),
        _safe_float(metrics.get("max_drawdown")),
        _safe_float(metrics.get("average_exposure")),
    )


def _daily_returns_from_nav(curve: pd.DataFrame) -> pd.Series:
    if curve.empty or "tw_nav" not in curve:
        return pd.Series(dtype=float)
    nav = pd.to_numeric(curve["tw_nav"], errors="coerce").ffill().fillna(1.0)
    returns = nav.pct_change().fillna(0.0)
    returns.index = pd.to_datetime(curve["date"])
    return returns


def _summarize_nav_curve(curve: pd.DataFrame) -> dict[str, Any]:
    if curve.empty or "tw_nav" not in curve:
        return {
            "cagr": 0.0,
            "max_drawdown": 0.0,
            "average_exposure": 0.0,
            "total_return": 0.0,
            "final_total_asset": 0.0,
        }
    nav = pd.to_numeric(curve["tw_nav"], errors="coerce").fillna(1.0)
    running_max = nav.cummax()
    drawdown = (nav / running_max - 1.0).min() if not nav.empty else 0.0
    start_date = pd.Timestamp(curve["date"].iloc[0])
    end_date = pd.Timestamp(curve["date"].iloc[-1])
    years = max((end_date - start_date).days / 365.2425, 0.0)
    final_nav = float(nav.iloc[-1]) if not nav.empty else 1.0
    return {
        "cagr": float(final_nav ** (1.0 / years) - 1.0) if years > 0 and final_nav > 0 else 0.0,
        "max_drawdown": abs(float(drawdown)) if nav.size else 0.0,
        "average_exposure": float(pd.to_numeric(curve.get("exposure", 0.0), errors="coerce").fillna(0.0).mean()),
        "total_return": float(final_nav - 1.0),
        "final_total_asset": float(pd.to_numeric(curve.get("total_asset", 0.0), errors="coerce").fillna(0.0).iloc[-1]) if "total_asset" in curve else 0.0,
    }


def _chain_oos_curves(curves: Sequence[pd.DataFrame]) -> pd.DataFrame:
    if not curves:
        return pd.DataFrame(columns=["date", "tw_nav", "exposure", "total_asset"])
    nav_anchor = 1.0
    combined: list[pd.DataFrame] = []
    for idx, curve in enumerate(curves):
        if curve.empty:
            continue
        segment = curve.copy()
        segment["date"] = pd.to_datetime(segment["date"])
        segment["tw_nav"] = pd.to_numeric(segment["tw_nav"], errors="coerce").fillna(1.0) * nav_anchor
        nav_anchor = float(segment["tw_nav"].iloc[-1])
        if idx > 0:
            segment = segment.iloc[1:].copy()
        combined.append(segment)
    if not combined:
        return pd.DataFrame(columns=["date", "tw_nav", "exposure", "total_asset"])
    return pd.concat(combined, ignore_index=True)


def _serialize_strategy(strategy: Any) -> StrategyArtifact:
    metrics = asdict(strategy.metrics) if dataclass_is_instance(strategy.metrics) else dict(strategy.metrics)
    return StrategyArtifact(
        name=str(strategy.name),
        metrics=metrics,
        equity_curve=strategy.equity_curve.copy(),
        trades=strategy.trades.copy(),
        daily_returns=_daily_returns_from_nav(strategy.equity_curve),
        extra=dict(strategy.extra or {}),
    )


def run_legacy_core_suite(
    price_df: pd.DataFrame,
    dividend_df: pd.DataFrame,
    *,
    slippage_bps: float,
    scheme2_take_profit_grid: Sequence[float] = DEFAULT_TAKE_PROFIT_GRID,
) -> tuple[dict[str, StrategyArtifact], list[dict[str, Any]]]:
    legacy = _load_legacy_module()
    assumptions = _legacy_assumptions(slippage_bps)
    _trade_dates, monthly_schedule, next_trade_after_ex = legacy.build_trading_calendar(price_df, dividend_df)
    indicators = legacy.build_indicator_frame(price_df)

    scheme1 = legacy.simulate_monthly_dca(
        price_df,
        dividend_df,
        monthly_schedule,
        next_trade_after_ex,
        assumptions,
        name="scheme1",
        description="legacy frozen baseline: monthly DCA",
        fixed_external_injection=True,
        take_profit_pct=None,
    )
    scheme2_candidates: list[StrategyArtifact] = []
    for take_profit_pct in scheme2_take_profit_grid:
        candidate = legacy.simulate_monthly_dca(
            price_df,
            dividend_df,
            monthly_schedule,
            next_trade_after_ex,
            assumptions,
            name="scheme2",
            description="legacy frozen scheme2 with variable take-profit",
            fixed_external_injection=False,
            take_profit_pct=float(take_profit_pct),
        )
        serialized = _serialize_strategy(candidate)
        serialized.extra["take_profit_pct"] = float(take_profit_pct)
        scheme2_candidates.append(serialized)
    scheme2_candidates.sort(key=lambda item: _metrics_rank_key(item.metrics))
    scheme2_best = scheme2_candidates[0]

    optimized_candidates = legacy.search_optimization_candidates(
        price_df,
        dividend_df,
        monthly_schedule,
        next_trade_after_ex,
        assumptions,
        indicators,
    )
    optimized_payloads = [
        {
            "ma_window": candidate.ma_window,
            "rsi_floor": candidate.rsi_floor,
            "rsi_cap": candidate.rsi_cap,
            "sell_rsi": candidate.sell_rsi,
            "use_slope": candidate.use_slope,
            "metrics": asdict(candidate.metrics),
        }
        for candidate in optimized_candidates
    ]
    best_candidate = optimized_candidates[0]
    optimized = legacy.simulate_regime_strategy(
        price_df,
        dividend_df,
        monthly_schedule,
        next_trade_after_ex,
        assumptions,
        indicators,
        ma_window=best_candidate.ma_window,
        rsi_floor=best_candidate.rsi_floor,
        rsi_cap=best_candidate.rsi_cap,
        sell_rsi=best_candidate.sell_rsi,
        use_slope=best_candidate.use_slope,
    )
    optimized_serialized = _serialize_strategy(optimized)
    optimized_serialized.extra.update(
        {
            "ma_window": int(best_candidate.ma_window),
            "rsi_floor": int(best_candidate.rsi_floor),
            "rsi_cap": int(best_candidate.rsi_cap),
            "sell_rsi": int(best_candidate.sell_rsi),
            "use_slope": bool(best_candidate.use_slope),
        }
    )
    return {
        "scheme1": _serialize_strategy(scheme1),
        "scheme2": scheme2_best,
        "optimized_regime": optimized_serialized,
    }, optimized_payloads


def _empty_artifact(name: str, *, extra: Optional[dict[str, Any]] = None) -> StrategyArtifact:
    empty_curve = pd.DataFrame(columns=["date", "total_asset", "exposure", "tw_nav"])
    return StrategyArtifact(
        name=name,
        metrics=_summarize_nav_curve(empty_curve),
        equity_curve=empty_curve,
        trades=pd.DataFrame(columns=["date", "side", "price", "cash_amount", "reason"]),
        daily_returns=pd.Series(dtype=float),
        extra=dict(extra or {}),
    )


def _build_monthly_execution_dates(calendar: pd.Series | pd.Index | Iterable[pd.Timestamp]) -> list[pd.Timestamp]:
    trade_index = pd.Index(pd.to_datetime(list(calendar))).sort_values()
    if trade_index.empty:
        return []
    frame = pd.DataFrame({"date": trade_index})
    month_ends = frame.groupby(frame["date"].dt.to_period("M"))["date"].max().tolist()
    execution_dates: list[pd.Timestamp] = []
    for month_end in month_ends:
        idx = trade_index.searchsorted(pd.Timestamp(month_end))
        if idx + 1 < len(trade_index):
            execution_dates.append(pd.Timestamp(trade_index[idx + 1]))
        else:
            execution_dates.append(pd.Timestamp(month_end))
    return execution_dates


def _round_down_lot(shares: float, lot_size: int = 100) -> int:
    return int(math.floor(float(shares) / float(lot_size)) * lot_size)


def _trade_price(frame: pd.DataFrame, date: pd.Timestamp, column: str) -> float | None:
    row = frame.loc[frame["date"] == date]
    if row.empty:
        return None
    value = _safe_float(row.iloc[0].get(column), np.nan)
    return None if not np.isfinite(value) or value <= 0 else float(value)


def _monthly_score(
    frame: pd.DataFrame,
    signal_date: pd.Timestamp,
    *,
    lookback_months: int,
    trend_window: int,
) -> float | None:
    current_rows = frame.loc[frame["date"] <= signal_date].copy()
    if current_rows.empty:
        return None
    current_rows = current_rows.sort_values("date").reset_index(drop=True)
    if len(current_rows) <= trend_window:
        return None
    lookback_days = max(21, int(lookback_months * 21))
    if len(current_rows) <= lookback_days:
        return None
    closes = pd.to_numeric(current_rows["close"], errors="coerce").fillna(0.0)
    current_close = float(closes.iloc[-1])
    prior_close = float(closes.iloc[-lookback_days])
    trend_ma = float(closes.rolling(trend_window).mean().iloc[-1])
    if prior_close <= 0 or trend_ma <= 0 or current_close <= 0:
        return None
    if current_close <= trend_ma:
        return None
    return current_close / prior_close - 1.0


def _build_strategy_artifact_from_rows(
    *,
    name: str,
    equity_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
) -> StrategyArtifact:
    equity_curve = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trade_rows)
    if not equity_curve.empty:
        equity_curve["date"] = pd.to_datetime(equity_curve["date"])
    if not trades.empty:
        trades["date"] = pd.to_datetime(trades["date"])
    return StrategyArtifact(
        name=name,
        metrics=_summarize_nav_curve(equity_curve),
        equity_curve=equity_curve,
        trades=trades,
        daily_returns=_daily_returns_from_nav(equity_curve),
        extra=dict(extra or {}),
    )

# --- research_510300_v3_parts/evaluation.py ---
def simulate_monthly_rotation_family(
    *,
    name: str,
    base_calendar: pd.DataFrame,
    instrument_frames: dict[str, pd.DataFrame],
    lookback_months: int,
    trend_window: int,
    assumptions: FactoryBacktestAssumptions,
) -> StrategyArtifact:
    if base_calendar.empty or not instrument_frames:
        return _empty_artifact(name, extra={"lookback_months": lookback_months, "trend_window": trend_window})
    calendar = base_calendar["date"].drop_duplicates().sort_values().reset_index(drop=True)
    execution_dates = set(_build_monthly_execution_dates(calendar))
    signal_dates = set(pd.to_datetime(base_calendar.groupby(base_calendar["date"].dt.to_period("M"))["date"].max().tolist()))
    next_trade_map = {
        pd.Timestamp(calendar.iloc[idx]): pd.Timestamp(calendar.iloc[idx + 1]) if idx + 1 < len(calendar) else pd.Timestamp(calendar.iloc[idx])
        for idx in range(len(calendar))
    }
    target_by_exec: dict[pd.Timestamp, str | None] = {}
    for signal_date in sorted(signal_dates):
        scored: list[tuple[float, str]] = []
        for code, frame in instrument_frames.items():
            score = _monthly_score(
                frame,
                signal_date,
                lookback_months=lookback_months,
                trend_window=trend_window,
            )
            if score is None:
                continue
            scored.append((score, code))
        scored.sort(key=lambda item: (-item[0], item[1]))
        target = scored[0][1] if scored else None
        target_by_exec[next_trade_map[pd.Timestamp(signal_date)]] = target

    cash = 0.0
    current_code: str | None = None
    current_shares = 0
    prev_total_asset: float | None = None
    tw_nav = 1.0
    equity_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    for trade_date in calendar:
        trade_date = pd.Timestamp(trade_date)
        external_flow = 0.0
        if trade_date in execution_dates:
            cash += MONTHLY_CONTRIBUTION
            external_flow += MONTHLY_CONTRIBUTION
        target_code = target_by_exec.get(trade_date, current_code)
        if target_code != current_code:
            if current_code and current_shares > 0:
                current_frame = instrument_frames[current_code]
                sell_open = _trade_price(current_frame, trade_date, "open") or _trade_price(current_frame, trade_date, "close")
                if sell_open:
                    revenue = current_shares * sell_open * (1.0 - assumptions.commission_rate - assumptions.slippage_bps / 10000.0)
                    cash += revenue
                    trade_rows.append(
                        {
                            "date": trade_date,
                            "side": "sell",
                            "asset_code": current_code,
                            "shares": current_shares,
                            "price": sell_open,
                            "cash_amount": revenue,
                            "reason": "monthly_rotation",
                        }
                    )
                    current_shares = 0
                    current_code = None
            if target_code:
                target_frame = instrument_frames[target_code]
                buy_open = _trade_price(target_frame, trade_date, "open")
                if buy_open:
                    cost_per_share = buy_open * (1.0 + assumptions.commission_rate + assumptions.slippage_bps / 10000.0)
                    shares = _round_down_lot(cash / cost_per_share, assumptions.min_trade_lot)
                    if shares > 0:
                        total_cost = shares * buy_open * (1.0 + assumptions.commission_rate + assumptions.slippage_bps / 10000.0)
                        cash -= total_cost
                        current_code = target_code
                        current_shares = shares
                        trade_rows.append(
                            {
                                "date": trade_date,
                                "side": "buy",
                                "asset_code": target_code,
                                "shares": shares,
                                "price": buy_open,
                                "cash_amount": total_cost,
                                "reason": "monthly_rotation",
                            }
                        )
        market_value = 0.0
        if current_code and current_shares > 0:
            current_frame = instrument_frames[current_code]
            close_price = _trade_price(current_frame, trade_date, "close")
            if close_price:
                market_value = current_shares * close_price
        total_asset = cash + market_value
        exposure = market_value / total_asset if total_asset > 0 else 0.0
        if prev_total_asset and prev_total_asset > 0:
            tw_nav *= max((total_asset - external_flow) / prev_total_asset, 0.0)
        prev_total_asset = total_asset
        equity_rows.append(
            {
                "date": trade_date,
                "total_asset": total_asset,
                "market_value": market_value,
                "cash_pool": cash,
                "external_flow": external_flow,
                "exposure": exposure,
                "tw_nav": tw_nav,
                "selected_asset": current_code or "",
            }
        )
    return _build_strategy_artifact_from_rows(
        name=name,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        extra={"lookback_months": lookback_months, "trend_window": trend_window},
    )


def simulate_leveraged_family(
    *,
    name: str,
    price_df: pd.DataFrame,
    strong_exposure: float,
    weak_exposure: float,
    assumptions: FactoryBacktestAssumptions,
    financing_rate_daily: float = 0.04 / 252.0,
) -> StrategyArtifact:
    if price_df.empty or len(price_df) < 160:
        return _empty_artifact(name, extra={"strong_exposure": strong_exposure, "weak_exposure": weak_exposure})
    frame = price_df.copy().reset_index(drop=True)
    frame["ma150"] = frame["close"].rolling(150).mean()
    frame["ma150_slope"] = frame["ma150"].diff(20)
    execution_dates = set(_build_monthly_execution_dates(frame["date"]))
    cash = 0.0
    equity_value = 0.0
    exposure = 0.0
    prev_total_asset: float | None = None
    tw_nav = 1.0
    trade_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(frame.itertuples(index=False)):
        trade_date = pd.Timestamp(row.date)
        external_flow = 0.0
        if trade_date in execution_dates:
            cash += MONTHLY_CONTRIBUTION
            equity_value += MONTHLY_CONTRIBUTION
            external_flow += MONTHLY_CONTRIBUTION
        prior_signal = weak_exposure
        if idx > 0:
            prev = frame.iloc[idx - 1]
            if np.isfinite(prev["ma150"]) and np.isfinite(prev["ma150_slope"]) and prev["close"] > prev["ma150"] and prev["ma150_slope"] > 0:
                prior_signal = strong_exposure
        if abs(prior_signal - exposure) > 1e-9:
            turnover = abs(prior_signal - exposure)
            trade_cost = turnover * max(equity_value, 0.0) * (assumptions.commission_rate + assumptions.slippage_bps / 10000.0)
            equity_value = max(equity_value - trade_cost, 0.0)
            trade_rows.append(
                {
                    "date": trade_date,
                    "side": "rebalance",
                    "price": float(row.open),
                    "cash_amount": trade_cost,
                    "reason": "signal_shift",
                    "target_exposure": prior_signal,
                }
            )
            exposure = prior_signal
        open_price = _safe_float(row.open)
        close_price = _safe_float(row.close)
        intraday_return = (close_price / open_price - 1.0) if open_price > 0 else 0.0
        financing_cost = max(exposure - 1.0, 0.0) * financing_rate_daily
        equity_value = max(equity_value * (1.0 + exposure * intraday_return - financing_cost), 0.0)
        total_asset = equity_value
        if prev_total_asset and prev_total_asset > 0:
            tw_nav *= max((total_asset - external_flow) / prev_total_asset, 0.0)
        prev_total_asset = total_asset
        equity_rows.append(
            {
                "date": trade_date,
                "total_asset": total_asset,
                "market_value": total_asset * exposure,
                "cash_pool": total_asset * max(1.0 - exposure, 0.0),
                "external_flow": external_flow,
                "exposure": exposure,
                "tw_nav": tw_nav,
            }
        )
    return _build_strategy_artifact_from_rows(
        name=name,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        extra={"strong_exposure": strong_exposure, "weak_exposure": weak_exposure},
    )


def simulate_futures_family(
    *,
    name: str,
    futures_df: pd.DataFrame,
    strong_exposure: float,
    weak_exposure: float,
    fee_info: Mapping[str, Any],
) -> StrategyArtifact:
    if futures_df.empty or len(futures_df) < 160:
        return _empty_artifact(name, extra={"strong_exposure": strong_exposure, "weak_exposure": weak_exposure})
    frame = futures_df.copy().reset_index(drop=True)
    frame["ma150"] = frame["close"].rolling(150).mean()
    frame["ma150_slope"] = frame["ma150"].diff(20)
    execution_dates = set(_build_monthly_execution_dates(frame["date"]))
    cash = 0.0
    contracts = 0
    multiplier = _safe_int(fee_info.get("contract_multiplier"), FUTURES_FALLBACK_COST["contract_multiplier"])
    margin_rate = _safe_float(fee_info.get("margin_rate"), FUTURES_FALLBACK_COST["margin_rate"])
    fixed_fee = _safe_float(fee_info.get("fixed_fee_per_contract"), FUTURES_FALLBACK_COST["fixed_fee_per_contract"])
    slippage_bps = _safe_float(fee_info.get("slippage_bps"), FUTURES_FALLBACK_COST["slippage_bps"])
    prev_total_asset: float | None = None
    tw_nav = 1.0
    trade_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(frame.itertuples(index=False)):
        trade_date = pd.Timestamp(row.date)
        external_flow = 0.0
        if trade_date in execution_dates:
            cash += MONTHLY_CONTRIBUTION
            external_flow += MONTHLY_CONTRIBUTION
        target_exposure = weak_exposure
        if idx > 0:
            prev = frame.iloc[idx - 1]
            if np.isfinite(prev["ma150"]) and np.isfinite(prev["ma150_slope"]) and prev["close"] > prev["ma150"] and prev["ma150_slope"] > 0:
                target_exposure = strong_exposure
        open_price = _safe_float(row.open)
        notional_per_contract = open_price * multiplier if open_price > 0 else 0.0
        desired_contracts = 0
        if notional_per_contract > 0 and cash > 0:
            max_by_notional = math.floor(target_exposure * cash / notional_per_contract)
            max_by_margin = math.floor(cash / max(notional_per_contract * margin_rate, 1.0))
            desired_contracts = max(0, min(max_by_notional, max_by_margin))
        delta = desired_contracts - contracts
        if delta != 0:
            fee_paid = abs(delta) * fixed_fee
            slippage_paid = abs(delta) * notional_per_contract * (slippage_bps / 10000.0)
            cash -= fee_paid + slippage_paid
            trade_rows.append(
                {
                    "date": trade_date,
                    "side": "buy" if delta > 0 else "sell",
                    "contracts": abs(delta),
                    "price": open_price,
                    "cash_amount": fee_paid + slippage_paid,
                    "reason": "futures_rebalance",
                }
            )
            contracts = desired_contracts
        close_price = _safe_float(row.close)
        pnl = contracts * multiplier * (close_price - open_price)
        cash += pnl
        total_asset = max(cash, 0.0)
        gross_notional = contracts * close_price * multiplier
        exposure = gross_notional / total_asset if total_asset > 0 else 0.0
        if prev_total_asset and prev_total_asset > 0:
            tw_nav *= max((total_asset - external_flow) / prev_total_asset, 0.0)
        prev_total_asset = total_asset
        equity_rows.append(
            {
                "date": trade_date,
                "total_asset": total_asset,
                "market_value": gross_notional,
                "cash_pool": cash,
                "external_flow": external_flow,
                "exposure": exposure,
                "tw_nav": tw_nav,
                "contracts": contracts,
            }
        )
    return _build_strategy_artifact_from_rows(
        name=name,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        extra={"strong_exposure": strong_exposure, "weak_exposure": weak_exposure, "fee_info": dict(fee_info)},
    )


def simulate_cash_sleeve_scheduler(
    *,
    cash_price_df: pd.DataFrame,
    funding_needs: Mapping[pd.Timestamp, float],
    idle_cash_by_date: Mapping[pd.Timestamp, float] | None = None,
    lot_size: int = 100,
) -> dict[str, Any]:
    if cash_price_df.empty:
        return {
            "listed_before_start": False,
            "pre_listing_idle_days": 0,
            "open_redemption_days": 0,
            "close_rebuild_days": 0,
            "ending_cash": 0.0,
            "ending_shares": 0,
        }
    frame = cash_price_df.copy().sort_values("date").reset_index(drop=True)
    idle_map = {pd.Timestamp(key): _safe_float(value) for key, value in dict(idle_cash_by_date or {}).items()}
    listing_date = pd.Timestamp(frame["date"].iloc[0])
    pre_listing_idle_days = sum(1 for date in idle_map if pd.Timestamp(date) < listing_date)
    cash = sum(value for date, value in idle_map.items() if pd.Timestamp(date) < listing_date)
    shares = 0
    open_redemption_days = 0
    close_rebuild_days = 0
    for row in frame.itertuples(index=False):
        trade_date = pd.Timestamp(row.date)
        cash += idle_map.get(trade_date, 0.0)
        need = _safe_float(funding_needs.get(trade_date), 0.0)
        if need > cash and shares > 0 and _safe_float(row.open) > 0:
            required = need - cash
            redeem_shares = _round_down_lot(required / _safe_float(row.open), lot_size)
            redeem_shares = min(max(redeem_shares, lot_size), shares) if required > 0 else 0
            if redeem_shares > 0:
                cash += redeem_shares * _safe_float(row.open)
                shares -= redeem_shares
                open_redemption_days += 1
        if need > 0:
            cash = max(cash - need, 0.0)
        if cash > 0 and _safe_float(row.close) > 0:
            rebuild_shares = _round_down_lot(cash / _safe_float(row.close), lot_size)
            if rebuild_shares > 0:
                shares += rebuild_shares
                cash -= rebuild_shares * _safe_float(row.close)
                close_rebuild_days += 1
    return {
        "listed_before_start": bool(listing_date <= pd.Timestamp(frame["date"].iloc[0])),
        "pre_listing_idle_days": int(pre_listing_idle_days),
        "open_redemption_days": int(open_redemption_days),
        "close_rebuild_days": int(close_rebuild_days),
        "ending_cash": float(cash),
        "ending_shares": int(shares),
    }


def _build_candidate_validation(candidate_runs: Mapping[str, StrategyArtifact]) -> dict[str, Any]:
    if len(candidate_runs) < 2:
        return {"status": "skipped", "reason": "candidate_count_lt_2"}
    daily_matrix = pd.DataFrame({key: run.daily_returns for key, run in candidate_runs.items()}).fillna(0.0)
    if daily_matrix.empty:
        return {"status": "skipped", "reason": "empty_daily_matrix"}
    monthly_returns = ((daily_matrix + 1.0).groupby(daily_matrix.index.to_period("M")).prod() - 1.0).sort_index()
    score_panel = monthly_returns.rolling(6, min_periods=3).mean()
    factor_panel = score_panel.iloc[:-1].to_numpy(dtype=float)
    return_panel = monthly_returns.shift(-1).iloc[:-1].to_numpy(dtype=float)
    result: dict[str, Any] = {"status": "ok", "candidate_count": len(candidate_runs)}
    if factor_panel.shape[0] >= 72:
        wf = WalkForwardValidator(train_window=60, test_window=12, step=12, min_samples_per_period=2)
        result["walk_forward"] = asdict(wf.validate(factor_panel, return_panel))
    else:
        result["walk_forward"] = {"status": "skipped", "reason": "insufficient_months"}
    if factor_panel.shape[0] >= 24:
        pkf = PurgedKFoldCV(n_folds=min(5, max(2, factor_panel.shape[0] // 12)), purge_gap=1, min_samples_per_period=2)
        result["purged_kfold"] = asdict(pkf.validate(factor_panel, return_panel))
    else:
        result["purged_kfold"] = {"status": "skipped", "reason": "insufficient_months"}
    matrix = daily_matrix.to_numpy(dtype=float)
    result["multiple_testing"] = {
        "pbo": probability_of_backtest_overfitting(matrix, n_splits=min(8, max(2, matrix.shape[0] // 50)), seed=13),
        "white_reality_check": white_reality_check(matrix, n_bootstrap=200, seed=13),
        "hansen_spa": hansen_spa_test(matrix, n_bootstrap=200, seed=13, center="consistent"),
    }
    return result


def resolve_default_instruments(end_date: str) -> tuple[dict[str, ResolvedInstrument], dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, Any]]:
    resolved: dict[str, ResolvedInstrument] = {}
    prices: dict[str, pd.DataFrame] = {}
    dividends: dict[str, pd.DataFrame] = {}
    futures_fee_info: dict[str, Any] = dict(FUTURES_FALLBACK_COST)
    configs = {
        "risk_core": {
            "category": "risk_core",
            "keyword_group": "hs300",
            "keywords": ["沪深300ETF", "沪深300", "300ETF"],
            "code_hints": ["510300", "159919", "510310"],
            "locked_code": "510300",
        },
        "cash_money": {
            "category": "cash_sleeve",
            "keyword_group": "money_etf",
            "keywords": ["货币ETF", "华宝添益", "货币"],
            "code_hints": ["511990", "159001"],
        },
        "cash_short_bond": {
            "category": "cash_sleeve",
            "keyword_group": "short_bond_etf",
            "keywords": ["短融ETF", "短债ETF", "短融", "短债"],
            "code_hints": ["511360"],
        },
        "cash_treasury": {
            "category": "cash_sleeve",
            "keyword_group": "treasury_etf",
            "keywords": ["十年国债ETF", "国债ETF", "政金债ETF", "十年国债", "国债"],
            "code_hints": ["511260", "511010", "511520", "511580"],
        },
        "style_300": {
            "category": "style",
            "keyword_group": "hs300",
            "keywords": ["沪深300ETF", "沪深300", "300ETF"],
            "code_hints": ["510300", "159919"],
            "locked_code": "510300",
        },
        "style_500": {
            "category": "style",
            "keyword_group": "zz500",
            "keywords": ["中证500ETF", "中证500", "500ETF"],
            "code_hints": ["510500", "159922"],
        },
        "style_chinext": {
            "category": "style",
            "keyword_group": "chinext",
            "keywords": ["创业板ETF", "创业板"],
            "code_hints": ["159915", "159949"],
        },
        "style_div_lowvol": {
            "category": "style",
            "keyword_group": "dividend_lowvol",
            "keywords": ["红利低波ETF", "红利低波"],
            "code_hints": ["512890", "515300"],
        },
    }
    cache: dict[str, tuple[ResolvedInstrument, pd.DataFrame, pd.DataFrame]] = {}
    for key, payload in configs.items():
        cache_key = ",".join(payload["code_hints"])
        if cache_key in cache:
            instrument, history, dividend = cache[cache_key]
        else:
            instrument, history, dividend = resolve_etf_instrument(end_date=end_date, **payload)
            cache[cache_key] = (instrument, history, dividend)
        resolved[key] = instrument
        prices[key] = history
        dividends[key] = dividend
    futures_instrument, futures_history, futures_fee_info = resolve_if_futures(end_date)
    resolved["futures_if0"] = futures_instrument
    prices["futures_if0"] = futures_history
    dividends["futures_if0"] = pd.DataFrame(columns=["ex_date", "per_share_dividend"])
    return resolved, prices, dividends, futures_fee_info


def _slice_price_map(price_map: Mapping[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {key: _slice_frame(frame, start, end) for key, frame in price_map.items()}


def _build_family_candidate_runs(
    family: str,
    *,
    price_map: Mapping[str, pd.DataFrame],
    assumptions: FactoryBacktestAssumptions,
    futures_fee_info: Mapping[str, Any],
) -> dict[str, StrategyArtifact]:
    base_calendar = price_map.get("risk_core", pd.DataFrame())
    if family == "family_a":
        instruments = {
            key: frame
            for key, frame in price_map.items()
            if key in {"risk_core", "cash_money", "cash_short_bond", "cash_treasury"} and not frame.empty
        }
        runs: dict[str, StrategyArtifact] = {}
        for lookback_months in DEFAULT_ROTATION_LOOKBACK_MONTHS:
            for trend_window in (150,):
                candidate_id = f"lb{lookback_months}_ma{trend_window}"
                runs[candidate_id] = simulate_monthly_rotation_family(
                    name=f"{family}_{candidate_id}",
                    base_calendar=base_calendar,
                    instrument_frames=instruments,
                    lookback_months=lookback_months,
                    trend_window=trend_window,
                    assumptions=assumptions,
                )
        return runs
    if family == "family_b":
        instruments = {
            key: frame
            for key, frame in price_map.items()
            if key in {"style_300", "style_500", "style_chinext", "style_div_lowvol"} and not frame.empty
        }
        runs = {}
        for lookback_months in DEFAULT_ROTATION_LOOKBACK_MONTHS:
            for trend_window in (100,):
                candidate_id = f"lb{lookback_months}_ma{trend_window}"
                runs[candidate_id] = simulate_monthly_rotation_family(
                    name=f"{family}_{candidate_id}",
                    base_calendar=base_calendar,
                    instrument_frames=instruments,
                    lookback_months=lookback_months,
                    trend_window=trend_window,
                    assumptions=assumptions,
                )
        return runs
    if family == "family_c":
        runs = {}
        price_df = price_map.get("risk_core", pd.DataFrame())
        for strong in DEFAULT_LEVERAGE_GRID:
            for weak in (0.0, 1.0):
                candidate_id = f"strong_{strong:.1f}_weak_{weak:.1f}"
                runs[candidate_id] = simulate_leveraged_family(
                    name=f"{family}_{candidate_id}",
                    price_df=price_df,
                    strong_exposure=strong,
                    weak_exposure=weak,
                    assumptions=assumptions,
                )
        return runs
    if family == "family_d":
        runs = {}
        futures_df = price_map.get("futures_if0", pd.DataFrame())
        for strong in DEFAULT_LEVERAGE_GRID:
            for weak in (0.0, 1.0):
                candidate_id = f"strong_{strong:.1f}_weak_{weak:.1f}"
                runs[candidate_id] = simulate_futures_family(
                    name=f"{family}_{candidate_id}",
                    futures_df=futures_df,
                    strong_exposure=strong,
                    weak_exposure=weak,
                    fee_info=futures_fee_info,
                )
        return runs
    return {}

# --- research_510300_v3_parts/reporting.py ---
def _select_best_candidate(candidate_runs: Mapping[str, StrategyArtifact]) -> tuple[str | None, StrategyArtifact | None]:
    ranked = [
        (candidate_id, run)
        for candidate_id, run in candidate_runs.items()
        if run.equity_curve is not None and not run.equity_curve.empty
    ]
    ranked.sort(key=lambda item: _metrics_rank_key(item[1].metrics))
    if not ranked:
        return None, None
    return ranked[0]


def _candidate_payload(candidate_id: str | None, artifact: StrategyArtifact | None) -> dict[str, Any] | None:
    if not candidate_id or artifact is None:
        return None
    return {
        "candidate_id": candidate_id,
        "name": artifact.name,
        "metrics": dict(artifact.metrics),
        "params": dict(artifact.extra),
    }


def _main_summary_source(protocol: ResearchProtocol) -> str:
    return f"{protocol.baseline_slippage_bps:.1f}bps_main"


def _build_cost_scenarios(
    *,
    price_df: pd.DataFrame,
    dividend_df: pd.DataFrame,
    protocol: ResearchProtocol,
) -> tuple[list[CostScenarioResult], dict[str, StrategyArtifact], list[dict[str, Any]]]:
    scenarios: list[tuple[str, float]] = [
        ("historical_control", CONTROL_SCENARIO_SLIPPAGE_BPS),
        ("main", float(protocol.baseline_slippage_bps)),
        ("stress", float(protocol.stress_slippage_bps)),
    ]
    results: list[CostScenarioResult] = []
    main_suite: dict[str, StrategyArtifact] = {}
    optimization_payloads: list[dict[str, Any]] = []
    for scenario_name, slippage_bps in scenarios:
        suite, optimized_candidates = run_legacy_core_suite(
            price_df,
            dividend_df,
            slippage_bps=slippage_bps,
        )
        if scenario_name == "main":
            main_suite = suite
            optimization_payloads = optimized_candidates
        results.append(
            CostScenarioResult(
                scenario=scenario_name,
                slippage_bps=float(slippage_bps),
                commission_rate=0.00025,
                sell_tax_rate=0.0,
                strategy_metrics={key: dict(value.metrics) for key, value in suite.items()},
                summary_source="legacy_core_suite",
            )
        )
    return results, main_suite, optimization_payloads


def _aggregate_family_oos(curves: Mapping[str, list[pd.DataFrame]]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for family, family_curves in curves.items():
        combined_curve = _chain_oos_curves(family_curves)
        payload[family] = _summarize_nav_curve(combined_curve)
    return payload


def _baseline_reference_payload() -> dict[str, Any]:
    return {
        "summary_json": str(BASELINE_REFERENCE_DIR / "510300_backtest_summary_20260410.json"),
        "report_markdown": str(BASELINE_REFERENCE_DIR / "510300_backtest_report_20260410.md"),
        "frozen_script": str(LEGACY_SCRIPT_PATH),
    }


def build_oos_fold_results(
    *,
    protocol: ResearchProtocol,
    price_map: Mapping[str, pd.DataFrame],
    dividend_map: Mapping[str, pd.DataFrame],
    futures_fee_info: Mapping[str, Any],
) -> tuple[list[FoldResult], dict[str, list[pd.DataFrame]], dict[str, list[pd.DataFrame]]]:
    core_prices = price_map.get("risk_core", pd.DataFrame())
    core_dividends = dividend_map.get("risk_core", pd.DataFrame())
    windows = build_monthly_windows(
        core_prices["date"],
        train_months=protocol.train_months,
        test_months=protocol.test_months,
        step_months=protocol.step_months,
    )
    assumptions = _legacy_assumptions(protocol.baseline_slippage_bps)
    fold_results: list[FoldResult] = []
    family_curves: dict[str, list[pd.DataFrame]] = {
        "scheme1": [],
        "scheme2": [],
        "optimized_regime": [],
        "family_a": [],
        "family_b": [],
        "family_c": [],
        "family_d": [],
    }
    full_candidate_curves: dict[str, list[pd.DataFrame]] = {
        "family_a": [],
        "family_b": [],
        "family_c": [],
        "family_d": [],
    }
    for fold_index, window in enumerate(windows, start=1):
        train_start = window["train_start"]
        train_end = window["train_end"]
        test_start = window["test_start"]
        test_end = window["test_end"]
        train_prices = _slice_frame(core_prices, train_start, train_end)
        train_dividends = _slice_dividends(core_dividends, train_start, train_end)
        test_prices = _slice_frame(core_prices, test_start, test_end)
        test_dividends = _slice_dividends(core_dividends, test_start, test_end)
        train_suite, _ = run_legacy_core_suite(train_prices, train_dividends, slippage_bps=protocol.baseline_slippage_bps)
        legacy = _load_legacy_module()
        _trade_dates, monthly_schedule, next_trade_after_ex = legacy.build_trading_calendar(test_prices, test_dividends)
        indicators = legacy.build_indicator_frame(test_prices)
        scheme1_test = legacy.simulate_monthly_dca(
            test_prices,
            test_dividends,
            monthly_schedule,
            next_trade_after_ex,
            assumptions,
            name="scheme1",
            description="oos scheme1",
            fixed_external_injection=True,
            take_profit_pct=None,
        )
        scheme2_tp = _safe_float(train_suite["scheme2"].extra.get("take_profit_pct"), 0.20)
        scheme2_test = legacy.simulate_monthly_dca(
            test_prices,
            test_dividends,
            monthly_schedule,
            next_trade_after_ex,
            assumptions,
            name="scheme2",
            description="oos scheme2",
            fixed_external_injection=False,
            take_profit_pct=scheme2_tp,
        )
        optimized_test = legacy.simulate_regime_strategy(
            test_prices,
            test_dividends,
            monthly_schedule,
            next_trade_after_ex,
            assumptions,
            indicators,
            ma_window=_safe_int(train_suite["optimized_regime"].extra.get("ma_window"), 150),
            rsi_floor=_safe_int(train_suite["optimized_regime"].extra.get("rsi_floor"), 45),
            rsi_cap=_safe_int(train_suite["optimized_regime"].extra.get("rsi_cap"), 90),
            sell_rsi=_safe_int(train_suite["optimized_regime"].extra.get("sell_rsi"), 40),
            use_slope=bool(train_suite["optimized_regime"].extra.get("use_slope", True)),
        )
        serialized_scheme1 = _serialize_strategy(scheme1_test)
        serialized_scheme2 = _serialize_strategy(scheme2_test)
        serialized_optimized = _serialize_strategy(optimized_test)
        family_curves["scheme1"].append(serialized_scheme1.equity_curve)
        family_curves["scheme2"].append(serialized_scheme2.equity_curve)
        family_curves["optimized_regime"].append(serialized_optimized.equity_curve)

        sliced_prices = _slice_price_map(price_map, train_start, train_end)
        family_selections: dict[str, dict[str, Any]] = {
            "scheme2": _candidate_payload("take_profit", train_suite["scheme2"]),
            "optimized_regime": _candidate_payload("optimized_regime", train_suite["optimized_regime"]),
        }
        family_metrics: dict[str, dict[str, Any]] = {
            "scheme1": dict(serialized_scheme1.metrics),
            "scheme2": dict(serialized_scheme2.metrics),
            "optimized_regime": dict(serialized_optimized.metrics),
        }
        for family in ("family_a", "family_b", "family_c", "family_d"):
            candidate_runs = _build_family_candidate_runs(
                family,
                price_map=sliced_prices,
                assumptions=assumptions,
                futures_fee_info=futures_fee_info,
            )
            selected_candidate_id, selected_candidate_run = _select_best_candidate(candidate_runs)
            family_selections[family] = _candidate_payload(selected_candidate_id, selected_candidate_run)
            if selected_candidate_run is None:
                family_metrics[family] = _empty_artifact(family).metrics
                continue
            test_price_map = _slice_price_map(price_map, test_start, test_end)
            oos_candidates = _build_family_candidate_runs(
                family,
                price_map=test_price_map,
                assumptions=assumptions,
                futures_fee_info=futures_fee_info,
            )
            oos_run = oos_candidates.get(selected_candidate_id) or _empty_artifact(family)
            family_metrics[family] = dict(oos_run.metrics)
            family_curves[family].append(oos_run.equity_curve)
            full_candidate_curves[family].extend(run.equity_curve for run in candidate_runs.values() if not run.equity_curve.empty)
        fold_results.append(
            FoldResult(
                fold_index=fold_index,
                train_start=train_start.strftime("%Y-%m-%d"),
                train_end=train_end.strftime("%Y-%m-%d"),
                test_start=test_start.strftime("%Y-%m-%d"),
                test_end=test_end.strftime("%Y-%m-%d"),
                selected_candidates=family_selections,
                oos_metrics=family_metrics,
            )
        )
    return fold_results, family_curves, full_candidate_curves


def build_selection_gate(
    *,
    aggregate_oos: Mapping[str, dict[str, Any]],
    fold_results: Sequence[FoldResult],
) -> list[SelectionGateResult]:
    benchmark = aggregate_oos.get("scheme1", {})
    benchmark_cagr = _safe_float(benchmark.get("cagr"))
    benchmark_mdd = _safe_float(benchmark.get("max_drawdown"))
    latest_selection: dict[str, dict[str, Any] | None] = {}
    for fold in fold_results:
        for family, candidate in fold.selected_candidates.items():
            latest_selection[family] = candidate
    results: list[SelectionGateResult] = []
    for family in ("scheme2", "optimized_regime", "family_a", "family_b", "family_c", "family_d"):
        metrics = aggregate_oos.get(family, {})
        oos_cagr = _safe_float(metrics.get("cagr"))
        oos_mdd = _safe_float(metrics.get("max_drawdown"))
        passed = oos_cagr >= benchmark_cagr * 2.0 and oos_mdd <= benchmark_mdd
        reason = "passed" if passed else "failed_gate"
        results.append(
            SelectionGateResult(
                family=family,
                passed=passed,
                oos_cagr=oos_cagr,
                benchmark_oos_cagr=benchmark_cagr,
                oos_max_drawdown=oos_mdd,
                benchmark_oos_max_drawdown=benchmark_mdd,
                selected_candidate=latest_selection.get(family),
                reason=reason,
            )
        )
    return results


def build_final_recommendation(selection_gate: Sequence[SelectionGateResult]) -> FinalRecommendation:
    passed = [item for item in selection_gate if item.passed and item.selected_candidate]
    passed.sort(key=lambda item: (-item.oos_cagr, item.oos_max_drawdown, item.family))
    if not passed:
        return FinalRecommendation(
            decision="no_candidate_passed",
            selected_family=None,
            selected_candidate=None,
            summary="全部放开后仍无候选通过门槛。",
            passed_gate=False,
        )
    winner = passed[0]
    return FinalRecommendation(
        decision="single_candidate",
        selected_family=winner.family,
        selected_candidate=winner.selected_candidate,
        summary=f"推荐 {winner.family} 作为唯一通过门槛的候选。",
        passed_gate=True,
    )


def render_formal_markdown(summary: Mapping[str, Any]) -> str:
    protocol = summary["research_protocol"]
    recommendation = summary["final_recommendation"]
    selection_gate = summary["selection_gate"]
    cost_scenarios = summary["cost_scenarios"]
    artifacts = dict(summary.get("artifacts") or {})
    lines = [
        "# 510300 研究升级 v3 正式报告",
        "",
        "## 一、研究协议",
        "",
        f"- 截止日期：{protocol['end_date']}",
        f"- Walk-forward：{protocol['train_months']}/{protocol['test_months']}/{protocol['step_months']} 月",
        f"- 成本主场景：{protocol['baseline_slippage_bps']:.1f} bps；压力场景：{protocol['stress_slippage_bps']:.1f} bps",
        f"- 现金池开关：{'开启' if protocol['enable_cash_sleeves'] else '关闭'}；增强家族开关：{'开启' if protocol['enable_enhancements'] else '关闭'}",
        "",
        "## 二、标的解析",
        "",
    ]
    for key, payload in summary["instrument_resolution"]["resolved"].items():
        lines.append(
            f"- `{key}`：{payload['name']}（{payload['code']}），历史样本 {payload['history_rows']} 行，"
            f"近 60 日中位成交额 {_ccy(_safe_float(payload['median_amount_60d']))}。"
        )
    lines.extend(
        [
            "",
            "## 三、成本场景",
            "",
            "| 场景 | 滑点(bps) | scheme1 CAGR | scheme2 CAGR | optimized CAGR |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for scenario in cost_scenarios:
        metrics = scenario["strategy_metrics"]
        lines.append(
            "| "
            + " | ".join(
                [
                    scenario["scenario"],
                    f"{scenario['slippage_bps']:.1f}",
                    _pct(_safe_float(metrics.get("scheme1", {}).get("cagr"))),
                    _pct(_safe_float(metrics.get("scheme2", {}).get("cagr"))),
                    _pct(_safe_float(metrics.get("optimized_regime", {}).get("cagr"))),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 四、样本外门槛",
            "",
            "| 家族 | OOS CAGR | 基准 OOS CAGR | OOS MDD | 基准 OOS MDD | 通过 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for gate in selection_gate:
        lines.append(
            "| "
            + " | ".join(
                [
                    gate["family"],
                    _pct(_safe_float(gate["oos_cagr"])),
                    _pct(_safe_float(gate["benchmark_oos_cagr"])),
                    _pct(_safe_float(gate["oos_max_drawdown"])),
                    _pct(_safe_float(gate["benchmark_oos_max_drawdown"])),
                    "是" if gate["passed"] else "否",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 五、最终推荐",
            "",
            f"- 结论：{recommendation['summary']}",
            f"- 决策枚举：`{recommendation['decision']}`",
            "",
            "## 六、产物与 promotion",
            "",
            f"- bundle：`{artifacts.get('bundle_dir', 'pending_write')}`",
            f"- CSV 数量：{len(list(artifacts.get('csv_inventory') or []))}",
            f"- baseline 冻结引用：`{summary['research_protocol']['baseline_reference']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def create_bundle_artifacts(timestamp: str | None = None) -> BundleArtifacts:
    run_id = timestamp or pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    bundle_dir = V3_RUNS_ROOT / run_id
    csv_dir = bundle_dir / "csv"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)
    return BundleArtifacts(
        bundle_dir=bundle_dir,
        summary_path=bundle_dir / "summary.json",
        markdown_path=bundle_dir / "formal_report.md",
        csv_dir=csv_dir,
        latest_json_path=V3_REPORT_ROOT / "latest.json",
        latest_markdown_path=V3_REPORT_ROOT / "latest.md",
        latest_pdf_path=V3_REPORT_ROOT / "latest.pdf",
    )


def write_bundle(
    *,
    bundle: BundleArtifacts,
    summary: Mapping[str, Any],
    markdown_text: str,
    csv_frames: Mapping[str, pd.DataFrame],
) -> None:
    csv_inventory: list[str] = []
    for name, frame in csv_frames.items():
        path = bundle.csv_dir / f"{name}.csv"
        frame.to_csv(path, index=False)
        csv_inventory.append(path.name)
    payload = dict(summary)
    payload["artifacts"] = {
        "bundle_dir": str(bundle.bundle_dir),
        "summary_path": str(bundle.summary_path),
        "markdown_path": str(bundle.markdown_path),
        "csv_inventory": csv_inventory,
    }
    _write_json(bundle.summary_path, payload)
    bundle.markdown_path.write_text(markdown_text, encoding="utf-8")
    V3_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle.summary_path, bundle.latest_json_path)
    shutil.copy2(bundle.markdown_path, bundle.latest_markdown_path)


def load_bundle_summary(bundle_dir: Path) -> dict[str, Any]:
    return json.loads((bundle_dir / "summary.json").read_text(encoding="utf-8"))


def validate_bundle_for_promotion(bundle_dir: Path, pdf_path: Path | None = None) -> dict[str, Any]:
    summary = load_bundle_summary(bundle_dir)
    markdown_path = bundle_dir / "formal_report.md"
    csv_dir = bundle_dir / "csv"
    markdown_text = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    csv_files = sorted(csv_dir.glob("*.csv"))
    schema_ok = required_summary_fields().issubset(summary.keys())
    csv_ok = bool(csv_files) and all(path.stat().st_size > 0 for path in csv_files)
    pdf_ok = bool(pdf_path and pdf_path.exists() and pdf_path.stat().st_size > 0)
    markdown_ok = summary.get("final_recommendation", {}).get("summary") in markdown_text
    recommendation_gate = bool(summary.get("final_recommendation", {}).get("passed_gate"))
    return {
        "schema_ok": bool(schema_ok),
        "csv_ok": bool(csv_ok),
        "markdown_pdf_consistent": bool(markdown_ok and pdf_ok),
        "recommendation_gate": recommendation_gate,
        "promotion_ready": bool(schema_ok and csv_ok and markdown_ok and pdf_ok and recommendation_gate),
    }


def finalize_bundle_outputs(bundle_dir: Path, pdf_path: Path) -> dict[str, Any]:
    checks = validate_bundle_for_promotion(bundle_dir, pdf_path=pdf_path)
    summary = load_bundle_summary(bundle_dir)
    shutil.copy2(bundle_dir / "summary.json", V3_REPORT_ROOT / "latest.json")
    shutil.copy2(bundle_dir / "formal_report.md", V3_REPORT_ROOT / "latest.md")
    shutil.copy2(pdf_path, V3_REPORT_ROOT / "latest.pdf")
    applied = False
    if checks["promotion_ready"]:
        promoted_dir = V3_REPORT_ROOT / "promoted"
        promoted_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle_dir / "formal_report.md", promoted_dir / ROOT_REPORT_MD)
        shutil.copy2(pdf_path, promoted_dir / ROOT_REPORT_PDF)
        applied = True
    summary["promotion"] = {**checks, "applied": applied, "pdf_path": str(pdf_path)}
    _write_json(bundle_dir / "summary.json", summary)
    shutil.copy2(bundle_dir / "summary.json", V3_REPORT_ROOT / "latest.json")
    return summary["promotion"]


def _build_csv_frames(
    *,
    main_suite: Mapping[str, StrategyArtifact],
    fold_results: Sequence[FoldResult],
    selection_gate: Sequence[SelectionGateResult],
    aggregate_oos: Mapping[str, dict[str, Any]],
) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {
        "folds": pd.DataFrame([asdict(item) for item in fold_results]),
        "selection_gate": pd.DataFrame([asdict(item) for item in selection_gate]),
        "aggregate_oos": pd.DataFrame(
            [{"family": family, **metrics} for family, metrics in aggregate_oos.items()]
        ),
    }
    for key, artifact in main_suite.items():
        frames[f"main_{key}_equity_curve"] = artifact.equity_curve
        if artifact.trades is not None and not artifact.trades.empty:
            frames[f"main_{key}_trades"] = artifact.trades
    return frames

# --- research_510300_v3_parts/part_5.py ---
def run_510300_research_v3(protocol: ResearchProtocol, *, timestamp: str | None = None) -> BundleArtifacts:
    resolved, price_map, dividend_map, futures_fee_info = resolve_default_instruments(protocol.end_date)
    core_prices = price_map["risk_core"]
    core_dividends = dividend_map["risk_core"]
    cost_scenarios, main_suite, optimization_payloads = _build_cost_scenarios(
        price_df=core_prices,
        dividend_df=core_dividends,
        protocol=protocol,
    )
    fold_results, family_curves, _full_candidate_curves = build_oos_fold_results(
        protocol=protocol,
        price_map=price_map,
        dividend_map=dividend_map,
        futures_fee_info=futures_fee_info,
    )
    aggregate_oos = _aggregate_family_oos(family_curves)
    cash_sleeve_results: list[CashSleeveResult] = []
    if protocol.enable_cash_sleeves:
        for key in ("cash_money", "cash_short_bond", "cash_treasury"):
            frame = price_map.get(key, pd.DataFrame())
            funding_dates = {date: MONTHLY_CONTRIBUTION for date in _build_monthly_execution_dates(core_prices["date"])}
            scheduler_audit = simulate_cash_sleeve_scheduler(cash_price_df=frame, funding_needs=funding_dates)
            cash_sleeve_results.append(
                CashSleeveResult(
                    family=key,
                    selected_instrument=asdict(resolved[key]),
                    metrics={"history_rows": int(len(frame)), "median_amount_60d": _median_amount_60(frame)},
                    scheduler_audit=scheduler_audit,
                )
            )
    enhancement_results: list[EnhancementFamilyResult] = []
    if protocol.enable_enhancements:
        assumptions = _legacy_assumptions(protocol.baseline_slippage_bps)
        for family in ("family_a", "family_b", "family_c", "family_d"):
            candidate_runs = _build_family_candidate_runs(
                family,
                price_map=price_map,
                assumptions=assumptions,
                futures_fee_info=futures_fee_info,
            )
            candidate_id, candidate_run = _select_best_candidate(candidate_runs)
            enhancement_results.append(
                EnhancementFamilyResult(
                    family=family,
                    selected_candidate=_candidate_payload(candidate_id, candidate_run),
                    aggregate_oos=aggregate_oos.get(family, {}),
                    validation=_build_candidate_validation(candidate_runs),
                    candidate_count=len(candidate_runs),
                    notes=[],
                )
            )
    selection_gate = build_selection_gate(aggregate_oos=aggregate_oos, fold_results=fold_results)
    final_recommendation = build_final_recommendation(selection_gate)
    summary = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "research_protocol": {
            **asdict(protocol),
            "baseline_reference": protocol.baseline_reference or _baseline_reference_payload()["summary_json"],
        },
        "baseline_reference": _baseline_reference_payload(),
        "instrument_resolution": {
            "resolved": {key: asdict(value) for key, value in resolved.items()},
        },
        "cost_scenarios": [asdict(item) for item in cost_scenarios],
        "optimization_candidates": optimization_payloads,
        "oos_folds": [asdict(item) for item in fold_results],
        "cash_sleeve_results": [asdict(item) for item in cash_sleeve_results],
        "enhancement_results": [asdict(item) for item in enhancement_results],
        "selection_gate": [asdict(item) for item in selection_gate],
        "final_recommendation": asdict(final_recommendation),
        "backtest_sanity_check": {},
    }
    try:
        market_data = {
            code: frame.loc[:, ["date", "open", "high", "low", "close", "volume"]].to_dict(orient="records")
            for code, frame in price_map.items()
            if code in {"risk_core", "style_500", "style_chinext", "style_div_lowvol"} and not frame.empty
        }
        sanity = BacktestEngine.run_portfolio_backtest(
            market_data,
            strategy="buy_and_hold",
            params={"initial_capital": 100000.0, "target_weight_scheme": "equal_weight", "commission": 0.00025},
            return_trades=False,
        )
        summary["backtest_sanity_check"] = sanity
    except Exception as exc:
        summary["backtest_sanity_check"] = {"success": False, "error": str(exc)}
    markdown_text = render_formal_markdown(summary)
    bundle = create_bundle_artifacts(timestamp=timestamp)
    write_bundle(
        bundle=bundle,
        summary=summary,
        markdown_text=markdown_text,
        csv_frames=_build_csv_frames(
            main_suite=main_suite,
            fold_results=fold_results,
            selection_gate=selection_gate,
            aggregate_oos=aggregate_oos,
        ),
    )
    return bundle

