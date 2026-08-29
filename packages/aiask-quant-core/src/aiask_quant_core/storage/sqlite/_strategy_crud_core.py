"""SQLite 策略超市 Mixin — CRUD / 静态工具 / 工厂 / 质量报告 / 领域事件"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

from .strategy_factory_json_budget import (
    bounded_json_text,
    strategy_json_field_max_bytes,
    strategy_params_max_bytes,
)


class _StrategyCrudCoreMixin:
        async def save_strategy(self, data: dict) -> dict:
            sid = str(data.get("id", "")).strip()
            if not sid:
                raise ValueError("strategy id is required")
            now = datetime.now(timezone.utc)
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO strategies (id, name, description, author_id, strategy_type, params, factor_weights, status, tags, backtest_artifact_id, subscriber_count, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, $11, $11)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        strategy_type = EXCLUDED.strategy_type,
                        params = EXCLUDED.params,
                        factor_weights = EXCLUDED.factor_weights,
                        tags = EXCLUDED.tags,
                        backtest_artifact_id = EXCLUDED.backtest_artifact_id,
                        updated_at = EXCLUDED.updated_at
                    """,
                    sid,
                    str(data.get("name", "")),
                    data.get("description"),
                    str(data.get("author_id", "default")),
                    str(data.get("strategy_type", "custom")),
                    bounded_json_text(
                        "strategies.params",
                        data.get("params") or {},
                        max_bytes=strategy_params_max_bytes(),
                    ),
                    bounded_json_text(
                        "strategies.factor_weights",
                        data.get("factor_weights") or {},
                        max_bytes=strategy_json_field_max_bytes(),
                    ),
                    str(data.get("status", "draft")),
                    list(data.get("tags") or []),
                    data.get("backtest_artifact_id"),
                    now,
                )
            return {**data, "id": sid, "created_at": now.isoformat(), "updated_at": now.isoformat()}

        async def get_strategy(self, strategy_id: str) -> Optional[dict]:
            sid = str(strategy_id or "").strip()
            if not sid:
                return None
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT s.*,
                           COALESCE((SELECT AVG(rating) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS avg_rating,
                           COALESCE((SELECT COUNT(*) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS review_count
                    FROM strategies s WHERE s.id = $1
                    """,
                    sid,
                )
            if not row:
                return None
            return self._decode_strategy_row(dict(row))

        async def list_strategies(self, status: Any = "listed", strategy_type: str = None, limit: int = 20, offset: int = 0) -> List[dict]:
            statuses = self._normalize_strategy_statuses(status)
            async with self.acquire() as conn:
                where_parts = ["1=1"]
                params: list[Any] = []
                idx = 1
                if statuses:
                    where_parts.append(f"s.status IN (${idx})")
                    params.append(statuses)
                    idx += 1
                if strategy_type:
                    where_parts.append(f"s.strategy_type = ${idx}")
                    params.append(strategy_type)
                    idx += 1
                params.extend([limit, offset])
                rows = await conn.fetch(
                    f"""
                    SELECT s.*,
                           COALESCE((SELECT AVG(rating) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS avg_rating
                    FROM strategies s
                    WHERE {" AND ".join(where_parts)}
                    ORDER BY s.updated_at DESC LIMIT ${idx} OFFSET ${idx + 1}
                    """,
                    *params,
                )
            return [self._decode_strategy_row(dict(r)) for r in rows]

        async def list_submitted_strategies_for_dedup(self, limit: int = 500, offset: int = 0) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           COALESCE((SELECT AVG(rating) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS avg_rating
                    FROM strategies s
                    WHERE s.status = 'submitted'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM strategy_incubation_accounts a
                          LEFT JOIN paper_accounts pa ON pa.id = a.account_id
                          WHERE a.strategy_id = s.id
                            AND a.status = 'active'
                            AND a.stage IN ('paper', 'warmup', 'observe')
                            AND COALESCE(pa.status, 'active') = 'active'
                            AND COALESCE(pa.account_type, 'incubation') = 'incubation'
                      )
                    ORDER BY s.updated_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit,
                    offset,
                )
            return [self._decode_strategy_row(dict(r)) for r in rows]

        async def list_paper_observation_strategies(self, limit: int = 50) -> List[dict]:
            """DEV-V1 P1: 列出所有 paper observation 候选策略.

            边界:
              - 只取 strategies.status = 'submitted'
              - 只取 strategy_incubation_accounts.stage = 'paper' AND status = 'active'
              - 反 EXISTS 排除已升 candidate / listed 的策略,避免重复处理
              - ORDER BY a.bound_at DESC,新进入 paper 的优先
              - LIMIT 通过参数化绑定,默认 50,toggle 可配
            """
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           a.account_id AS paper_account_id,
                           a.bound_at AS paper_bound_at
                    FROM strategies s
                    JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
                    WHERE s.status = 'submitted'
                      AND a.stage = 'paper'
                      AND a.status = 'active'
                      AND NOT EXISTS (
                          SELECT 1 FROM strategy_incubation_accounts a2
                          WHERE a2.strategy_id = s.id
                            AND a2.stage IN ('candidate', 'listed')
                            AND a2.status = 'active'
                      )
                    ORDER BY datetime(a.bound_at) DESC
                    LIMIT $1
                    """,
                    limit,
                )
            return [self._decode_strategy_row(dict(r)) for r in rows]

        async def list_active_paper_observation_strategies(self, limit: int = 50) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           a.account_id AS paper_account_id,
                           a.bound_at AS paper_bound_at,
                           a.stage AS observation_stage
                    FROM strategies s
                    JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
                    LEFT JOIN paper_accounts pa ON pa.id = a.account_id
                    WHERE s.status = 'submitted'
                      AND a.status = 'active'
                      AND a.stage IN ('paper', 'warmup', 'observe')
                      AND COALESCE(pa.status, 'active') = 'active'
                      AND COALESCE(pa.account_type, 'incubation') = 'incubation'
                      AND NOT EXISTS (
                          SELECT 1 FROM strategy_incubation_accounts a2
                          WHERE a2.strategy_id = s.id
                            AND a2.stage IN ('candidate', 'graduation_ready', 'listed', 'promoted')
                            AND a2.status = 'active'
                      )
                    ORDER BY
                        CASE a.stage
                            WHEN 'paper' THEN 0
                            WHEN 'warmup' THEN 1
                            WHEN 'observe' THEN 2
                            WHEN 'observe' THEN 2
                            ELSE 9
                        END,
                        datetime(COALESCE(a.updated_at, a.bound_at)) DESC
                    LIMIT $1
                    """,
                    limit,
                )
            return [self._decode_strategy_row(dict(r)) for r in rows]

        async def get_paper_observation_backlog_status(self, limit: int = 500) -> dict:
            """Return submitted + active stage=paper backlog and latest intake event."""
            _ = limit
            async with self.acquire() as conn:
                backlog = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS backlog_count,
                           MAX(datetime(a.bound_at)) AS latest_bound_at
                    FROM strategies s
                    JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
                    WHERE s.status = 'submitted'
                      AND a.stage = 'paper'
                      AND a.status = 'active'
                      AND NOT EXISTS (
                          SELECT 1 FROM strategy_incubation_accounts a2
                          WHERE a2.strategy_id = s.id
                            AND a2.stage IN ('candidate', 'listed')
                            AND a2.status = 'active'
                      )
                    """,
                )
                active_observation = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS active_count,
                           MAX(datetime(COALESCE(a.updated_at, a.bound_at))) AS latest_active_at
                    FROM strategies s
                    JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
                    LEFT JOIN paper_accounts pa ON pa.id = a.account_id
                    WHERE s.status = 'submitted'
                      AND a.status = 'active'
                      AND a.stage IN ('paper', 'warmup', 'observe')
                      AND COALESCE(pa.status, 'active') = 'active'
                      AND COALESCE(pa.account_type, 'incubation') = 'incubation'
                      AND NOT EXISTS (
                          SELECT 1 FROM strategy_incubation_accounts a2
                          WHERE a2.strategy_id = s.id
                            AND a2.stage IN ('candidate', 'graduation_ready', 'listed', 'promoted')
                            AND a2.status = 'active'
                      )
                    """,
                )
                latest_recognized = await conn.fetchrow(
                    """
                    SELECT event_type, created_at
                    FROM strategy_domain_events
                    WHERE event_type = 'incubation_factory.paper_observation_recognized'
                    ORDER BY datetime(created_at) DESC
                    LIMIT 1
                    """
                )
            backlog_count = int(dict(backlog or {}).get("backlog_count") or 0)
            latest_bound_at = dict(backlog or {}).get("latest_bound_at")
            active_count = int(dict(active_observation or {}).get("active_count") or 0)
            latest_active_at = dict(active_observation or {}).get("latest_active_at")
            latest_recognized_at = dict(latest_recognized or {}).get("created_at")
            return {
                "paper_observation_backlog_count": backlog_count,
                "paper_observation_backlog_status": "empty" if active_count <= 0 else "queued",
                "paper_observation_active_count": active_count,
                "paper_observation_latest_bound_at": str(latest_bound_at) if latest_bound_at else None,
                "paper_observation_latest_active_at": str(latest_active_at) if latest_active_at else None,
                "paper_observation_last_recognized_at": str(latest_recognized_at) if latest_recognized_at else None,
                "paper_observation_latest_event_type": dict(latest_recognized or {}).get("event_type"),
            }

        async def list_diagnostic_observation_strategies(self, limit: int = 5) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           a.account_id AS diagnostic_account_id,
                           a.bound_at AS diagnostic_bound_at
                    FROM strategies s
                    JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
                    WHERE s.status IN ('diagnostic', 'submitted')
                      AND a.stage = 'diagnostic'
                      AND a.status = 'active'
                      AND NOT EXISTS (
                          SELECT 1 FROM strategy_incubation_accounts a2
                          WHERE a2.strategy_id = s.id
                            AND a2.stage IN ('paper', 'candidate', 'listed', 'incubating', 'promoted')
                            AND a2.status = 'active'
                      )
                    ORDER BY datetime(a.bound_at) DESC
                    LIMIT $1
                    """,
                    max(1, min(int(limit or 5), 50)),
                )
            return [self._decode_strategy_row(dict(r)) for r in rows]

        async def find_active_diagnostic_observation_by_fingerprint(
            self,
            fingerprint: str,
            ttl_days: int = 7,
        ) -> Optional[dict]:
            token = str(fingerprint or "").strip()
            if not token:
                return None
            try:
                ttl = max(1, min(int(ttl_days or 7), 30))
            except Exception:
                ttl = 7
            cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl)).strftime("%Y-%m-%d %H:%M:%S")
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           a.account_id AS diagnostic_account_id,
                           a.bound_at AS diagnostic_bound_at,
                           a.metadata AS diagnostic_account_metadata
                    FROM strategies s
                    JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
                    WHERE s.status IN ('diagnostic', 'submitted')
                      AND a.stage = 'diagnostic'
                      AND a.status = 'active'
                      AND datetime(COALESCE(a.bound_at, a.updated_at)) >= datetime($1)
                    ORDER BY datetime(a.bound_at) DESC
                    LIMIT 200
                    """,
                    cutoff,
                )
            for row in rows:
                payload = dict(row)
                strategy = self._decode_strategy_row(payload)
                params = dict(strategy.get("params") or {})
                account_metadata = self._decode_json_field(
                    payload.get("diagnostic_account_metadata"),
                    {},
                )
                if (
                    str(params.get("diagnostic_fingerprint") or "").strip() == token
                    or str((account_metadata or {}).get("diagnostic_fingerprint") or "").strip() == token
                    or str(dict((account_metadata or {}).get("trace") or {}).get("diagnostic_fingerprint") or "").strip() == token
                ):
                    if account_metadata:
                        strategy["diagnostic_account_metadata"] = account_metadata
                    return strategy
            return None

        async def get_incubation_factory_health(self, max_age_hours: int = 24) -> dict:
            try:
                max_age = max(1, min(int(max_age_hours or 24), 168))
            except Exception:
                max_age = 24
            event_types = [
                "incubation_factory.heartbeat",
                "incubation_factory.hit_rate_report_generated",
                "incubation_factory.paper_observation_recognized",
                "incubation_factory.diagnostic_observation_processed",
                "incubation.metric_recorded",
            ]
            async with self.acquire() as conn:
                latest_event = await conn.fetchrow(
                    """
                    SELECT event_type, created_at
                    FROM strategy_domain_events
                    WHERE event_type IN ($1)
                    ORDER BY datetime(created_at) DESC
                    LIMIT 1
                    """,
                    event_types,
                )
                latest_metric = await conn.fetchrow(
                    """
                    SELECT created_at
                    FROM strategy_incubation_metrics
                    ORDER BY datetime(created_at) DESC
                    LIMIT 1
                    """
                )

            def _normalize_ts(value: Any) -> Optional[datetime]:
                resolved = self._coerce_timestamp(value)
                if resolved is None:
                    return None
                if resolved.tzinfo is None:
                    return resolved.replace(tzinfo=timezone.utc)
                return resolved.astimezone(timezone.utc)

            latest_event_at = _normalize_ts(dict(latest_event or {}).get("created_at"))
            latest_metric_at = _normalize_ts(dict(latest_metric or {}).get("created_at"))
            candidates = [item for item in (latest_event_at, latest_metric_at) if item is not None]
            latest_activity_at = max(candidates) if candidates else None
            cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=max_age)
            healthy = latest_activity_at is not None and latest_activity_at >= cutoff_dt
            stale_reason = None
            if latest_activity_at is None:
                stale_reason = "no_incubation_activity"
            elif not healthy:
                stale_reason = "incubation_activity_stale"
            return {
                "healthy": healthy,
                "max_age_hours": max_age,
                "latest_activity_at": latest_activity_at.isoformat() if latest_activity_at else None,
                "latest_event_at": latest_event_at.isoformat() if latest_event_at else None,
                "latest_event_type": dict(latest_event or {}).get("event_type"),
                "latest_metric_at": latest_metric_at.isoformat() if latest_metric_at else None,
                "stale_reason": stale_reason,
            }

        async def update_strategy_status(
            self,
            strategy_id: str,
            status: str,
            actor_id: str = "system",
            reason: Optional[str] = None,
            metadata: Optional[dict] = None,
        ) -> None:
            now = datetime.now(timezone.utc)
            async with self.acquire() as conn:
                row = await conn.fetchrow("SELECT status FROM strategies WHERE id = $1", strategy_id)
                if not row:
                    return
                from_status = row["status"]
                await conn.execute(
                    "UPDATE strategies SET status = $1, updated_at = $2 WHERE id = $3",
                    status, now, strategy_id,
                )
                if from_status != status:
                    metadata_payload = metadata if isinstance(metadata, dict) else {}
                    encoded_metadata = bounded_json_text(
                        "strategy_status_events.metadata",
                        metadata_payload,
                        max_bytes=strategy_json_field_max_bytes(),
                    )
                    domain_payload = {
                        "from_status": from_status,
                        "to_status": status,
                        "reason": reason,
                        "metadata": metadata_payload,
                    }
                    await conn.execute(
                        """
                        INSERT INTO strategy_status_events
                            (strategy_id, from_status, to_status, event_type, actor_id, reason, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        strategy_id,
                        from_status,
                        status,
                        "status_change",
                        actor_id,
                        reason,
                        encoded_metadata,
                    )
                    await conn.execute(
                        """
                        INSERT INTO strategy_domain_events
                            (strategy_id, aggregate_type, aggregate_id, event_type, source, severity, correlation_id, payload, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        strategy_id,
                        "strategy",
                        strategy_id,
                        "strategy.status_changed",
                        actor_id or "system",
                        "info",
                        metadata_payload.get("task_run_id"),
                        bounded_json_text(
                            "strategy_domain_events.payload",
                            domain_payload,
                            max_bytes=strategy_json_field_max_bytes(),
                        ),
                        now,
                    )

        async def save_strategy_metrics(self, strategy_id: str, period: str, metrics: dict) -> None:
            now = datetime.now(timezone.utc)
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO strategy_metrics (strategy_id, period, total_return, annual_return, sharpe_ratio, max_drawdown, win_rate, calmar_ratio, trade_count, computed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (strategy_id, period) DO UPDATE SET
                        total_return = EXCLUDED.total_return,
                        annual_return = EXCLUDED.annual_return,
                        sharpe_ratio = EXCLUDED.sharpe_ratio,
                        max_drawdown = EXCLUDED.max_drawdown,
                        win_rate = EXCLUDED.win_rate,
                        calmar_ratio = EXCLUDED.calmar_ratio,
                        trade_count = EXCLUDED.trade_count,
                        computed_at = EXCLUDED.computed_at
                    """,
                    strategy_id, period,
                    metrics.get("total_return"), metrics.get("annual_return"),
                    metrics.get("sharpe_ratio"), metrics.get("max_drawdown"),
                    metrics.get("win_rate"), metrics.get("calmar_ratio"),
                    metrics.get("trade_count"), now,
                )

        async def get_strategy_metrics(self, strategy_id: str) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM strategy_metrics WHERE strategy_id = $1 ORDER BY period",
                    strategy_id,
                )
            return [dict(r) for r in rows]

        async def save_review(self, strategy_id: str, user_id: str, rating: int, comment: str = None) -> None:
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO strategy_reviews (strategy_id, user_id, rating, comment)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (strategy_id, user_id) DO UPDATE SET
                        rating = EXCLUDED.rating,
                        comment = EXCLUDED.comment,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    strategy_id, user_id, rating, comment,
                )

        async def get_reviews(self, strategy_id: str, limit: int = 20) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM strategy_reviews WHERE strategy_id = $1 ORDER BY created_at DESC LIMIT $2",
                    strategy_id, limit,
                )
            return [dict(r) for r in rows]

        async def subscribe_strategy(self, strategy_id: str, user_id: str) -> None:
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO strategy_subscriptions (strategy_id, user_id, status)
                    VALUES ($1, $2, 'active')
                    ON CONFLICT (strategy_id, user_id) DO UPDATE SET status = 'active', subscribed_at = CURRENT_TIMESTAMP
                    """,
                    strategy_id, user_id,
                )
                await conn.execute(
                    "UPDATE strategies SET subscriber_count = (SELECT COUNT(*) FROM strategy_subscriptions WHERE strategy_id = $1 AND status = 'active') WHERE id = $1",
                    strategy_id,
                )

        async def unsubscribe_strategy(self, strategy_id: str, user_id: str) -> None:
            async with self.acquire() as conn:
                await conn.execute(
                    "UPDATE strategy_subscriptions SET status = 'cancelled' WHERE strategy_id = $1 AND user_id = $2",
                    strategy_id, user_id,
                )
                await conn.execute(
                    "UPDATE strategies SET subscriber_count = (SELECT COUNT(*) FROM strategy_subscriptions WHERE strategy_id = $1 AND status = 'active') WHERE id = $1",
                    strategy_id,
                )

        async def list_user_subscriptions(self, user_id: str) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*, ss.subscribed_at,
                           COALESCE((SELECT AVG(rating) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS avg_rating
                    FROM strategy_subscriptions ss
                    JOIN strategies s ON s.id = ss.strategy_id
                    WHERE ss.user_id = $1 AND ss.status = 'active'
                    ORDER BY ss.subscribed_at DESC
                    """,
                    user_id,
                )
            return [self._decode_strategy_row(dict(r)) for r in rows]

        async def list_user_strategies(
            self,
            user_id: str,
            *,
            include_archived: bool = False,
            limit: int = 50,
            offset: int = 0,
        ) -> List[dict]:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.*,
                           COALESCE((SELECT AVG(rating) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS avg_rating,
                           COALESCE((SELECT COUNT(*) FROM strategy_reviews WHERE strategy_id = s.id), 0) AS review_count
                    FROM strategies s
                    WHERE s.author_id = $1
                      AND ($2 OR s.status <> 'archived')
                    ORDER BY s.updated_at DESC
                    LIMIT $3 OFFSET $4
                    """,
                    user_id,
                    bool(include_archived),
                    max(1, min(int(limit or 50), 200)),
                    max(0, int(offset or 0)),
                )
            return [self._decode_strategy_row(dict(row)) for row in rows]

        async def update_strategy_fields(self, strategy_id: str, updates: dict) -> Optional[dict]:
            payload = dict(updates or {})
            if not payload:
                return await self.get_strategy(strategy_id)
            assignments: list[str] = []
            values: list[Any] = []
            allowed_fields = (
                "name",
                "description",
                "params",
                "factor_weights",
                "tags",
                "backtest_artifact_id",
            )
            for field in allowed_fields:
                if field not in payload:
                    continue
                idx = len(values) + 2
                if field == "params":
                    assignments.append(f"{field} = ${idx}")
                    values.append(
                        bounded_json_text(
                            "strategies.params",
                            payload.get(field) or {},
                            max_bytes=strategy_params_max_bytes(),
                        )
                    )
                    continue
                if field == "factor_weights":
                    assignments.append(f"{field} = ${idx}")
                    values.append(
                        bounded_json_text(
                            "strategies.factor_weights",
                            payload.get(field) or {},
                            max_bytes=strategy_json_field_max_bytes(),
                        )
                    )
                    continue
                if field == "tags":
                    assignments.append(f"{field} = ${idx}[]")
                    values.append(list(payload.get(field) or []))
                    continue
                assignments.append(f"{field} = ${idx}")
                values.append(payload.get(field))
            if not assignments:
                return await self.get_strategy(strategy_id)
            values.append(datetime.now(timezone.utc))
            updated_at_idx = len(values) + 1
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    UPDATE strategies
                    SET {", ".join(assignments)},
                        updated_at = ${updated_at_idx}
                    WHERE id = $1
                    RETURNING *
                    """,
                    strategy_id,
                    *values,
                )
            return self._decode_strategy_row(dict(row)) if row else None

        async def save_strategy_paper_session(self, session: dict) -> dict:
            payload = dict(session or {})
            session_id = str(payload.get("id") or f"sps_{uuid4().hex[:16]}").strip()
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO strategy_paper_sessions
                        (id, strategy_id, user_id, account_id, session_type, source_strategy_id,
                         created_at, updated_at, last_used_at)
                    VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, COALESCE($7, CURRENT_TIMESTAMP))
                    ON CONFLICT (user_id, strategy_id, session_type) DO UPDATE SET
                        account_id = EXCLUDED.account_id,
                        source_strategy_id = EXCLUDED.source_strategy_id,
                        updated_at = CURRENT_TIMESTAMP,
                        last_used_at = COALESCE(EXCLUDED.last_used_at, strategy_paper_sessions.last_used_at, CURRENT_TIMESTAMP)
                    RETURNING *
                    """,
                    session_id,
                    str(payload.get("strategy_id") or ""),
                    str(payload.get("user_id") or "default"),
                    str(payload.get("account_id") or ""),
                    str(payload.get("session_type") or "personal_paper"),
                    payload.get("source_strategy_id"),
                    self._coerce_timestamp(payload.get("last_used_at")),
                )
            return dict(row)

        async def get_strategy_paper_session(
            self,
            strategy_id: str,
            user_id: str,
            session_type: str = "personal_paper",
        ) -> Optional[dict]:
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT sps.*, pa.name AS account_name, pa.status AS account_status,
                           pa.account_type, pa.strategy_id AS account_strategy_id
                    FROM strategy_paper_sessions sps
                    LEFT JOIN paper_accounts pa ON pa.id = sps.account_id
                    WHERE sps.strategy_id = $1 AND sps.user_id = $2 AND sps.session_type = $3
                    ORDER BY sps.updated_at DESC, sps.created_at DESC
                    LIMIT 1
                    """,
                    strategy_id,
                    user_id,
                    session_type,
                )
            return dict(row) if row else None

        async def touch_strategy_paper_session(
            self,
            strategy_id: str,
            user_id: str,
            session_type: str = "personal_paper",
        ) -> Optional[dict]:
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE strategy_paper_sessions
                    SET last_used_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE strategy_id = $1 AND user_id = $2 AND session_type = $3
                    RETURNING *
                    """,
                    strategy_id,
                    user_id,
                    session_type,
                )
            return dict(row) if row else None

        async def save_strategy_lineage(self, strategy_id: str, parent_id: Optional[str],
                                         spawn_reason: str, birth_regime: dict) -> None:
            async with self.acquire() as conn:
                await conn.execute(
                    """INSERT INTO strategy_lineage (strategy_id, parent_id, spawn_reason, birth_regime)
                       VALUES ($1, $2, $3, $4)""",
                    strategy_id, parent_id, spawn_reason,
                    json.dumps(birth_regime, ensure_ascii=False, default=str),
                )

        async def save_elimination_log(self, strategy_id: str, elimination_date, red_flags: list,
                                        reason: str) -> None:
            async with self.acquire() as conn:
                await conn.execute(
                    """INSERT INTO strategy_elimination_log (strategy_id, elimination_date, red_flags, reason)
                       VALUES ($1, $2, $3, $4)""",
                    strategy_id, elimination_date,
                    json.dumps(red_flags, ensure_ascii=False, default=str), reason,
                )
