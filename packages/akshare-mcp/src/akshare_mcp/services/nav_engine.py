"""NAV 引擎 — 每日收盘后计算模拟账户净值快照。"""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, time, timedelta, date
from typing import Optional

logger = logging.getLogger(__name__)

# 每日 15:30 CST 计算 NAV
NAV_RUN_TIME = time(15, 30)


class NavEngine:
    """每日 NAV 快照计算引擎"""

    def __init__(self, run_time: time = NAV_RUN_TIME):
        self.run_time = run_time
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="nav-engine")
        logger.info("[NavEngine] started, daily run at %s", self.run_time)

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def shutdown(self, grace_sec: float = 3.0):
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            logger.info("[NavEngine] stopped")
            return
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, grace_sec))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        else:
            with suppress(asyncio.CancelledError):
                await task
        logger.info("[NavEngine] stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "run_time": str(self.run_time),
            "last_run": str(self.last_run) if self.last_run else None,
            "last_result": self.last_result,
        }

    async def _loop(self):
        while self._running:
            try:
                now = datetime.now()
                target = datetime.combine(now.date(), self.run_time)
                if target <= now:
                    target += timedelta(days=1)
                # 跳过周末
                while target.weekday() >= 5:
                    target += timedelta(days=1)
                wait = (target - now).total_seconds()
                logger.info("[NavEngine] next run in %.0fs at %s", wait, target)
                await asyncio.sleep(wait)
                if self._running:
                    await self.compute_nav()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[NavEngine] loop error: %s", e, exc_info=True)
                await asyncio.sleep(120)

    async def compute_nav(self):
        """计算所有账户的当日 NAV 快照"""
        from ..storage import get_db
        db = get_db()
        today = date.today()
        computed = 0
        errors = 0

        async with db.acquire() as conn:
            accounts = await conn.fetch("SELECT * FROM paper_accounts")

        for acct in accounts:
            try:
                account_id = acct['id']
                await self._compute_account_nav(db, account_id, today)
                computed += 1
            except Exception as e:
                errors += 1
                logger.warning("[NavEngine] account %s error: %s", acct['id'], e)

        self.last_run = datetime.now()
        self.last_result = {"date": str(today), "computed": computed, "errors": errors}
        logger.info("[NavEngine] done: %d computed, %d errors", computed, errors)
        return self.last_result

    async def _compute_account_nav(self, db, account_id: str, nav_date: date):
        """计算单个账户的 NAV"""
        async with db.acquire() as conn:
            account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id=$1", account_id)
            if not account:
                return

            positions = await conn.fetch(
                "SELECT * FROM paper_positions WHERE account_id=$1", account_id
            )

            # 更新持仓现价
            market_value = 0.0
            for pos in positions:
                code = pos['stock_code']
                qty = int(pos.get('quantity') or 0)
                current = await self._get_price(code)
                if current and current > 0:
                    mv = current * qty
                    market_value += mv
                    await conn.execute(
                        """UPDATE paper_positions SET current_price=$1, market_value=$2,
                           profit_rate=CASE WHEN cost_price>0 THEN ($1-cost_price)/cost_price ELSE 0 END,
                           updated_at=CURRENT_TIMESTAMP
                           WHERE account_id=$3 AND stock_code=$4""",
                        current, mv, account_id, code
                    )
                else:
                    market_value += float(pos.get('market_value') or 0)

            cash = float(account.get('current_capital') or 0)
            total = cash + market_value

            # 更新账户总资产
            await conn.execute(
                "UPDATE paper_accounts SET total_value=$1, updated_at=CURRENT_TIMESTAMP WHERE id=$2",
                total, account_id
            )

            # 获取前一日 NAV 计算日收益率
            prev = await conn.fetchrow(
                "SELECT total_value FROM paper_nav WHERE account_id=$1 AND nav_date<$2 ORDER BY nav_date DESC LIMIT 1",
                account_id, nav_date
            )
            prev_val = float(prev['total_value']) if prev else float(account.get('initial_capital') or total)
            daily_return = (total - prev_val) / prev_val if prev_val > 0 else 0.0

            # UPSERT NAV 记录
            await conn.execute(
                """INSERT INTO paper_nav (account_id, nav_date, total_value, cash, market_value, daily_return, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,CURRENT_TIMESTAMP)
                   ON CONFLICT (account_id, nav_date) DO UPDATE
                   SET total_value=$3, cash=$4, market_value=$5, daily_return=$6""",
                account_id, nav_date, total, cash, market_value, daily_return
            )

    async def _get_price(self, code: str) -> Optional[float]:
        try:
            from .market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_response
            from ..utils import normalize_code
            res = await get_quote_snapshot_response(normalize_code(code), fallback_mode=FALLBACK_DB_ONLY)
            if res and res.get('success') and res.get('data'):
                d = res['data']
                return d.get('price') or d.get('close')
        except Exception:
            pass
        return None


_nav_engine: Optional[NavEngine] = None


def get_nav_engine() -> NavEngine:
    global _nav_engine
    if _nav_engine is None:
        _nav_engine = NavEngine()
    return _nav_engine
