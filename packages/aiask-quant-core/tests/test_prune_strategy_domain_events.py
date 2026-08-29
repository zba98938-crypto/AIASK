"""数据治理:prune_strategy_domain_events 有界裁剪高频领域事件,缓解表膨胀。

根因:strategy_domain_events 无任何保留机制,incubation.account_bound 等幂等高频事件
会累积到百万行级。该方法按 event_type 分组保留最近 N 行,用自增 id 作精确游标
(created_at 是秒级 TEXT,同秒批量插入有并列时间戳,不能用 created_at<=cutoff 否则误删)。
"""

from __future__ import annotations

import asyncio

from aiask_quant_core.storage import close_db, get_db


def _make_db(tmp_path, monkeypatch, name: str):
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / name))
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / name))
    return get_db()


def test_prune_keeps_newest_n_per_type_using_id_cursor(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch, "prune_keep.sqlite3")
    etype = "incubation.account_bound.keep_case"
    other = "incubation.stage_transitioned.keep_case"

    async def _run() -> None:
        try:
            await db.initialize()
            for i in range(30):
                await db.save_strategy_domain_event(
                    {"event_type": etype, "payload": {"i": i}}
                )
            for i in range(5):
                await db.save_strategy_domain_event(
                    {"event_type": other, "payload": {"i": i}}
                )

            # dry_run 只统计计划删除数,不实删。
            dry = await db.prune_strategy_domain_events(
                event_types=[etype], keep_per_type=10, dry_run=True
            )
            assert dry["dry_run"] is True
            assert dry["planned_deletes"][etype] == 20
            assert dry["deleted"] == 0
            # dry_run 后数据不变。
            still = await db.list_strategy_domain_events(event_type=etype, limit=500)
            assert len(still) == 30

            # 实删:保留最新 10 条(payload i=20..29),删除最旧 20 条。
            real = await db.prune_strategy_domain_events(
                event_types=[etype], keep_per_type=10, dry_run=False
            )
            assert real["deleted"] == 20
            remain = await db.list_strategy_domain_events(event_type=etype, limit=500)
            kept_i = sorted(int(e["payload"]["i"]) for e in remain)
            assert kept_i == list(range(20, 30))

            # 未列出的 event_type 不受影响。
            untouched = await db.list_strategy_domain_events(event_type=other, limit=500)
            assert len(untouched) == 5
        finally:
            await close_db()

    asyncio.run(_run())


def test_prune_without_event_types_returns_counts_only(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch, "prune_counts.sqlite3")
    etype = "incubation.account_bound.counts_only_case"

    async def _run() -> None:
        try:
            await db.initialize()
            for i in range(7):
                await db.save_strategy_domain_event(
                    {"event_type": etype, "payload": {"i": i}}
                )
            # 不传 event_types → 只返回各类型计数,绝不删除。
            result = await db.prune_strategy_domain_events()
            assert result["deleted"] == 0
            assert result["event_type_counts"][etype] == 7
            remain = await db.list_strategy_domain_events(event_type=etype, limit=500)
            assert len(remain) == 7
        finally:
            await close_db()

    asyncio.run(_run())


def test_prune_noop_when_keep_exceeds_total(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch, "prune_noop.sqlite3")
    etype = "incubation.pipeline_evaluated.noop_case"

    async def _run() -> None:
        try:
            await db.initialize()
            for i in range(3):
                await db.save_strategy_domain_event(
                    {"event_type": etype, "payload": {"i": i}}
                )
            result = await db.prune_strategy_domain_events(
                event_types=[etype],
                keep_per_type=100,
                dry_run=False,
            )
            assert result["deleted"] == 0
            assert result["planned_deletes"][etype] == 0
            remain = await db.list_strategy_domain_events(event_type=etype, limit=500)
            assert len(remain) == 3
        finally:
            await close_db()

    asyncio.run(_run())
