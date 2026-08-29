"""手动触发前向收益回填"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(REPO / "packages" / "strategy-factory" / "src"))
sys.path.insert(0, str(REPO / "packages" / "akshare-mcp" / "src"))

from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=REPO,
    script_path=Path(__file__).resolve(),
    argv=sys.argv[1:],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)

from akshare_mcp.env_loader import load_mcp_env
load_mcp_env(override=False)

from akshare_mcp.storage import get_db, close_db


async def backfill_forward_returns():
    """直接调用 backfill_forward_returns 方法"""
    from akshare_mcp.services.signal_tracker import SignalTracker

    tracker = SignalTracker()
    db = get_db()

    print("开始回填前向收益...")
    result = await tracker.backfill_forward_returns(db=db)

    print("\n回填结果:")
    print(f"  总计算数: {result.get('computed', 0)}")
    print(f"  批次限制: {result.get('batch_limit', 0)}")
    print(f"  最大轮次: {result.get('max_rounds', 0)}")
    print(f"\n各窗口详情:")
    for window, stats in result.get('windows', {}).items():
        print(f"  {window}: rounds={stats['rounds']}, pending={stats['pending_seen']}, "
              f"computed={stats['computed']}, stalled={stats['stalled']}")

    await close_db()


if __name__ == "__main__":
    asyncio.run(backfill_forward_returns())
