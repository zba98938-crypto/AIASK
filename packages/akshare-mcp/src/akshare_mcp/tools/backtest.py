"""回测工具"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, List, Tuple
from ..services import backtest_engine
from ..services.data_sync import data_sync_service
from ..storage import get_db
from ..utils import ok, fail, normalize_code, parse_date_input, resolve_existing_security_code_async
from .manager_protocol import fail_with_meta, ok_with_meta
from .market import get_kline_data
from .pit_middleware import build_pit_meta_simple

# 检查Ray是否可用
RAY_AVAILABLE = False
try:
    from ..services.backtest import ParallelBacktestEngine
    RAY_AVAILABLE = True
except ImportError:
    pass




def _normalize_dates_global(
    start_date: Optional[str], end_date: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    sd = start_date
    ed = end_date
    if sd and len(sd) == 4:
        sd = f"{sd}-01-01"
    if ed and len(ed) == 4:
        ed = f"{ed}-12-31"
    return sd, ed


def _normalize_klines_global(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for row in klines or []:
        if not isinstance(row, dict):
            continue
        if row.get("close") is None:
            continue
        date_val = row.get("date") or row.get("time")
        if date_val is not None:
            row = {**row, "date": str(date_val)[:10]}
        cleaned.append(row)
    cleaned.sort(key=lambda x: str(x.get("date") or x.get("time") or ""))
    return cleaned


async def _fetch_klines_for_code(
    db: Any,
    code: str,
    start_date: Optional[str],
    end_date: Optional[str],
    limit: int = 300,
) -> tuple[List[Dict[str, Any]], str]:
    try:
        klines = await db.get_klines(code, start_date, end_date)
    except Exception:
        klines = []

    normalized = _normalize_klines_global(klines)
    if normalized:
        return normalized, "sqlite"

    fallback = await get_kline_data(
        code=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        adjust="qfq",
    )
    if fallback.get("success"):
        fallback_klines = _normalize_klines_global(fallback.get("data") or [])
        if fallback_klines:
            return fallback_klines, "market_fallback"

    return [], "none"


def _estimate_limit_global(
    start_date: Optional[str], end_date: Optional[str], default: int = 300
) -> int:
    start = parse_date_input(start_date) if start_date else None
    end = parse_date_input(end_date) if end_date else None
    if start and end:
        days = abs((end - start).days) + 1
        return min(max(days, 50), 1000)
    return default


async def _fetch_klines_batch_global(
    db: Any,
    codes: List[str],
    start_date: Optional[str],
    end_date: Optional[str],
    fetch_concurrency: int,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """Prefer DB batch fetch and fallback to bounded concurrent single-code fetch."""
    source_counter: Dict[str, int] = {
        "sqlite_batch": 0,
        "sqlite": 0,
        "market_fallback": 0,
        "none": 0,
    }

    normalized_codes = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if not normalized_codes:
        return {}, source_counter

    klines_dict: Dict[str, List[Dict[str, Any]]] = {}
    batch_method = getattr(db, "get_klines_batch", None)
    if callable(batch_method):
        try:
            limit = _estimate_limit_global(start_date, end_date)
            batch_rows = await batch_method(normalized_codes, start_date, end_date, limit)
            rows_dict = batch_rows if isinstance(batch_rows, dict) else {}
            for code in normalized_codes:
                normalized = _normalize_klines_global(rows_dict.get(code, []))
                if normalized:
                    klines_dict[code] = normalized
                    source_counter["sqlite_batch"] += 1
        except Exception:
            pass

    missing_codes = [c for c in normalized_codes if c not in klines_dict]
    if missing_codes:
        concurrency = max(1, min(int(fetch_concurrency or 1), 20))
        semaphore = asyncio.Semaphore(concurrency)
        limit = _estimate_limit_global(start_date, end_date)

        async def _worker(code: str) -> Tuple[str, List[Dict[str, Any]], str]:
            async with semaphore:
                klines, source = await _fetch_klines_for_code(
                    db=db,
                    code=code,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                )
                return code, klines, source

        results = await asyncio.gather(*[_worker(code) for code in missing_codes], return_exceptions=True)
        for item in results:
            if isinstance(item, Exception):
                source_counter["none"] += 1
                continue
            code, klines, source = item
            source_counter[source] = source_counter.get(source, 0) + 1
            if klines:
                klines_dict[code] = klines

    return klines_dict, source_counter


def _dedupe_failure_reasons(reasons: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    deduped: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in reasons or []:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code") or "") if item.get("code") else ""
        reason = str(item.get("reason") or "").strip() or "unknown"
        key = (code, reason)
        if key in seen:
            continue
        seen.add(key)
        payload = {"reason": reason}
        if code:
            payload["code"] = code
        deduped.append(payload)
    return deduped


def _finalize_batch_backtest_payload(
    payload: Dict[str, Any],
    *,
    normalized_codes: List[str],
    source_stats: Dict[str, int],
    fetch_concurrency: int,
    execution_mode: str,
    total_start: float,
    io_seconds: float,
    compute_seconds: Optional[float] = None,
    aggregation_seconds: Optional[float] = None,
    warmup_before_fetch: bool = False,
    warmup_result: Optional[Dict[str, Any]] = None,
    failure_reasons: Optional[List[Dict[str, Any]]] = None,
    processed_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    payload = dict(payload or {})
    payload["execution_mode"] = execution_mode
    payload["codes_count"] = len(normalized_codes)
    payload["requested_codes"] = normalized_codes
    payload["fetch_concurrency"] = max(1, min(int(fetch_concurrency or 1), 20))
    payload["source_stats"] = source_stats
    payload["warmup_enabled"] = bool(warmup_before_fetch)
    if warmup_result is not None:
        payload["warmup"] = warmup_result

    successful_results = [
        r for r in (payload.get("results") or []) if isinstance(r, dict) and r.get("success", True)
    ]
    payload["successful_count"] = len(successful_results)
    payload["failed_count"] = payload["codes_count"] - payload["successful_count"]

    reasons = list(failure_reasons or [])
    if payload["successful_count"] == 0:
        processed = {normalize_code(code) for code in (processed_codes or []) if str(code).strip()}
        covered_codes = {
            normalize_code(item.get("code") or "")
            for item in reasons
            if isinstance(item, dict) and item.get("code")
        }
        for code in normalized_codes:
            if code in covered_codes:
                continue
            reasons.append({
                "code": code,
                "reason": "backtest_failed" if code in processed else "no_kline_data",
            })
        payload["degraded"] = True
        payload["quality_flags"] = list(dict.fromkeys(list(payload.get("quality_flags") or []) + ["degraded", "all_fetch_failed"]))
    else:
        payload.setdefault("degraded", False)
        payload.setdefault("quality_flags", [])

    if reasons:
        payload["failure_reasons"] = _dedupe_failure_reasons(reasons)

    if successful_results:
        avg_return = sum(r.get("total_return", 0) for r in successful_results) / len(successful_results)
        avg_sharpe = sum(r.get("sharpe_ratio", 0) for r in successful_results) / len(successful_results)
        summary = {
            "avg_return": float(avg_return),
            "avg_return_pct": f"{avg_return * 100:.2f}%",
            "avg_sharpe_ratio": float(avg_sharpe),
        }
        avg_max_dd = [
            r.get("max_drawdown")
            for r in successful_results
            if isinstance(r, dict) and r.get("max_drawdown") is not None
        ]
        if avg_max_dd:
            avg_max_dd_value = sum(avg_max_dd) / len(avg_max_dd)
            summary["avg_max_drawdown"] = float(avg_max_dd_value)
            summary["avg_max_drawdown_pct"] = f"{avg_max_dd_value * 100:.2f}%"
        payload["summary"] = summary

    total_seconds = time.perf_counter() - total_start
    timings = {
        "io_fetch_seconds": round(io_seconds, 6),
        "total_seconds": round(total_seconds, 6),
    }
    if compute_seconds is not None:
        timings["compute_seconds"] = round(compute_seconds, 6)
    if aggregation_seconds is not None:
        timings["aggregation_seconds"] = round(aggregation_seconds, 6)
    payload["timings"] = timings
    payload["execution_time"] = f"{total_seconds:.2f}s"
    return payload


def _build_backtest_pit_meta(
    *,
    as_of: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    short_period: Optional[int] = None,
    long_period: Optional[int] = None,
) -> Dict[str, Any]:
    event_time_window = None
    if start_date or end_date:
        event_time_window = {
            "start": start_date,
            "end": end_date,
        }

    feature_time_window = None
    resolved_lookback = max(int(short_period or 0), int(long_period or 0), 0)
    if resolved_lookback > 0:
        feature_time_window = {"lookback_bars": resolved_lookback}

    resolved_as_of = as_of or end_date or start_date
    return build_pit_meta_simple(
        resolved_as_of,
        event_time=end_date or start_date,
        event_time_window=event_time_window,
        feature_time_window=feature_time_window,
    )


def _build_execution_reality_payload(cost: Dict[str, Any]) -> Dict[str, Any]:
    from ..services.execution_reality import build_execution_reality_report

    report = build_execution_reality_report(
        mode="backtest",
        fill_model="close_price",
        kwargs=cost,
    )
    return report.to_dict()


async def run_simple_backtest(
    code: str,
    strategy: str = 'ma_cross',
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    initial_capital: float = 100000,
    commission: float = 0.0003,
    short_period: int = 5,
    long_period: int = 20,
    benchmark: str = '000300',
    slippage: float = 0.0,
    as_of: Optional[str] = None,
):
    """模块级回测接口（兼容测试中的直接导入调用）。"""
    try:
        db = get_db()
        code = normalize_code(code)
        start_date, end_date = _normalize_dates_global(start_date, end_date)

        klines, _ = await _fetch_klines_for_code(db, code, start_date, end_date)
        if not klines:
            return fail('No kline data found')

        benchmark_klines: List[Dict[str, Any]] = []
        benchmark_code = (benchmark or '').strip()
        if benchmark_code:
            bm_code = normalize_code(benchmark_code)
            benchmark_klines, _ = await _fetch_klines_for_code(db, bm_code, start_date, end_date)

        from ..services.cost_model import resolve_cost_assumptions
        cost = resolve_cost_assumptions(
            {"commission": commission, "slippage": slippage},
            default_mode="backtest",
        )
        params = {
            'initial_capital': initial_capital,
            'commission': cost["commission_rate"],
            'slippage': cost["slippage_bps"] / 10000.0,
            'short_period': short_period,
            'long_period': long_period,
            'benchmark': benchmark_code,
            'benchmark_klines': benchmark_klines,
            '_cost_assumptions': cost,
        }
        pit_meta = _build_backtest_pit_meta(
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
            short_period=short_period,
            long_period=long_period,
        )
        execution_reality = _build_execution_reality_payload(cost)
        result = backtest_engine.run_backtest(code, klines, strategy, params)
        if result.get('success'):
            payload = dict(result.get('data') or {})
            payload["pit"] = pit_meta
            payload["execution_reality"] = execution_reality
            return ok(payload)
        return fail(result.get('error', 'Backtest failed'))
    except Exception as e:
        return fail(str(e))


async def run_batch_backtest(
    codes: List[str],
    strategy: str = 'ma_cross',
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    initial_capital: float = 100000,
    commission: float = 0.0003,
    short_period: int = 5,
    long_period: int = 20,
    use_parallel: bool = True,
    fetch_concurrency: int = 8,
    warmup_before_fetch: bool = False,
    as_of: Optional[str] = None,
):
    """模块级批量回测接口（兼容 tests 直接 import）。"""
    try:
        total_start = time.perf_counter()
        db = get_db()

        start_date, end_date = _normalize_dates_global(start_date, end_date)
        normalized_codes = [normalize_code(c) for c in (codes or [])]
        if not normalized_codes:
            return fail('codes is empty')

        if warmup_before_fetch:
            await data_sync_service.sync_stock_klines(
                codes=normalized_codes,
                start_date=start_date or '',
                end_date=end_date or '',
                period='daily',
            )

        from ..services.cost_model import resolve_cost_assumptions
        cost = resolve_cost_assumptions(
            {"commission": commission},
            default_mode="backtest",
        )
        params = {
            'initial_capital': initial_capital,
            'commission': cost["commission_rate"],
            'short_period': short_period,
            'long_period': long_period,
            '_cost_assumptions': cost,
        }
        pit_meta = _build_backtest_pit_meta(
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
            short_period=short_period,
            long_period=long_period,
        )
        execution_reality = _build_execution_reality_payload(cost)
        io_start = time.perf_counter()
        klines_dict, source_stats = await _fetch_klines_batch_global(
            db=db,
            codes=normalized_codes,
            start_date=start_date,
            end_date=end_date,
            fetch_concurrency=fetch_concurrency,
        )
        io_seconds = time.perf_counter() - io_start

        results: List[Dict[str, Any]] = []
        failure_reasons: List[Dict[str, Any]] = []
        for code in normalized_codes:
            klines = klines_dict.get(code) or []
            if not klines:
                failure_reasons.append({"code": code, "reason": "no_kline_data"})
                continue
            single = backtest_engine.run_backtest(code, klines, strategy, params)
            if single.get('success'):
                results.append(single.get('data') or {})
            else:
                failure_reasons.append({
                    "code": code,
                    "reason": str(single.get("error") or "backtest_failed"),
                })

        payload = _finalize_batch_backtest_payload({
            'results': results,
            'count': len(results),
        },
            normalized_codes=normalized_codes,
            source_stats=source_stats,
            fetch_concurrency=fetch_concurrency,
            execution_mode='parallel_optimized' if (use_parallel and RAY_AVAILABLE) else 'local_sequential',
            total_start=total_start,
            io_seconds=io_seconds,
            failure_reasons=failure_reasons,
            processed_codes=list(klines_dict.keys()),
        )
        payload["pit"] = pit_meta
        payload["execution_reality"] = execution_reality
        return ok(payload)
    except Exception as e:
        return fail(str(e))


__all__ = ['run_simple_backtest', 'run_batch_backtest', 'register']



def register(mcp):
    """注册回测工具"""

    def _normalize_dates(
        start_date: Optional[str], end_date: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        sd = start_date
        ed = end_date
        if sd and len(sd) == 4:
            sd = f"{sd}-01-01"
        if ed and len(ed) == 4:
            ed = f"{ed}-12-31"
        return sd, ed

    def _estimate_limit(
        start_date: Optional[str], end_date: Optional[str], default: int = 300
    ) -> int:
        return _estimate_limit_global(start_date, end_date, default)

    def _normalize_klines(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return _normalize_klines_global(klines)

    async def _fetch_klines(
        db, code: str, start_date: Optional[str], end_date: Optional[str]
    ) -> tuple[List[Dict[str, Any]], str]:
        limit = _estimate_limit(start_date, end_date)
        return await _fetch_klines_for_code(
            db=db,
            code=code,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    async def _fetch_klines_batch(
        db,
        codes: List[str],
        start_date: Optional[str],
        end_date: Optional[str],
        fetch_concurrency: int,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
        return await _fetch_klines_batch_global(
            db=db,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            fetch_concurrency=fetch_concurrency,
        )

    @mcp.tool()
    async def run_simple_backtest(
        code: str,
        strategy: str = 'ma_cross',
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        initial_capital: float = 100000,
        commission: float = 0.0003,
        short_period: int = 5,
        long_period: int = 20,
        benchmark: str = '000300',
        slippage: float = 0.0,
        as_of: Optional[str] = None,
    ):
        """
        运行简单回测

        Args:
            code: 股票代码
            strategy: 策略名称 ('ma_cross', 'buy_and_hold', 'momentum', 'rsi')
            start_date: 开始日期 (YYYY-MM-DD 或 YYYY)
            end_date: 结束日期 (YYYY-MM-DD 或 YYYY)
            initial_capital: 初始资金
            commission: 手续费率
            short_period: 短期均线周期
            long_period: 长期均线周期
            benchmark: 基准代码（默认000300）
            slippage: 滑点成本率（与commission叠加）
        """
        started_at = time.perf_counter()
        source_chain = ["backtest.run_simple_backtest", "db.get_klines"]
        pit_meta = _build_backtest_pit_meta(
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
            short_period=short_period,
            long_period=long_period,
        )
        try:
            db = get_db()

            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail_with_meta(
                    error,
                    tool_name="run_simple_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {"status": "invalid_params"},
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": normalize_code(code or ""),
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": True,
                    },
                )

            # 日期格式处理：支持 YYYY 或 YYYY-MM-DD
            start_date, end_date = _normalize_dates(start_date, end_date)

            klines, data_source_name = await _fetch_klines(db, code, start_date, end_date)
            if data_source_name == "market_fallback":
                source_chain.append("market.get_kline_data")

            if not klines:
                return fail_with_meta(
                    'No kline data found',
                    tool_name="run_simple_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {"status": "not_found", "data_source": data_source_name},
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": code,
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": True,
                    },
                )

            benchmark_klines: List[Dict[str, Any]] = []
            benchmark_code = (benchmark or '').strip()
            benchmark_source: Optional[str] = None
            if benchmark_code:
                bm_code = normalize_code(benchmark_code)
                benchmark_klines, benchmark_source = await _fetch_klines(db, bm_code, start_date, end_date)
                if benchmark_source == "market_fallback":
                    source_chain.append("market.get_kline_data:benchmark")

            from ..services.cost_model import resolve_cost_assumptions
            cost = resolve_cost_assumptions(
                {"commission": commission, "slippage": slippage},
                default_mode="backtest",
            )
            params = {
                'initial_capital': initial_capital,
                'commission': cost["commission_rate"],
                'slippage': cost["slippage_bps"] / 10000.0,
                'short_period': short_period,
                'long_period': long_period,
                'benchmark': benchmark_code,
                'benchmark_klines': benchmark_klines,
                '_cost_assumptions': cost,
            }
            execution_reality = _build_execution_reality_payload(cost)

            result = backtest_engine.run_backtest(code, klines, strategy, params)

            if result.get('success'):
                degraded = data_source_name != "sqlite" or bool(benchmark_code and not benchmark_klines)
                payload = dict(result['data'])
                payload["pit"] = pit_meta
                payload["execution_reality"] = execution_reality
                return ok_with_meta(
                    payload,
                    tool_name="run_simple_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {
                            "status": "partial" if degraded else "available",
                            "data_source": data_source_name,
                            "benchmark_source": benchmark_source,
                            "strategy": strategy,
                        },
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": code,
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": degraded,
                    },
                )
            else:
                return fail_with_meta(
                    result.get('error', 'Backtest failed'),
                    tool_name="run_simple_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {"status": "failed", "data_source": data_source_name, "strategy": strategy},
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": code,
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": True,
                    },
                )

        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="run_simple_backtest",
                action="run",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": {"status": "failed", "strategy": strategy},
                    "pit": pit_meta,
                    "side_effect": {
                        "level": "read_only",
                        "target": normalize_code(code),
                        "confirmation_required": False,
                        "idempotent": True,
                    },
                    "degraded": True,
                },
            )

    @mcp.tool()
    async def run_batch_backtest(
        codes: List[str],
        strategy: str = 'ma_cross',
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        initial_capital: float = 100000,
        commission: float = 0.0003,
        short_period: int = 5,
        long_period: int = 20,
        use_parallel: bool = True,
        fetch_concurrency: int = 8,
        warmup_before_fetch: bool = False,
        as_of: Optional[str] = None,
    ):
        """
        批量回测多只股票（支持Ray并行加速）- 性能优化版

        Args:
            codes: 股票代码列表
            strategy: 策略名称 ('ma_cross', 'buy_and_hold', 'momentum', 'rsi')
            start_date: 开始日期 (YYYY-MM-DD 或 YYYY)
            end_date: 结束日期 (YYYY-MM-DD 或 YYYY)
            initial_capital: 初始资金
            commission: 手续费率
            short_period: 短期均线周期
            long_period: 长期均线周期
            use_parallel: 是否使用Ray并行计算（需要安装ray）
            warmup_before_fetch: 是否在回测前做数据预热

        Returns:
            批量回测结果，包含每只股票的回测指标
        """
        started_at = time.perf_counter()
        source_chain = ["backtest.run_batch_backtest", "db.get_klines_batch"]
        pit_meta = _build_backtest_pit_meta(
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
            short_period=short_period,
            long_period=long_period,
        )
        try:
            total_start = time.perf_counter()
            db = get_db()

            # 日期格式处理：支持 YYYY 或 YYYY-MM-DD
            start_date, end_date = _normalize_dates(start_date, end_date)
            normalized_codes = [normalize_code(c) for c in (codes or [])]
            if not normalized_codes:
                return fail_with_meta(
                    "codes is empty",
                    tool_name="run_batch_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {"status": "invalid_params"},
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": "batch_backtest",
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": True,
                    },
                )

            # 可选：回测前预热（走统一 data_sync 流程）
            warmup_result = None
            if warmup_before_fetch:
                source_chain.append("data_sync.sync_stock_klines")
                warmup_result = await data_sync_service.sync_stock_klines(
                    codes=normalized_codes,
                    start_date=start_date or "",
                    end_date=end_date or "",
                    period="daily",
                )

            # 阶段1：并发取数（优先批量DB）
            io_start = time.perf_counter()
            klines_dict, source_stats = await _fetch_klines_batch(
                db=db,
                codes=normalized_codes,
                start_date=start_date,
                end_date=end_date,
                fetch_concurrency=fetch_concurrency,
            )
            io_seconds = time.perf_counter() - io_start
            if source_stats.get("market_fallback"):
                source_chain.append("market.get_kline_data")

            if not klines_dict:
                return fail_with_meta(
                    "No kline data found for any code",
                    tool_name="run_batch_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {"status": "not_found", "source_stats": source_stats},
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": "batch_backtest",
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": True,
                    },
                )

            from ..services.cost_model import resolve_cost_assumptions
            cost = resolve_cost_assumptions(
                {"commission": commission},
                default_mode="backtest",
            )
            params = {
                "initial_capital": initial_capital,
                "commission": cost["commission_rate"],
                "short_period": short_period,
                "long_period": long_period,
                "_cost_assumptions": cost,
            }
            execution_reality = _build_execution_reality_payload(cost)

            # 阶段2：回测计算
            compute_start = time.perf_counter()
            engine = globals().get("ParallelBacktestEngine")
            can_parallel = bool(use_parallel and RAY_AVAILABLE and engine)
            failure_reasons: List[Dict[str, Any]] = [
                {"code": code, "reason": "no_kline_data"}
                for code in normalized_codes
                if code not in klines_dict
            ]
            if can_parallel:
                result = engine.batch_backtest(
                    list(klines_dict.keys()),
                    klines_dict,
                    strategy,
                    params,
                )
                execution_mode = "parallel_optimized"
            elif engine and hasattr(engine, "batch_backtest_sequential"):
                result = engine.batch_backtest_sequential(
                    list(klines_dict.keys()),
                    klines_dict,
                    strategy,
                    params,
                )
                execution_mode = "sequential"
            else:
                # 无Ray环境下的纯本地后备执行
                local_results: List[Dict[str, Any]] = []
                for code in list(klines_dict.keys()):
                    single = backtest_engine.run_backtest(
                        code,
                        klines_dict[code],
                        strategy,
                        params,
                    )
                    if single.get("success"):
                        local_results.append(single.get("data") or {})
                    else:
                        failure_reasons.append({
                            "code": code,
                            "reason": str(single.get("error") or "backtest_failed"),
                        })
                result = {
                    "success": True,
                    "data": {"results": local_results, "count": len(local_results)},
                }
                execution_mode = "local_sequential"
            compute_seconds = time.perf_counter() - compute_start

            if not result.get("success"):
                return fail_with_meta(
                    result.get("error", "Batch backtest failed"),
                    tool_name="run_batch_backtest",
                    action="run",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta={
                        "quality": {"status": "failed", "source_stats": source_stats},
                        "pit": pit_meta,
                        "side_effect": {
                            "level": "read_only",
                            "target": "batch_backtest",
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                        "degraded": True,
                    },
                )

            # 阶段3：汇总统计
            aggregation_start = time.perf_counter()
            aggregation_seconds = time.perf_counter() - aggregation_start
            payload = _finalize_batch_backtest_payload(
                result.get("data") or {},
                normalized_codes=normalized_codes,
                source_stats=source_stats,
                fetch_concurrency=fetch_concurrency,
                execution_mode=execution_mode,
                total_start=total_start,
                io_seconds=io_seconds,
                compute_seconds=compute_seconds,
                aggregation_seconds=aggregation_seconds,
                warmup_before_fetch=warmup_before_fetch,
                warmup_result=warmup_result,
                failure_reasons=failure_reasons,
                processed_codes=list(klines_dict.keys()),
            )
            payload["performance_goal"] = {
                "target": "same codes, total time reduce 30%+",
                "io_parallel_enabled": True,
            }
            payload["pit"] = pit_meta
            payload["execution_reality"] = execution_reality

            degraded = bool(payload.get("degraded")) or bool(source_stats.get("market_fallback")) or bool(source_stats.get("none"))
            return ok_with_meta(
                payload,
                tool_name="run_batch_backtest",
                action="run",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": {
                        "status": "partial" if degraded else "available",
                        "source_stats": source_stats,
                        "successful_count": payload.get("successful_count"),
                        "failed_count": payload.get("failed_count"),
                        "execution_mode": execution_mode,
                    },
                    "pit": pit_meta,
                    "side_effect": {
                        "level": "read_only",
                        "target": "batch_backtest",
                        "confirmation_required": False,
                        "idempotent": True,
                    },
                    "degraded": degraded,
                },
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="run_batch_backtest",
                action="run",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": {"status": "failed"},
                    "pit": pit_meta,
                    "side_effect": {
                        "level": "read_only",
                        "target": "batch_backtest",
                        "confirmation_required": False,
                        "idempotent": True,
                    },
                    "degraded": True,
                },
            )
