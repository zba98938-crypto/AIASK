"""数据预热工具"""

from datetime import date, timedelta
from typing import Optional, List, Dict, Any

from ..cache import cache
from ..services.data_sync import data_sync_service
from ..utils import ok, fail, normalize_code


_LAST_WARMUP_STATE: Dict[str, Any] = {
    "last_warmup": None,
    "stocks": [],
    "lookback_days": None,
    "include_financials": None,
    "result": None,
}


def _normalize_warmup_stocks(stocks: Optional[List[str] | str]) -> List[str]:
    if isinstance(stocks, str):
        raw_items = [part.strip() for part in stocks.split(",") if part.strip()]
    else:
        raw_items = [str(item).strip() for item in (stocks or []) if str(item).strip()]

    normalized: List[str] = []
    seen = set()
    for item in raw_items:
        code = normalize_code(item)
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append(code)
    return normalized


def register(mcp):
    """注册数据预热工具"""

    @mcp.tool()
    async def data_warmup(
        action: str,
        stocks: Optional[List[str] | str] = None,
        lookback_days: int = 250,
        force_update: bool = False,
        include_financials: bool = True
    ):
        """
        数据预热

        Args:
            action: 操作 ('warmup', 'status', 'clear')
            stocks: 股票代码列表
            lookback_days: 回溯天数
            force_update: 强制更新（会先清理缓存）
            include_financials: 包含财务数据（当前仅记录参数，后续可扩展）
        """
        try:
            action = str(action or "").strip().lower()

            if action == "warmup":
                normalized_stocks = _normalize_warmup_stocks(stocks)
                if not normalized_stocks:
                    return fail("stocks is empty")

                lookback_days = max(1, int(lookback_days or 250))
                end_date = date.today()
                start_date = end_date - timedelta(days=lookback_days)

                if force_update:
                    cache.clear()

                sync_result = await data_sync_service.sync_stock_klines(
                    codes=normalized_stocks,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    period="daily",
                )

                _LAST_WARMUP_STATE.update(
                    {
                        "last_warmup": end_date.isoformat(),
                        "stocks": normalized_stocks,
                        "lookback_days": lookback_days,
                        "include_financials": bool(include_financials),
                        "result": sync_result,
                    }
                )

                stats = {
                    "stocks_warmed": len(normalized_stocks),
                    "lookback_days": lookback_days,
                    "period": "daily",
                    "force_update": bool(force_update),
                    "include_financials": bool(include_financials),
                    "sync_result": sync_result,
                }
                return ok(stats)

            if action == "status":
                status = {
                    "last_warmup": _LAST_WARMUP_STATE.get("last_warmup"),
                    "last_warmup_stocks": len(_LAST_WARMUP_STATE.get("stocks") or []),
                    "last_warmup_lookback_days": _LAST_WARMUP_STATE.get("lookback_days"),
                    "sync_metrics": data_sync_service.get_sync_metrics(),
                    "cache_stats": cache.get_cache_stats(),
                }
                return ok(status)

            if action == "clear":
                cleared_files = cache.clear()
                _LAST_WARMUP_STATE.update(
                    {
                        "last_warmup": None,
                        "stocks": [],
                        "lookback_days": None,
                        "include_financials": None,
                        "result": None,
                    }
                )
                return ok({"cleared": True, "cache_files_removed": int(cleared_files)})

            return fail(f"Unknown action: {action}")

        except Exception as e:
            return fail(str(e))
