"""孵化工厂 · 自动识别与接纳模块。

负责扫描策略工厂新产出的合格策略，自动创建孵化账户并绑定。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from aiask_quant_core.strategy_explanation import build_strategy_explanation

logger = logging.getLogger(__name__)

_GRADE_RANKS: dict[str, int] = {
    "D": 0,
    "C": 1,
    "B": 2,
    "A": 3,
    "S": 4,
    "SS": 5,
    "SSS": 6,
}


def _resolve_db_async_method(db: Any, name: str) -> Any:
    try:
        method = getattr(db, name)
    except Exception:
        return None
    if not callable(method):
        return None
    if type(method).__module__ == "unittest.mock" and name not in getattr(db, "__dict__", {}):
        return None
    return method


class IncubationIntake:
    """自动识别和接纳策略工厂产出的新策略。"""

    @staticmethod
    def _strategy_explanation(
        payload: dict[str, Any],
        *,
        source: str,
        metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        try:
            return build_strategy_explanation(
                payload,
                metrics=metrics,
                source=source,
            )
        except Exception:
            return {}

    async def scan_and_accept(self, db: Any) -> dict[str, Any]:
        """
        扫描 status='incubating' 且尚未绑定孵化账户的策略，自动接纳。

        识别条件：
        1. status = 'incubating'（策略工厂 Gate-3 通过后设置）
        2. 尚未有 incubation_account 绑定
        3. 有有效的策略记录

        接纳动作：
        1. 确认策略有效
        2. 创建 incubation_account 记录
        3. 初始化 pipeline_snapshot（stage='warmup'）
        4. 记录 domain_event（incubation.strategy_accepted）
        """
        from ..incubation import get_strategy_incubation_service

        incubation_service = get_strategy_incubation_service()

        # 加载所有 incubating 状态的策略
        incubating = await self._list_incubating_strategies(db)
        if not incubating:
            # === DEV-V1 P1: 即使 incubating 为空,也要走 paper observation 通道 ===
            # 不再 early return,继续走下面的 paper 处理段。
            result = {
                "scanned": 0,
                "accepted": 0,
                "skipped": 0,
                "errors": 0,
                "details": [],
            }
            paper_candidates = await self._list_paper_observation_strategies(db)
            paper_recognized = 0
            for strategy in paper_candidates:
                sid = str(strategy.get("id") or "").strip()
                if not sid:
                    continue
                try:
                    await self._record_paper_intake_event(db, strategy)
                    paper_recognized += 1
                except Exception as exc:
                    logger.warning(
                        "IncubationIntake: paper intake event failed for %s: %s", sid, exc,
                    )
            result["paper_observation_intake"] = {
                "scanned": len(paper_candidates),
                "recognized": paper_recognized,
                "strategy_ids": [str(s.get("id") or "") for s in paper_candidates],
            }
            result["diagnostic_observation_intake"] = await self._recognize_diagnostic_observation(
                db,
                no_incubating=True,
            )
            result["gate3_record_only_audit"] = await self._recognize_gate3_record_only_candidates(
                db,
                no_incubating=True,
            )
            if paper_candidates:
                logger.info(
                    "IncubationIntake: paper observation recognized %d/%d candidates (no incubating)",
                    paper_recognized,
                    len(paper_candidates),
                )
            return result

        accepted: list[dict[str, Any]] = []
        skipped = 0
        errors = 0

        for strategy in incubating:
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                skipped += 1
                continue

            try:
                # 检查是否已有孵化账户
                existing_account = None
                if hasattr(db, "get_strategy_incubation_account"):
                    existing_account = await db.get_strategy_incubation_account(sid)

                if existing_account:
                    skipped += 1
                    continue

                # 自动创建孵化账户
                ensure_result = await incubation_service.ensure_account(
                    db,
                    strategy,
                    stage="warmup",
                    source_run_id="incubation_factory_intake",
                )

                account = dict(ensure_result.get("account") or {})
                explanation = self._strategy_explanation(
                    strategy,
                    source="incubation_factory_intake",
                )
                accepted.append({
                    "strategy_id": sid,
                    "strategy_name": strategy.get("name"),
                    "strategy_type": strategy.get("strategy_type"),
                    "account_id": account.get("account_id") or account.get("id"),
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                    "strategy_explanation": explanation,
                })

                # 记录领域事件
                await self._record_acceptance_event(db, strategy, account)

                logger.info(
                    "IncubationIntake: accepted strategy %s (%s)",
                    sid,
                    strategy.get("name"),
                )

            except Exception as exc:
                errors += 1
                logger.warning(
                    "IncubationIntake: failed to accept strategy %s: %s",
                    sid,
                    exc,
                )

        result = {
            "scanned": len(incubating),
            "accepted": len(accepted),
            "skipped": skipped,
            "errors": errors,
            "details": accepted,
        }

        if accepted:
            logger.info(
                "IncubationIntake: accepted %d new strategies (scanned=%d, skipped=%d, errors=%d)",
                len(accepted),
                len(incubating),
                skipped,
                errors,
            )

        # === DEV-V1 P1: paper observation 通道 ===
        # 默认开启,让已创建 paper account 的 observe 样本进入孵化消费;
        # 设置 INCUBATION_FACTORY_PAPER_INTAKE_ENABLED=0 时返回空列表。
        paper_candidates = await self._list_paper_observation_strategies(db)
        paper_recognized = 0
        for strategy in paper_candidates:
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                continue
            try:
                # paper observation 策略已经由 _enqueue_paper_observation 创建账户,
                # 这里只追加 domain event 标记 intake 已经识别它进入孵化处理。
                await self._record_paper_intake_event(db, strategy)
                paper_recognized += 1
            except Exception as exc:
                logger.warning(
                    "IncubationIntake: paper intake event failed for %s: %s", sid, exc,
                )
        result["paper_observation_intake"] = {
            "scanned": len(paper_candidates),
            "recognized": paper_recognized,
            "strategy_ids": [str(s.get("id") or "") for s in paper_candidates],
        }
        result["diagnostic_observation_intake"] = await self._recognize_diagnostic_observation(db)
        result["gate3_record_only_audit"] = await self._recognize_gate3_record_only_candidates(db)
        if paper_candidates:
            logger.info(
                "IncubationIntake: paper observation recognized %d/%d candidates",
                paper_recognized,
                len(paper_candidates),
            )

        return result

    async def _list_incubating_strategies(self, db: Any) -> list[dict[str, Any]]:
        """加载所有 incubating 状态的策略。"""
        if hasattr(db, "list_strategies"):
            return await db.list_strategies("incubating", limit=500)
        return []

    async def _list_paper_observation_strategies(self, db: Any) -> list[dict[str, Any]]:
        """DEV-V1 P1: 加载 paper observation 候选策略。

        边界:
          - 只消费 stage='paper' AND status='active'
          - 排除已升 candidate / listed 的策略(由 SQL 反 EXISTS 子句保证)
          - toggle 控制:INCUBATION_FACTORY_PAPER_INTAKE_ENABLED=1 才返回非空
          - LIMIT 由 INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT 控制,默认 50
          - db 不实现 list_paper_observation_strategies 时降级返回空,不抛异常
        """
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                paper_intake_enabled,
                paper_intake_batch_limit,
            )
        except Exception:
            return []
        if not paper_intake_enabled():
            return []
        for method_name in (
            "list_active_paper_observation_strategies",
            "list_paper_observation_strategies",
        ):
            method = _resolve_db_async_method(db, method_name)
            if method is None:
                continue
            try:
                return await method(limit=paper_intake_batch_limit())
            except Exception as exc:
                logger.warning(
                    "IncubationIntake: %s failed: %s", method_name, exc,
                )
                return []
        return []

    async def _list_diagnostic_observation_strategies(self, db: Any) -> list[dict[str, Any]]:
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                diagnostic_intake_enabled,
                diagnostic_intake_batch_limit,
            )
        except Exception:
            return []
        if not diagnostic_intake_enabled():
            return []
        if not hasattr(db, "list_diagnostic_observation_strategies"):
            return []
        try:
            return await db.list_diagnostic_observation_strategies(
                limit=diagnostic_intake_batch_limit(),
            )
        except Exception as exc:
            logger.warning(
                "IncubationIntake: list_diagnostic_observation_strategies failed: %s", exc,
            )
            return []

    async def _recognize_diagnostic_observation(
        self,
        db: Any,
        *,
        no_incubating: bool = False,
    ) -> dict[str, Any]:
        diagnostic_candidates = await self._list_diagnostic_observation_strategies(db)
        diagnostic_recognized = 0
        for strategy in diagnostic_candidates:
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                continue
            try:
                await self._record_diagnostic_intake_event(db, strategy)
                diagnostic_recognized += 1
            except Exception as exc:
                logger.warning(
                    "IncubationIntake: diagnostic intake event failed for %s: %s", sid, exc,
                )
        if diagnostic_candidates:
            suffix = " (no incubating)" if no_incubating else ""
            logger.info(
                "IncubationIntake: diagnostic observation recognized %d/%d candidates%s",
                diagnostic_recognized,
                len(diagnostic_candidates),
                suffix,
            )
        return {
            "scanned": len(diagnostic_candidates),
            "recognized": diagnostic_recognized,
            "strategy_ids": [str(s.get("id") or "") for s in diagnostic_candidates],
        }

    async def _list_gate3_record_only_candidates(self, db: Any) -> list[dict[str, Any]]:
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                gate3_record_only_intake_batch_limit,
                gate3_record_only_intake_enabled,
                gate3_record_only_intake_min_grade,
            )
        except Exception:
            return []
        if not gate3_record_only_intake_enabled():
            return []
        if not hasattr(db, "list_factory_task_evidence"):
            return []
        min_grade = gate3_record_only_intake_min_grade()
        min_rank = _GRADE_RANKS.get(min_grade, _GRADE_RANKS["S"])
        try:
            rows = await db.list_factory_task_evidence(
                evidence_type="gate3_record_only_audit",
                limit=gate3_record_only_intake_batch_limit(),
            )
        except Exception as exc:
            logger.warning(
                "IncubationIntake: list_factory_task_evidence failed: %s", exc,
            )
            return []

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            item = dict(row or {})
            if str(item.get("evidence_type") or "").strip() != "gate3_record_only_audit":
                continue
            payload = dict(item.get("evidence_payload") or {})
            candidate_id = str(
                payload.get("candidate_id")
                or payload.get("strategy_id")
                or payload.get("generated_strategy_id")
                or item.get("task_key")
                or ""
            ).strip()
            if not candidate_id or candidate_id in seen:
                continue
            grade = str(payload.get("validation_grade") or "").strip().upper()
            if _GRADE_RANKS.get(grade, -1) < min_rank:
                continue
            if payload.get("strategy_created") is not False:
                continue
            if payload.get("lifecycle_action_executed") is not False:
                continue
            seen.add(candidate_id)
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "strategy_name": payload.get("strategy_name") or payload.get("name"),
                    "strategy_type": payload.get("strategy_type"),
                    "description": payload.get("description") or payload.get("hypothesis"),
                    "tags": list(payload.get("tags") or []),
                    "generation_reason": dict(payload.get("generation_reason") or {}),
                    "research_task": dict(payload.get("research_task") or {}),
                    "strategy_explanation": dict(payload.get("strategy_explanation") or {}),
                    "task_key": item.get("task_key"),
                    "event_id": item.get("event_id"),
                    "theme_code": item.get("theme_code"),
                    "symbol": item.get("symbol"),
                    "evidence_id": item.get("id"),
                    "evidence_created_at": item.get("created_at"),
                    "validation_grade": grade,
                    "validation_total_score": payload.get("validation_total_score"),
                    "factory_run_id": payload.get("factory_run_id"),
                    "experiment_id": payload.get("experiment_id"),
                    "planned_submission_lane": payload.get("planned_submission_lane"),
                    "planned_final_status": payload.get("planned_final_status"),
                    "quality_summary": dict(payload.get("quality_summary") or {}),
                    "backtest_metrics": dict(payload.get("backtest_metrics") or {}),
                }
            )
        return candidates

    async def _recognize_gate3_record_only_candidates(
        self,
        db: Any,
        *,
        no_incubating: bool = False,
    ) -> dict[str, Any]:
        candidates = await self._list_gate3_record_only_candidates(db)
        recognized = 0
        skipped_existing = 0
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            try:
                if await self._has_gate3_record_only_intake_event(db, candidate_id):
                    skipped_existing += 1
                    continue
                await self._record_gate3_record_only_intake_event(db, candidate)
                recognized += 1
            except Exception as exc:
                logger.warning(
                    "IncubationIntake: gate3 record-only intake event failed for %s: %s",
                    candidate_id,
                    exc,
                )
        if candidates:
            suffix = " (no incubating)" if no_incubating else ""
            logger.info(
                "IncubationIntake: gate3 record-only recognized %d/%d candidates%s",
                recognized,
                len(candidates),
                suffix,
            )
        return {
            "scanned": len(candidates),
            "recognized": recognized,
            "skipped_existing": skipped_existing,
            "candidate_ids": [str(s.get("candidate_id") or "") for s in candidates],
        }

    async def _has_gate3_record_only_intake_event(self, db: Any, candidate_id: str) -> bool:
        if not hasattr(db, "list_strategy_domain_events"):
            return False
        try:
            events = await db.list_strategy_domain_events(
                aggregate_type="incubation_factory",
                aggregate_id=candidate_id,
                event_type="incubation_factory.gate3_record_only_candidate_recognized",
                limit=1,
            )
        except Exception:
            return False
        token = str(candidate_id or "").strip()
        for event in events or []:
            if str(event.get("aggregate_id") or "").strip() == token:
                return True
            payload = dict(event.get("payload") or {})
            if str(payload.get("candidate_id") or "").strip() == token:
                return True
        return False

    async def _record_gate3_record_only_intake_event(
        self,
        db: Any,
        candidate: dict[str, Any],
    ) -> None:
        if not hasattr(db, "save_strategy_domain_event"):
            return
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if not candidate_id:
            return
        explanation = dict(candidate.get("strategy_explanation") or {}) or self._strategy_explanation(
            {
                **dict(candidate or {}),
                "id": candidate_id,
                "name": candidate.get("strategy_name") or candidate_id,
                "strategy_type": candidate.get("strategy_type") or "record_only_candidate",
                "params": {
                    "generation_reason": dict(candidate.get("generation_reason") or {}),
                    "research_task": dict(candidate.get("research_task") or {}),
                    "quality_summary": dict(candidate.get("quality_summary") or {}),
                    "backtest_metrics": dict(candidate.get("backtest_metrics") or {}),
                },
            },
            metrics=dict(candidate.get("backtest_metrics") or {}),
            source="incubation_factory_gate3_record_only",
        )
        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "incubation_factory",
                "aggregate_id": candidate_id,
                "event_type": "incubation_factory.gate3_record_only_candidate_recognized",
                "source": "incubation_factory_intake_gate3_record_only",
                "severity": "info",
                "payload": {
                    "candidate_id": candidate_id,
                    "strategy_name": candidate.get("strategy_name"),
                    "strategy_type": candidate.get("strategy_type"),
                    "strategy_explanation": explanation,
                    "strategy_explanation_summary": explanation.get("summary"),
                    "strategy_explanation_labels": list(explanation.get("labels") or []),
                    "stage": "record_only_intake",
                    "record_only": True,
                    "action_boundary": "no_strategy_or_account_created",
                    "validation_grade": candidate.get("validation_grade"),
                    "validation_total_score": candidate.get("validation_total_score"),
                    "factory_run_id": candidate.get("factory_run_id"),
                    "experiment_id": candidate.get("experiment_id"),
                    "task_key": candidate.get("task_key"),
                    "evidence_id": candidate.get("evidence_id"),
                    "evidence_created_at": candidate.get("evidence_created_at"),
                    "symbol": candidate.get("symbol"),
                    "theme_code": candidate.get("theme_code"),
                    "planned_submission_lane": candidate.get("planned_submission_lane"),
                    "planned_final_status": candidate.get("planned_final_status"),
                    "quality_summary": candidate.get("quality_summary") or {},
                    "backtest_metrics": candidate.get("backtest_metrics") or {},
                },
            })
        except Exception as exc:
            logger.debug("IncubationIntake: gate3 record-only domain event save failed: %s", exc)

    async def _record_paper_intake_event(
        self,
        db: Any,
        strategy: dict[str, Any],
    ) -> None:
        """DEV-V1 P1: 记录策略被 paper observation intake 识别的领域事件。

        与 _record_acceptance_event 区别:
          - paper observation 策略不创建新账户(已经由 _enqueue_paper_observation 创建)
          - 只追加 domain event 表示孵化工厂已识别它纳入 paper 通道
        """
        if not hasattr(db, "save_strategy_domain_event"):
            return
        explanation = self._strategy_explanation(
            strategy,
            source="incubation_factory_paper_intake",
        )
        try:
            await db.save_strategy_domain_event({
                "strategy_id": strategy.get("id"),
                "aggregate_type": "incubation_factory",
                "aggregate_id": str(strategy.get("id")),
                "event_type": "incubation_factory.paper_observation_recognized",
                "source": "incubation_factory_intake_paper",
                "severity": "info",
                "payload": {
                    "strategy_name": strategy.get("name"),
                    "strategy_type": strategy.get("strategy_type"),
                    "strategy_explanation": explanation,
                    "strategy_explanation_summary": explanation.get("summary"),
                    "strategy_explanation_labels": list(explanation.get("labels") or []),
                    "stage": "paper",
                },
            })
        except Exception as exc:
            logger.debug("IncubationIntake: paper domain event save failed: %s", exc)

    async def _record_diagnostic_intake_event(
        self,
        db: Any,
        strategy: dict[str, Any],
    ) -> None:
        if not hasattr(db, "save_strategy_domain_event"):
            return
        explanation = self._strategy_explanation(
            strategy,
            source="incubation_factory_diagnostic_intake",
        )
        try:
            await db.save_strategy_domain_event({
                "strategy_id": strategy.get("id"),
                "aggregate_type": "incubation_factory",
                "aggregate_id": str(strategy.get("id")),
                "event_type": "incubation_factory.diagnostic_observation_recognized",
                "source": "incubation_factory_intake_diagnostic",
                "severity": "info",
                "payload": {
                    "strategy_name": strategy.get("name"),
                    "strategy_type": strategy.get("strategy_type"),
                    "strategy_explanation": explanation,
                    "strategy_explanation_summary": explanation.get("summary"),
                    "strategy_explanation_labels": list(explanation.get("labels") or []),
                    "stage": "diagnostic",
                    "diagnostic_observation": True,
                },
            })
        except Exception as exc:
            logger.debug("IncubationIntake: diagnostic domain event save failed: %s", exc)

    async def _record_acceptance_event(
        self,
        db: Any,
        strategy: dict[str, Any],
        account: dict[str, Any],
    ) -> None:
        """记录策略被孵化工厂接纳的领域事件。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return
        explanation = self._strategy_explanation(
            strategy,
            source="incubation_factory_intake",
        )
        try:
            await db.save_strategy_domain_event({
                "strategy_id": strategy.get("id"),
                "aggregate_type": "incubation_factory",
                "aggregate_id": str(strategy.get("id")),
                "event_type": "incubation_factory.strategy_accepted",
                "source": "incubation_factory_intake",
                "severity": "info",
                "payload": {
                    "strategy_name": strategy.get("name"),
                    "strategy_type": strategy.get("strategy_type"),
                    "strategy_explanation": explanation,
                    "strategy_explanation_summary": explanation.get("summary"),
                    "strategy_explanation_labels": list(explanation.get("labels") or []),
                    "account_id": account.get("account_id") or account.get("id"),
                    "initial_stage": "warmup",
                },
            })
        except Exception as exc:
            logger.debug("IncubationIntake: domain event save failed: %s", exc)
