"""FIX-18 (F-N07-1) 回归：valuation_consensus 内部 DCF 字段对齐，盈利股不再全失败。"""

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "akshare_mcp"


def test_consensus_dcf_reads_correct_net_profit_field():
    """consensus 内部 DCF 必须读 net_profit（db.get_financials 实际列），而非仅 netProfit。"""
    src = (_SRC / "tools" / "valuation_consensus.py").read_text(encoding="utf-8")
    # 必须包含对真实列名 net_profit 的读取
    assert 'fin_payload.get("net_profit")' in src
    # 必须有从 eps 推导股本的逻辑（financials 无独立 shares 列）
    assert "base_ni / eps" in src


def test_consensus_dcf_field_mapping_logic():
    """单元模拟：用真实字段名的 payload 能算出正股本与正 per_share。"""
    from akshare_mcp.tools.valuation_consensus import _simple_dcf_per_share

    net_profit = 27242512500.0
    eps = 87.02
    shares = net_profit / eps  # 推导股本
    per_share = _simple_dcf_per_share(
        base_cashflow=net_profit,
        growth_rate=0.05,
        discount_rate=0.10,
        terminal_growth_rate=0.05,
        years=5,
        shares_outstanding=shares,
    )
    assert per_share is not None and per_share > 0


def test_consensus_no_longer_uses_only_postgres_shares_fields():
    """回归保护：不应再仅依赖 totalShares/shares 这类 financials 不存在的列。"""
    src = (_SRC / "tools" / "valuation_consensus.py").read_text(encoding="utf-8")
    # 旧 bug 形式：base_ni 只读 netProfit/net_income（无 net_profit）
    assert 'fin_payload.get("netProfit") or fin_payload.get("net_income") or 0' not in src
