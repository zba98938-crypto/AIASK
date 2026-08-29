"""动态 KYC 适当性评估服务"""
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 等级约束
KYC_LEVELS = {
    'C1': {'max_score': 25, 'max_drawdown': 0.05, 'label': '保守型'},
    'C2': {'max_score': 45, 'max_drawdown': 0.10, 'label': '稳健型'},
    'C3': {'max_score': 65, 'max_drawdown': 0.20, 'label': '平衡型'},
    'C4': {'max_score': 80, 'max_drawdown': 0.35, 'label': '进取型'},
    'C5': {'max_score': 100, 'max_drawdown': 1.00, 'label': '激进型'},
}


class KycDynamic:
    """动态 KYC 适当性评估"""

    async def assess_risk_level(self, user_id: str, db) -> dict:
        """综合评分 → KYC 等级

        评分构成：
        - 60% 交易行为评分（频率、金额、止损行为）
        - 30% 画像评分（user_profile_snapshots）
        - 10% 问卷基线（users.settings.risk_score）
        """
        trade_score = await self._trade_behavior_score(user_id, db)
        profile_score = await self._profile_score(user_id, db)
        baseline_score = await self._questionnaire_baseline(user_id, db)

        composite = trade_score * 0.6 + profile_score * 0.3 + baseline_score * 0.1

        # 评分→等级映射
        level = 'C5'
        for lv in ['C1', 'C2', 'C3', 'C4', 'C5']:
            if composite < KYC_LEVELS[lv]['max_score']:
                level = lv
                break

        # 30天稳定性保护：等级上调需30天稳定行为数据
        current_level = await self._get_current_level(user_id, db)
        level_order = ['C1', 'C2', 'C3', 'C4', 'C5']
        if current_level and level_order.index(level) > level_order.index(current_level):
            has_stable = await self._has_stable_history(user_id, db, days=30)
            if not has_stable:
                level = current_level  # 维持当前等级

        return {
            'user_id': user_id,
            'composite_score': round(composite, 2),
            'kyc_level': level,
            'label': KYC_LEVELS[level]['label'],
            'max_drawdown': KYC_LEVELS[level]['max_drawdown'],
            'components': {
                'trade_behavior': round(trade_score, 2),
                'profile': round(profile_score, 2),
                'questionnaire': round(baseline_score, 2),
            },
        }

    async def _trade_behavior_score(self, user_id: str, db) -> float:
        """分析 paper_trades 的频率、金额、止损行为 → 0~100"""
        try:
            async with db.acquire() as conn:
                # 近90天交易统计
                rows = await conn.fetch(
                    """SELECT trade_type, price, quantity, amount, reason
                       FROM paper_trades pt
                       JOIN paper_accounts pa ON pt.account_id = pa.id
                       WHERE pa.user_id = $1
                         AND pt.trade_time > datetime(CURRENT_TIMESTAMP, '-90 days')
                       ORDER BY pt.trade_time DESC
                       LIMIT 200""",
                    user_id,
                )
            if not rows:
                return 25.0  # 无交易记录 → 保守

            total_trades = len(rows)
            sell_trades = [r for r in rows if r['trade_type'] == 'sell']
            stop_loss_count = sum(1 for r in sell_trades if r.get('reason') and '止损' in str(r['reason']))

            # 交易频率得分（越频繁越激进）
            freq_score = min(100, total_trades * 1.5)
            # 止损纪律得分（有止损 → 更理性，得分偏低）
            if sell_trades:
                stop_ratio = stop_loss_count / len(sell_trades)
                discipline_score = max(0, 50 - stop_ratio * 30)
            else:
                discipline_score = 30

            return (freq_score + discipline_score) / 2.0
        except Exception as e:
            logger.warning("trade_behavior_score error: %s", e)
            return 25.0

    async def _profile_score(self, user_id: str, db) -> float:
        """从 user_profile_snapshots 读取最新画像 → 0~100"""
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT neuroticism, openness, herd_tendency, greed_fear_axis
                       FROM user_profile_snapshots
                       WHERE user_id = $1
                       ORDER BY created_at DESC LIMIT 1""",
                    user_id,
                )
            if not row:
                return 50.0

            # 高神经质 → 保守（低分），高贪婪 → 激进（高分）
            n = float(row['neuroticism'] or 0.5)
            gfa = float(row['greed_fear_axis'] or 0.0)
            herd = float(row['herd_tendency'] or 0.5)

            score = 50.0 + gfa * 30.0 - (n - 0.5) * 20.0 + (herd - 0.5) * 10.0
            return max(0.0, min(100.0, score))
        except Exception as e:
            logger.warning("profile_score error: %s", e)
            return 50.0

    async def _questionnaire_baseline(self, user_id: str, db) -> float:
        """从 users.settings 读取初始问卷分数 → 0~100"""
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT settings FROM users WHERE id = $1", user_id,
                )
            if not row or not row['settings']:
                return 50.0
            settings = row['settings'] if isinstance(row['settings'], dict) else json.loads(row['settings'])
            return float(settings.get('risk_score', 50))
        except Exception as e:
            logger.warning("questionnaire_baseline error: %s", e)
            return 50.0

    async def _get_current_level(self, user_id: str, db) -> str | None:
        """获取当前 KYC 等级"""
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT settings FROM users WHERE id = $1", user_id,
                )
            if not row or not row['settings']:
                return None
            settings = row['settings'] if isinstance(row['settings'], dict) else json.loads(row['settings'])
            return settings.get('kyc_level')
        except Exception:
            return None

    async def _has_stable_history(self, user_id: str, db, days: int = 30) -> bool:
        """检查是否有足够的稳定行为数据"""
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT COUNT(*) as cnt
                       FROM paper_trades pt
                       JOIN paper_accounts pa ON pt.account_id = pa.id
                       WHERE pa.user_id = $1
                         AND pt.trade_time > datetime(CURRENT_TIMESTAMP, '-' || $2 || ' days')""",
                    user_id, int(days),
                )
            return row and int(row['cnt']) >= 5
        except Exception:
            return False


kyc_service = KycDynamic()
