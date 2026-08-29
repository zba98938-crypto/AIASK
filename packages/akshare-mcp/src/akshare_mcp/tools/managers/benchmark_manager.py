"""基准评测管理器 - 接口级/结果级评分（v1）"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...storage import get_db
from ...utils import fail, normalize_code, ok
from ..market import get_kline
from ..manager_protocol import normalize_manager_payload

logger = logging.getLogger(__name__)
_REPORT_CACHE: Dict[str, Dict[str, Any]] = {}

# 与《MCP_股票分析能力测试用例清单_v1.md》建立映射（v2：扩展到日报自动化核心检查）
CASE_MAPPING: List[Dict[str, Any]] = [
    # IF-001（数据完整性）：日报首屏要求样本覆盖率高，避免“有结果但不可用”
    {
        "id": "IF-001",
        "category": "接口级",
        "title": "日报样本覆盖率检查（代理 IF-001）",
        "metric": "summary.coverage",
        "op": ">=",
        "threshold": 0.99,
        "eval_type": "proxy",
    },
    # IF-002（鲁棒性）：接口健康度底线，避免大面积异常导致日报失真
    {
        "id": "IF-002",
        "category": "接口级",
        "title": "接口健康度底线（代理 IF-002）",
        "metric": "scores.interface_score",
        "op": ">=",
        "threshold": 60,
        "eval_type": "proxy",
    },
    # IF-013（回测可用性）：有有效样本才能生成日报回测板块
    {
        "id": "IF-013",
        "category": "接口级",
        "title": "run_simple_backtest 合法日期区间返回回测指标",
        "metric": "summary.total",
        "op": ">",
        "threshold": 0,
        "eval_type": "direct",
    },
    # IF-014（参数校验相关）：通过成功率代理反映非法参数场景是否被正确处理
    {
        "id": "IF-014",
        "category": "接口级",
        "title": "回测参数校验有效性（代理 IF-014）",
        "metric": "summary.success_rate",
        "op": ">=",
        "threshold": 0.5,
        "eval_type": "proxy",
    },
    # IF-024（批量完成率）：日报批量任务核心SLA
    {
        "id": "IF-024",
        "category": "接口级",
        "title": "run_batch_backtest 20只股票完成率 >95%",
        "metric": "summary.success_rate",
        "op": ">=",
        "threshold": 0.95,
        "eval_type": "proxy",
    },
    # RS-001（跨工具一致性）：用综合质量分做日常一致性门槛
    {
        "id": "RS-001",
        "category": "结果级",
        "title": "核心分析结论一致性（代理 RS-001）",
        "metric": "scores.overall_score",
        "op": ">=",
        "threshold": 60,
        "eval_type": "proxy",
    },
    # RS-002（口径一致性）：结果质量分过低时，通常意味着口径或数据存在偏差
    {
        "id": "RS-002",
        "category": "结果级",
        "title": "估值与基本面口径一致性（代理 RS-002）",
        "metric": "scores.result_score",
        "op": ">=",
        "threshold": 55,
        "eval_type": "proxy",
    },
    # RS-005（回测一致性）：与基准偏差可控是日报可靠性的关键锚点
    {
        "id": "RS-005",
        "category": "结果级",
        "title": "run_simple_backtest vs backtest_manager 同参数收益误差<0.5%",
        "metric": "benchmark.benchmark_diff_abs",
        "op": "<=",
        "threshold": 0.005,
        "eval_type": "proxy",
    },
    # RS-006（结构完整性）：四维评分在日报中的可解释性入口
    {
        "id": "RS-006",
        "category": "结果级",
        "title": "评分结构完整性（代理 RS-006）",
        "metric": "scores.interface_score",
        "op": ">=",
        "threshold": 60,
        "eval_type": "proxy",
    },
    # RS-010（回测核心指标）：回测结果质量主门槛
    {
        "id": "RS-010",
        "category": "结果级",
        "title": "run_simple_backtest 回测核心指标完整",
        "metric": "scores.result_score",
        "op": ">=",
        "threshold": 60,
        "eval_type": "direct",
    },
]


def _normalize_kwargs(kwargs: dict) -> dict:
    extra = kwargs.get("kwargs")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except Exception:
            extra = None
    if isinstance(extra, dict):
        kwargs = {**kwargs, **extra}
    return kwargs


async def _get_benchmark_return(db, benchmark: str, lookback: int) -> Optional[float]:
    code = normalize_code(benchmark)
    klines = await db.get_klines(code, limit=lookback)
    if not klines or len(klines) < 2:
        res = await get_kline(code, "daily", lookback)
        if res.get("success") and res.get("data"):
            klines = res["data"]
    if not klines or len(klines) < 2:
        return None
    first = float(klines[0].get("close") or 0)
    last = float(klines[-1].get("close") or 0)
    if first <= 0:
        return None
    return (last - first) / first


def _score_result(row: Dict[str, Any], benchmark_return: Optional[float]) -> Dict[str, Any]:
    tr = float(row.get("total_return") or 0)
    sharpe = float(row.get("sharpe_ratio") or 0)
    mdd = abs(float(row.get("max_drawdown") or 0))
    score = 50 + max(-30, min(30, tr * 100)) + max(-20, min(20, sharpe * 10)) - max(0, min(30, mdd * 100))
    score = max(0.0, min(100.0, score))
    item = {
        "id": str(row.get("id", "")),
        "code": str(row.get("code", "")),
        "strategy": str(row.get("strategy", "")),
        "total_return": tr,
        "sharpe_ratio": sharpe,
        "max_drawdown": mdd,
        "quality_score": round(score, 2),
        "passed": score >= 60,
    }
    if benchmark_return is not None:
        item["benchmark_return"] = float(benchmark_return)
        item["excess_return"] = float(tr - benchmark_return)
    return item


def _get_by_path(data: Dict[str, Any], path: str):
    cur: Any = data
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _compare(actual: Optional[float], op: str, threshold: float) -> Optional[bool]:
    if actual is None:
        return None
    try:
        val = float(actual)
    except Exception:
        return None
    if op == ">":
        return val > threshold
    if op == ">=":
        return val >= threshold
    if op == "<":
        return val < threshold
    if op == "<=":
        return val <= threshold
    if op == "==":
        return val == threshold
    return None


def _build_case_scores(report: Dict[str, Any]) -> Dict[str, Any]:
    details: List[Dict[str, Any]] = []
    passed = 0
    failed = 0
    unknown = 0

    # 补充派生指标，便于映射表达
    bm = report.get("benchmark") or {}
    if bm.get("benchmark_diff") is not None:
        try:
            bm["benchmark_diff_abs"] = abs(float(bm.get("benchmark_diff") or 0.0))
        except Exception:
            bm["benchmark_diff_abs"] = None

    for case in CASE_MAPPING:
        metric = str(case.get("metric") or "")
        actual = _get_by_path(report, metric) if metric else None
        status = _compare(actual, str(case.get("op") or ""), float(case.get("threshold") or 0.0))
        if status is True:
            passed += 1
            status_text = "pass"
        elif status is False:
            failed += 1
            status_text = "fail"
        else:
            unknown += 1
            status_text = "unknown"

        note = "direct 映射：直接使用 benchmark_manager 当前报告字段判定"
        if str(case.get("eval_type") or "").lower() == "proxy":
            note = "proxy 映射：以可观测代理指标替代原始跨工具/跨场景校验"

        details.append({
            "id": case.get("id"),
            "category": case.get("category"),
            "title": case.get("title"),
            "eval_type": case.get("eval_type"),
            "metric": metric,
            "op": case.get("op"),
            "threshold": case.get("threshold"),
            "actual": actual,
            "status": status_text,
            "note": note,
        })

    total = len(details)
    rate = passed / total if total else 0.0
    return {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "unknown": unknown,
            "pass_rate": round(rate, 4),
        },
        "details": details,
    }


def _build_report(evaluated: List[Dict[str, Any]], benchmark_return: Optional[float]) -> Dict[str, Any]:
    total = len(evaluated)
    passed = sum(1 for x in evaluated if x["passed"])
    coverage = sum(1 for x in evaluated if x.get("code") and x.get("strategy")) / total if total else 0.0
    avg_score = sum(x["quality_score"] for x in evaluated) / total if total else 0.0
    avg_return = sum(x["total_return"] for x in evaluated) / total if total else 0.0
    success_rate = passed / total if total else 0.0
    benchmark_diff = (avg_return - benchmark_return) if (benchmark_return is not None and total > 0) else None
    interface_score = 100 * (0.6 * coverage + 0.4 * success_rate)
    overall_score = 0.4 * interface_score + 0.6 * avg_score

    report = {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": max(total - passed, 0),
            "coverage": round(coverage, 4),
            "success_rate": round(success_rate, 4),
            "latency_bucket": {"unknown": total},
        },
        "scores": {
            "interface_score": round(interface_score, 2),
            "result_score": round(avg_score, 2),
            "overall_score": round(overall_score, 2),
        },
        "benchmark": {
            "benchmark_return": benchmark_return,
            "avg_return": round(avg_return, 6),
            "benchmark_diff": round(benchmark_diff, 6) if benchmark_diff is not None else None,
        },
        "top_results": sorted(evaluated, key=lambda x: x["quality_score"], reverse=True)[:5],
    }

    report["case_mapping"] = _build_case_scores(report)
    return report


def register_benchmark_manager(mcp):
    """注册 benchmark_manager 工具"""

    @mcp.tool()
    async def benchmark_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """基准评测管理器（统一 action + kwargs 协议）

        Args:
            action: help/run_daily/get_report
            kwargs: JSON字符串或关键字参数
        """
        try:
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            db = get_db()

            if action == "help":
                return ok({"supported_actions": {
                    "run_daily": "每日全量评测（读取最近回测结果并打分）",
                    "get_report": "按 run_id 获取报告；无 run_id 时返回最新即时报告",
                    "help": "显示帮助信息",
                }})

            benchmark = str(kwargs.get("benchmark") or "000300").strip()
            limit = int(kwargs.get("limit", 100) or 100)
            lookback = int(kwargs.get("lookback", 252) or 252)

            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT id, code, strategy, total_return, sharpe_ratio, max_drawdown, created_at
                       FROM backtest_results ORDER BY created_at DESC LIMIT $1""",
                    max(1, min(limit, 1000)),
                )
            raw_rows = [dict(r) for r in rows]
            bench_ret = await _get_benchmark_return(db, benchmark, lookback)
            evaluated = [_score_result(r, bench_ret) for r in raw_rows]

            if action == "run_daily":
                run_id = f"bm_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                report = _build_report(evaluated, bench_ret)
                report.update({
                    "run_id": run_id,
                    "benchmark_code": benchmark,
                    "generated_at": datetime.now().isoformat(),
                })
                _REPORT_CACHE[run_id] = report
                return ok(report)

            if action == "get_report":
                run_id = str(kwargs.get("run_id") or "").strip()
                if run_id and run_id in _REPORT_CACHE:
                    return ok(_REPORT_CACHE[run_id])
                instant = _build_report(evaluated, bench_ret)
                instant.update({
                    "run_id": run_id or "instant",
                    "benchmark_code": benchmark,
                    "generated_at": datetime.now().isoformat(),
                })
                return ok(instant)

            return fail(f"Unknown action: {action}. Supported: help, run_daily, get_report")
        except Exception as e:
            logger.error(f"[BenchmarkManager] Error: {e}")
            return fail(str(e))
