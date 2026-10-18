"""A 股交易日历：节假日感知 + 调休日支持。

提供 is_trading_day() 判断，优先使用 cn-stock-holidays 或 pandas_market_calendars，
不可用时降级为简单的工作日判断。
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional, Set

logger = logging.getLogger(__name__)

# 2025-2026 中国 A 股已知节假日（手动维护作为 fallback）
# 来源：上交所/深交所公告
_KNOWN_HOLIDAYS: Set[date] = {
    # 2025 元旦
    date(2025, 1, 1),
    # 2025 春节
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 1), date(2025, 2, 2), date(2025, 2, 3), date(2025, 2, 4),
    # 2025 清明
    date(2025, 4, 4), date(2025, 4, 5), date(2025, 4, 6),
    # 2025 劳动节
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3), date(2025, 5, 4), date(2025, 5, 5),
    # 2025 端午
    date(2025, 5, 31), date(2025, 6, 1), date(2025, 6, 2),
    # 2025 中秋+国庆
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3), date(2025, 10, 4),
    date(2025, 10, 5), date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8),
    # 2026 元旦
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    # 2026 春节
    date(2026, 2, 15), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22), date(2026, 2, 23),
    # 2026 清明
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    # 2026 劳动节
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4), date(2026, 5, 5),
    # 2026 端午
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 2026 中秋(09-25)。国庆区间到 10-07 为止：10-08 是复市首日，
    # 由当日 5,561 条 bar 与交易所融资融券数据双证，此前误列一天。
    date(2026, 9, 25),
    # 2026 中秋+国庆
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4),
    date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), }

# 调休工作日（周末但需要上班的日子）
_KNOWN_WORKDAYS: Set[date] = {
    # 2025 春节调休
    date(2025, 1, 26), date(2025, 2, 8),
    # 2025 劳动节调休
    date(2025, 4, 27),
    # 2025 国庆调休
    date(2025, 9, 28), date(2025, 10, 11),
    # 2026 春节调休
    date(2026, 2, 14), date(2026, 2, 28),
    # 2026 国庆调休
    date(2026, 9, 27), date(2026, 10, 10),
}


class TradingCalendar:
    """A 股交易日历，支持多种数据源。"""

    def __init__(self):
        self._external_calendar = None
        self._holidays = set(_KNOWN_HOLIDAYS)
        self._workdays = set(_KNOWN_WORKDAYS)
        self._try_load_external()

    def _try_load_external(self) -> None:
        """尝试加载外部交易日历库。"""
        # 尝试 cn-stock-holidays
        try:
            import cn_stock_holidays.data as csh_data
            self._external_calendar = "cn_stock_holidays"
            logger.info("TradingCalendar: using cn-stock-holidays")
            return
        except ImportError as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/domain/trading_calendar.py:82: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

        # 尝试 pandas_market_calendars
        try:
            import pandas_market_calendars as mcal
            self._external_calendar = mcal.get_calendar("SSE")
            logger.info("TradingCalendar: using pandas_market_calendars (SSE)")
            return
        except (ImportError, Exception) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/domain/trading_calendar.py:91: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

        logger.info("TradingCalendar: using built-in holiday list (fallback)")

    def is_trading_day(self, d: date) -> bool:
        """判断给定日期是否为 A 股交易日。"""
        # 优先使用外部库
        if self._external_calendar == "cn_stock_holidays":
            try:
                import cn_stock_holidays.data as csh_data
                holidays = set(csh_data.get_holidays())
                return d not in holidays and (d.weekday() < 5 or d in self._workdays)
            except Exception as _swallowed:
                logger.warning("packages/strategy-factory/src/strategy_factory/domain/trading_calendar.py:104: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass

        if self._external_calendar is not None and hasattr(self._external_calendar, "valid_days"):
            try:
                import pandas as pd
                valid = self._external_calendar.valid_days(
                    pd.Timestamp(d), pd.Timestamp(d)
                )
                return len(valid) > 0
            except Exception as _swallowed:
                logger.warning("packages/strategy-factory/src/strategy_factory/domain/trading_calendar.py:114: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass

        # Fallback：使用内置节假日列表。
        #
        # A-share trading days are weekdays minus holidays, and that is all. This
        # used to return True for a 调休 workday, on the reading that a Saturday the
        # government designates as a working day is a session. It is not: the
        # exchanges stay shut. Measured against the stored calendar, no Saturday or
        # Sunday in 2026 carries a bar, and the exchange's own margin feed published
        # nothing for 2026-10-10 or 2026-09-27, both of which this returned as
        # trading days — so the factory would have run on closed days and, on
        # 2026-10-08, refused to run on an open one.
        return d.weekday() < 5 and d not in self._holidays

    def next_trading_day(self, d: date) -> date:
        """返回给定日期之后的下一个交易日。"""
        candidate = d + timedelta(days=1)
        max_search = 15  # 最长假期不超过 15 天
        for _ in range(max_search):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate


# 全局单例
_trading_calendar: Optional[TradingCalendar] = None


def get_trading_calendar() -> TradingCalendar:
    """获取交易日历单例。"""
    global _trading_calendar
    if _trading_calendar is None:
        _trading_calendar = TradingCalendar()
    return _trading_calendar
