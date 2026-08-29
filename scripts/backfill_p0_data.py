#!/usr/bin/env python
"""P0 数据回填运维脚本(诊断报告 §2 数据基础设施)

执行内容:
1. P0-1 北向资金 21 月 stale → backfill (akshare.stock_hsgt_hist_em)
2. P0-1 margin_market_flow + margin_detail 同步链恢复
3. P0-1 dragon_tiger_list 6 个交易日全跪 → backfill
4. P0-4 vector_documents / kline_pattern_windows 全量 backfill
5. P0-2 sh000001 在 stock_quotes 错位映射诊断报告(只读,不修)
6. db.stock_quotes 全市场 prewarm(P2-4.1.1)

运维使用方式:
    python scripts/backfill_p0_data.py --all                    # 全量回填
    python scripts/backfill_p0_data.py --north-fund             # 仅北向
    python scripts/backfill_p0_data.py --margin --dragon-tiger  # 仅融资融券+龙虎榜
    python scripts/backfill_p0_data.py --vector --limit 200     # 仅向量索引
    python scripts/backfill_p0_data.py --diagnose-sh000001      # sh000001 错位诊断(只读)
    python scripts/backfill_p0_data.py --prewarm-quotes         # db.stock_quotes 全市场预热

每步骤会:
- 先打印 db 当前状态(行数 / max_date / coverage_ratio)
- 调用现有 ExternalGapSyncService 接口或 vector_backfill_* 任务
- 完成后再次打印 db 状态(变化对比)
- 任何异常不中断剩余步骤,全部步骤完成后输出汇总表

幂等:可重复运行,基于 INSERT ... ON CONFLICT DO UPDATE 语义。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "aiask-quant-core" / "src"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_table_status(db, table: str, date_col: str) -> dict[str, Any]:
    """获取表当前 max_date / count / days_stale。"""
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT COUNT(*) AS cnt, MIN({date_col}) AS min_d, MAX({date_col}) AS max_d FROM {table}"
            )
        max_d = row["max_d"]
        if max_d is None:
            days_stale = None
        else:
            try:
                if isinstance(max_d, str):
                    max_dt = datetime.fromisoformat(str(max_d).replace("Z", "+00:00")).replace(tzinfo=None)
                elif hasattr(max_d, "tzinfo") and max_d.tzinfo is not None:
                    max_dt = max_d.replace(tzinfo=None)
                else:
                    max_dt = max_d
                days_stale = max(0, (datetime.now() - max_dt).days)
            except Exception:
                days_stale = None
        return {
            "table": table,
            "row_count": int(row["cnt"] or 0),
            "min_date": str(row["min_d"]) if row["min_d"] is not None else None,
            "max_date": str(max_d) if max_d is not None else None,
            "days_stale": days_stale,
        }
    except Exception as exc:
        return {"table": table, "error": f"{type(exc).__name__}:{exc}"}


async def _backfill_north_fund(db) -> dict[str, Any]:
    """P0-1 北向资金回填。"""
    print("\n=== [1] P0-1 北向资金回填 ===")
    before = await _load_table_status(db, "north_fund_flow", "trade_date")
    print(f"  before: {before}")

    try:
        from akshare_mcp.services.external_gap_sync_service import ExternalGapSyncService
        try:
            import akshare as ak
        except ImportError:
            return {**before, "result": "akshare_not_installed", "after": before}

        service = ExternalGapSyncService(north_days=730, margin_days=730)  # 回填 2 年
        report = await service.sync_north_fund_from_akshare(db, ak)
        after = await _load_table_status(db, "north_fund_flow", "trade_date")
        print(f"  fill report: {report}")
        print(f"  after: {after}")
        return {"step": "north_fund_flow", "before": before, "after": after, "report": report}
    except Exception as exc:
        traceback.print_exc()
        return {"step": "north_fund_flow", "before": before, "error": f"{type(exc).__name__}:{exc}"}


async def _backfill_margin(db) -> dict[str, Any]:
    """P0-1 融资融券回填。"""
    print("\n=== [2] P0-1 融资融券回填 ===")
    mkt_before = await _load_table_status(db, "margin_market_flow", "trade_date")
    detail_before = await _load_table_status(db, "margin_detail", "date")
    print(f"  market before: {mkt_before}")
    print(f"  detail before: {detail_before}")

    try:
        from akshare_mcp.services.external_gap_sync_service import ExternalGapSyncService
        try:
            import akshare as ak
        except ImportError:
            return {"step": "margin", "result": "akshare_not_installed"}

        service = ExternalGapSyncService(margin_days=180)
        market_report = await service.sync_margin_market_from_akshare(db, ak)
        detail_report = await service.sync_margin_detail_from_akshare(db, ak)
        mkt_after = await _load_table_status(db, "margin_market_flow", "trade_date")
        detail_after = await _load_table_status(db, "margin_detail", "date")
        print(f"  market after: {mkt_after}")
        print(f"  detail after: {detail_after}")
        return {
            "step": "margin",
            "market": {"before": mkt_before, "after": mkt_after, "report": market_report},
            "detail": {"before": detail_before, "after": detail_after, "report": detail_report},
        }
    except Exception as exc:
        traceback.print_exc()
        return {"step": "margin", "error": f"{type(exc).__name__}:{exc}"}


async def _backfill_dragon_tiger(db) -> dict[str, Any]:
    """P0-1 龙虎榜回填。"""
    print("\n=== [3] P0-1 龙虎榜回填(过去 30 个交易日) ===")
    before = await _load_table_status(db, "dragon_tiger_list", "date")
    print(f"  before: {before}")

    try:
        try:
            import akshare as ak
        except ImportError:
            return {"step": "dragon_tiger", "result": "akshare_not_installed"}

        # 过去 30 个交易日(约 6 周)
        end = date.today()
        start = end - timedelta(days=45)
        try:
            df = await asyncio.to_thread(
                ak.stock_lhb_detail_em,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            inserted = 0
            if df is not None and not df.empty:
                async with db.acquire() as conn:
                    for _, r in df.iterrows():
                        try:
                            d = str(r.get("交易日", "") or r.get("trade_date", "")).strip()
                            if not d:
                                continue
                            await conn.execute(
                                """INSERT INTO dragon_tiger_list (date, code, source, raw)
                                   VALUES ($1, $2, 'akshare.stock_lhb_detail_em', $3)
                                   ON CONFLICT (date, code) DO NOTHING""",
                                d,
                                str(r.get("代码", "") or r.get("symbol", "")).strip(),
                                json.dumps(r.to_dict(), ensure_ascii=False, default=str),
                            )
                            inserted += 1
                        except Exception:
                            continue
            after = await _load_table_status(db, "dragon_tiger_list", "date")
            print(f"  inserted: {inserted}")
            print(f"  after: {after}")
            return {"step": "dragon_tiger", "before": before, "after": after, "inserted": inserted}
        except Exception as exc:
            traceback.print_exc()
            return {"step": "dragon_tiger", "before": before, "error": f"{type(exc).__name__}:{exc}"}
    except Exception as exc:
        return {"step": "dragon_tiger", "error": f"outer:{type(exc).__name__}:{exc}"}


async def _backfill_vector(db, limit: int = 200) -> dict[str, Any]:
    """P0-4 vector_documents / kline_pattern_windows 全量 backfill。"""
    print(f"\n=== [4] P0-4 向量索引回填(limit={limit}) ===")
    md_before = await _load_table_status(db, "market_documents", "created_at")
    vd_before = await _load_table_status(db, "vector_documents", "date")
    kpw_before = await _load_table_status(db, "kline_pattern_windows", "end_date")
    print(f"  market_documents: {md_before}")
    print(f"  vector_documents: {vd_before}")
    print(f"  kline_pattern_windows: {kpw_before}")

    results = {}

    # 通过 data_sync_manager 触发任务
    try:
        from akshare_mcp.tools.managers.data_sync_manager import register_data_sync_manager  # noqa: F401
        # 我们直接调底层 _execute_sync_task
        from akshare_mcp.tools.managers.data_sync_manager import _execute_sync_task, _build_task_payload
        for task_type in ("vector_backfill_market_docs", "vector_backfill_kline_patterns", "vector_backfill_stock_profiles"):
            try:
                payload = _build_task_payload(task_type, [], {"limit": limit})
                result = await _execute_sync_task(
                    db,
                    task_type=task_type,
                    codes=[],
                    priority="high",
                    payload=payload,
                )
                results[task_type] = result
                print(f"  {task_type}: {result.get('status', '?')} {result.get('message', '')}")
            except Exception as exc:
                results[task_type] = {"error": f"{type(exc).__name__}:{exc}"}
                print(f"  {task_type}: ERROR {exc}")
    except ImportError as exc:
        results["error"] = f"data_sync_manager_unavailable:{exc}"

    md_after = await _load_table_status(db, "market_documents", "created_at")
    vd_after = await _load_table_status(db, "vector_documents", "date")
    kpw_after = await _load_table_status(db, "kline_pattern_windows", "end_date")
    print(f"  market_documents after: {md_after}")
    print(f"  vector_documents after: {vd_after}")
    print(f"  kline_pattern_windows after: {kpw_after}")

    return {
        "step": "vector_backfill",
        "market_documents": {"before": md_before, "after": md_after},
        "vector_documents": {"before": vd_before, "after": vd_after},
        "kline_pattern_windows": {"before": kpw_before, "after": kpw_after},
        "tasks": results,
    }


async def _diagnose_sh000001(db) -> dict[str, Any]:
    """P0-2 sh000001 在 stock_quotes 是否错位映射。只读诊断,不修改数据。"""
    print("\n=== [5] P0-2 sh000001 stock_quotes 错位诊断(只读) ===")
    diag = {"step": "diagnose_sh000001", "checks": {}}
    try:
        async with db.acquire() as conn:
            for code in ("sh000001", "000001", "999999"):
                try:
                    rows = await conn.fetch(
                        f"""SELECT code, name, time, close
                            FROM stock_quotes
                            WHERE code = $1
                            ORDER BY time DESC LIMIT 3""",
                        code,
                    )
                    if rows:
                        diag["checks"][code] = [
                            {
                                "code": r.get("code"),
                                "name": str(r.get("name") or "")[:40],
                                "time": str(r.get("time")),
                                "close": float(r.get("close") or 0),
                            }
                            for r in rows
                        ]
                    else:
                        diag["checks"][code] = "no_rows"
                except Exception as exc:
                    diag["checks"][code] = f"error:{exc}"

            # 看 index_quotes(若存在)
            try:
                rows = await conn.fetch(
                    """SELECT code, time, close FROM index_quotes
                       WHERE code IN ('sh000001', '000001', 'sh000300')
                       ORDER BY time DESC LIMIT 6"""
                )
                diag["checks"]["index_quotes"] = [
                    {
                        "code": r.get("code"),
                        "time": str(r.get("time")),
                        "close": float(r.get("close") or 0),
                    }
                    for r in rows
                ]
            except Exception:
                diag["checks"]["index_quotes"] = "table_not_exists"
    except Exception as exc:
        diag["error"] = f"{type(exc).__name__}:{exc}"

    print("  诊断结果:")
    for code, info in diag.get("checks", {}).items():
        print(f"    {code}: {info if isinstance(info, str) else info[:2]}")

    # 推断
    sh = diag.get("checks", {}).get("sh000001")
    pa = diag.get("checks", {}).get("000001")
    if isinstance(sh, list) and isinstance(pa, list) and sh and pa:
        sh_close = sh[0].get("close")
        pa_close = pa[0].get("close")
        if sh_close and pa_close:
            if abs(sh_close - pa_close) < 0.5:
                diag["diagnosis"] = "MISALIGNED: sh000001 close 与 000001 平安银行接近,确认错位映射"
            elif sh_close > 1000:
                diag["diagnosis"] = "OK: sh000001 close 落在合理区间"
            else:
                diag["diagnosis"] = f"SUSPICIOUS: sh000001 close={sh_close} 偏低,需排查"
    return diag


async def _prewarm_stock_quotes(db, batch_size: int = 200) -> dict[str, Any]:
    """P2-4.1.1 db.stock_quotes 全市场预热(按 batch 触发 sync 任务)。"""
    print(f"\n=== [6] P2-4.1.1 db.stock_quotes 全市场预热(batch={batch_size}) ===")
    before = await _load_table_status(db, "stock_quotes", "time")
    print(f"  before: {before}")

    try:
        async with db.acquire() as conn:
            stock_codes_rows = await conn.fetch(
                "SELECT code FROM stocks ORDER BY code LIMIT 6000"
            )
        codes = [r["code"] for r in stock_codes_rows if r.get("code")]
    except Exception as exc:
        return {"step": "prewarm_quotes", "error": f"failed_to_list_stocks:{exc}"}

    print(f"  目标:{len(codes)} 只股票")

    try:
        from akshare_mcp.tools.managers.data_sync_manager import _execute_sync_task, _build_task_payload
        results = []
        for i in range(0, len(codes), batch_size):
            batch = codes[i: i + batch_size]
            try:
                payload = _build_task_payload("core_market", batch, {})
                r = await _execute_sync_task(db, task_type="core_market", codes=batch, priority="high", payload=payload)
                results.append({"batch": i // batch_size, "result": r.get("status")})
            except Exception as exc:
                results.append({"batch": i // batch_size, "error": str(exc)})
        after = await _load_table_status(db, "stock_quotes", "time")
        print(f"  after: {after}")
        return {"step": "prewarm_quotes", "before": before, "after": after, "batches": len(results)}
    except Exception as exc:
        traceback.print_exc()
        return {"step": "prewarm_quotes", "error": f"{type(exc).__name__}:{exc}"}


async def main() -> int:
    parser = argparse.ArgumentParser(description="P0 数据回填运维脚本")
    parser.add_argument("--all", action="store_true", help="执行所有回填(运维谨慎)")
    parser.add_argument("--north-fund", action="store_true")
    parser.add_argument("--margin", action="store_true")
    parser.add_argument("--dragon-tiger", action="store_true")
    parser.add_argument("--vector", action="store_true")
    parser.add_argument("--limit", type=int, default=200, help="向量索引 backfill 限制")
    parser.add_argument("--diagnose-sh000001", action="store_true", help="只读诊断 sh000001 错位")
    parser.add_argument("--prewarm-quotes", action="store_true", help="db.stock_quotes 全市场预热")
    parser.add_argument("--prewarm-batch", type=int, default=200)
    parser.add_argument("--report-path", type=str, default=None, help="JSON 报告路径")

    args = parser.parse_args()

    if not (
        args.all or args.north_fund or args.margin or args.dragon_tiger or args.vector
        or args.diagnose_sh000001 or args.prewarm_quotes
    ):
        parser.print_help()
        print("\n至少指定一个动作(--all 或 --north-fund / --margin / --dragon-tiger / ...)。")
        return 2

    print(f"=== P0 数据回填脚本启动 @ {_iso_now()} ===")

    try:
        from akshare_mcp.storage import get_db
    except ImportError as exc:
        print(f"FATAL: 无法 import akshare_mcp.storage: {exc}")
        return 3

    db = get_db()
    summary: list[dict[str, Any]] = []

    if args.all or args.north_fund:
        summary.append(await _backfill_north_fund(db))
    if args.all or args.margin:
        summary.append(await _backfill_margin(db))
    if args.all or args.dragon_tiger:
        summary.append(await _backfill_dragon_tiger(db))
    if args.all or args.vector:
        summary.append(await _backfill_vector(db, limit=args.limit))
    if args.all or args.diagnose_sh000001:
        summary.append(await _diagnose_sh000001(db))
    if args.all or args.prewarm_quotes:
        summary.append(await _prewarm_stock_quotes(db, batch_size=args.prewarm_batch))

    print("\n=== 汇总 ===")
    for s in summary:
        step = s.get("step", "?")
        if "error" in s:
            print(f"  ❌ {step}: {s['error']}")
        else:
            print(f"  ✅ {step}: completed")

    if args.report_path:
        rp = Path(args.report_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON 报告: {rp.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
