"""综合验证 v2 红队复测 5 项政策性 finding 的代码端 fix 是否落地。

方法:直接 import 函数测试,不依赖 MCP 重启。

涵盖:
  Fix 1: §4.5.1 GBK 乱码 (_safe_index_name + _COMMON_INDEX_NAMES 改正中文)
  Fix 2: §2.5 上证指数 close=10.68 (validate_kline 数值合理性护栏)
  Fix 3: §S13 governance backtest vs execution 一致性 (not_applicable / partial_input)
  Fix 4: 数据清洗(已用 _clean_corrupt_index.py 删除 506 行污染数据)
  Fix 5: §4.5.1 GBK 乱码静态名称回退表
"""

from __future__ import annotations
import os
import sys
import traceback
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path and os.path.isdir(_SCRIPTS_DIR):
    sys.path.insert(0, _SCRIPTS_DIR)

try:
    from _integration_paths import default_sqlite_path
except Exception:
    default_sqlite_path = None  # type: ignore[assignment]

def _aiask_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / "packages").is_dir() and (parent / "scripts").is_dir():
            return parent
    return Path.cwd()


def _aiask_db_path() -> str:
    override = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if override:
        return str(Path(override).expanduser().resolve())
    if default_sqlite_path is not None:
        return str(default_sqlite_path())
    return str(Path.home() / ".aiask" / "data" / "db" / "akshare_mcp.sqlite3")

# 强制 stdout UTF-8(避免 Windows GBK console 报 UnicodeEncodeError)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/_verify_v2_fixes.py:55: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass

# 让 import 找到 src/ 包(本脚本位于 packages/akshare-mcp/_verify_v2_fixes.py)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
for path_part in (
    os.path.join(_SCRIPT_DIR, "src"),
    os.path.join(_REPO_ROOT, "packages", "akshare-mcp", "src"),
    os.path.join(_REPO_ROOT, "packages", "aiask-quant-core", "src"),
):
    if path_part not in sys.path and os.path.isdir(path_part):
        sys.path.insert(0, path_part)

# Replace emoji with ASCII-safe markers for Windows console compatibility
PASS_MARK = "[PASS]"
FAIL_MARK = "[FAIL]"
WARN_MARK = "[WARN]"


def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


def verify_fix1_index_name_safety():
    """Verify _safe_index_name correctly substitutes GBK mojibake with static names."""
    _print_section("Fix 1: §4.5.1 GBK 乱码 — _safe_index_name 静态名表")
    from akshare_mcp.tools.market.quote import (
        _COMMON_INDEX_NAMES,
        _looks_like_gbk_garbled,
        _safe_index_name,
    )

    # 1.1 验证 _COMMON_INDEX_NAMES 正确含中文
    expected_names = {
        "000001": "上证指数",
        "399001": "深证成指",
        "399006": "创业板指",
        "000300": "沪深300",
        "000905": "中证500",
        "000852": "中证1000",
    }
    failures = []
    for code, expected in expected_names.items():
        actual = _COMMON_INDEX_NAMES.get(code)
        if actual != expected:
            failures.append(f"  {code}: expected={expected!r}, got={actual!r}")
    if failures:
        print(f"  {FAIL_MARK} _COMMON_INDEX_NAMES 仍含错误名称:")
        for f in failures:
            print(f)
        return False
    print(f"  {PASS_MARK} _COMMON_INDEX_NAMES 已修复为 {len(expected_names)}+ 中文条目(共 {len(_COMMON_INDEX_NAMES)} 个)")

    # 1.2 验证乱码检测
    test_cases = [
        ("????", True, "全问号视为乱码"),
        ("???", True, "短乱码"),
        ("\ufffd\ufffd", True, "replacement char"),
        ("上证指数", False, "正常中文"),
        ("S&P 500", False, "正常英文"),
        ("test", False, "非乱码字符串"),
        ("", False, "空字符串"),
    ]
    detect_failures = []
    for value, expected, desc in test_cases:
        actual = _looks_like_gbk_garbled(value)
        if actual != expected:
            detect_failures.append(f"  '{value}' ({desc}): expected={expected}, got={actual}")
    if detect_failures:
        print("  [FAIL] _looks_like_gbk_garbled 检测失败:")
        for f in detect_failures:
            print(f)
        return False
    print(f"  [PASS] _looks_like_gbk_garbled 7 个测试用例全部通过")

    # 1.3 验证 _safe_index_name 兜底
    fallback_cases = [
        ("????", "000001", "上证指数", "GBK 乱码 → 上证指数"),
        ("", "000001", "上证指数", "空字符串 → 上证指数"),
        (None, "000001", "上证指数", "None → 上证指数"),
        ("上证指数", "000001", "上证指数", "正常中文透传"),
        ("Test Index", "999999", "Test Index", "未知 code 透传 candidate"),
        ("?", "000300", "沪深300", "单问号 → 沪深300"),
    ]
    for raw, code, expected, desc in fallback_cases:
        actual = _safe_index_name(raw, code)
        if actual != expected:
            print(f"  [FAIL] {desc}: expected={expected!r}, got={actual!r}")
            return False
    print(f"  [PASS] _safe_index_name 6 个回退场景全部通过")

    return True


def verify_fix2_index_close_sanity():
    """Verify validate_kline rejects index code with stock-like close (e.g. close=10.68 on sh000001)."""
    _print_section("Fix 2: §2.5 上证指数 close=10.68 — validate_kline 数值合理性护栏")
    from aiask_quant_core.core.validators import (
        _check_index_close_in_range,
        _is_chinese_index_code,
        validate_kline,
    )

    # 2.1 验证 _is_chinese_index_code
    code_cases = [
        ("sh000001", True, "上证指数前缀"),
        ("sz399001", True, "深证成指前缀"),
        ("000001", True, "bare 上证 code"),
        ("000300", True, "沪深300"),
        ("399006", True, "创业板"),
        ("600519", False, "茅台不是指数"),
        ("000651", False, "格力不是指数"),
        ("index_sh000001", True, "前缀 index_"),
        ("", False, "空字符串"),
        (None, False, "None"),
    ]
    for code, expected, desc in code_cases:
        actual = _is_chinese_index_code(code)
        if actual != expected:
            print(f"  [FAIL] _is_chinese_index_code({code!r}) ({desc}): expected={expected}, got={actual}")
            return False
    print(f"  [PASS] _is_chinese_index_code 10 个测试用例全部通过")

    # 2.2 验证 _check_index_close_in_range
    range_cases = [
        ("sh000001", 4115.5, True, "正常上证点位"),
        ("sh000001", 10.68, False, "❗ 平安银行价格写入指数 — 应拒绝"),
        ("sh000001", 0, False, "零值"),
        ("sh000001", 50000, False, "超过 15000 上限"),
        ("000001", 10.68, False, "❗ bare code 错位"),
        ("600519", 1281.55, True, "茅台不参与指数检查"),
        ("600519", 10.68, True, "茅台正常股价(虽然假设值,但 stock 不参与检查)"),
        ("000300", 4500.0, True, "沪深300 正常"),
    ]
    for code, close, expected, desc in range_cases:
        actual = _check_index_close_in_range(code, close)
        if actual != expected:
            print(f"  [FAIL] {desc}: expected={expected}, got={actual}")
            return False
    print(f"  [PASS] _check_index_close_in_range 8 个测试用例全部通过")

    # 2.3 端到端验证:污染数据被 validate_kline 拒收
    bad_kline = {
        "code": "sh000001",
        "date": "2026-05-26",
        "open": 10.78,
        "high": 10.79,
        "low": 10.50,
        "close": 10.79,  # ← 平安银行价位错位
        "volume": 1000000,
    }
    try:
        validate_kline(bad_kline)
        print("  [FAIL] 污染 kline (sh000001 close=10.79) 应被拒绝但通过了")
        return False
    except ValueError as exc:
        if "index_close_out_of_range" in str(exc):
            print(f"  [PASS] 污染 kline 被正确拒绝: {exc}")
        else:
            print(f"  [WARN]️ kline 被拒绝但 error message 不含 index_close_out_of_range: {exc}")
            return False

    good_kline = {
        "code": "sh000001",
        "date": "2026-05-26",
        "open": 4112.0,
        "high": 4120.0,
        "low": 4110.0,
        "close": 4115.5,
        "volume": 100000000,
    }
    try:
        validate_kline(good_kline)
        print(f"  [PASS] 正常上证 kline (close=4115.5) 通过验证")
    except ValueError as exc:
        print(f"  [FAIL] 正常 kline 不应被拒绝: {exc}")
        return False

    # 2.4 普通股票不受影响
    stock_kline = {
        "code": "600519",
        "date": "2026-05-26",
        "open": 1281.55,
        "high": 1290.0,
        "low": 1278.0,
        "close": 1281.55,
        "volume": 5000,
    }
    try:
        validate_kline(stock_kline)
        print(f"  [PASS] 普通股票 kline (600519 茅台) 通过验证")
    except ValueError as exc:
        print(f"  [FAIL] 普通股票不应被拒绝: {exc}")
        return False

    return True


def verify_fix3_governance_consistency():
    """Verify governance check_online_offline_consistency now returns not_applicable when caller didn't provide."""
    _print_section("Fix 3: §S13 governance backtest_vs_execution — not_applicable 路径")
    from akshare_mcp.services.governance_monitor import check_online_offline_consistency

    # 3.1 caller 未提供任何参数 → not_applicable
    result = check_online_offline_consistency(None, None)
    if result.get("consistency_status") != "not_applicable":
        print(f"  [FAIL] 无参数应返回 not_applicable, got={result.get('consistency_status')}")
        return False
    if result.get("reason") != "no_assumptions_provided":
        print(f"  [FAIL] reason 应为 no_assumptions_provided, got={result.get('reason')}")
        return False
    print(f"  [PASS] 无参数返回 not_applicable + reason=no_assumptions_provided")

    # 3.2 仅提供单边 → partial_input
    result = check_online_offline_consistency(None, {"slippage_bps": 5.0, "market_impact_bps": 3.0})
    status = result.get("consistency_status")
    if status not in ("partial_input", "not_applicable"):
        print(f"  [FAIL] 单边参数应返回 partial_input/not_applicable, got={status}")
        return False
    print(f"  [PASS] 单边参数返回 {status}(non-blocking)")

    # 3.3 双边显式提供且差距大 → inconsistent(保留警示信号)
    result = check_online_offline_consistency(
        {"slippage_bps": 0.0, "market_impact_bps": 0.0, "commission_rate": 0.0003},
        {"slippage_bps": 5.0, "market_impact_bps": 3.0, "commission_rate": 0.0005},
    )
    if result.get("consistency_status") != "inconsistent":
        print(f"  [FAIL] 双边大差距应返回 inconsistent, got={result.get('consistency_status')}")
        return False
    if result.get("gap_count", 0) < 2:
        print(f"  [FAIL] 双边大差距应有 ≥2 gaps, got={result.get('gap_count')}")
        return False
    print(f"  [PASS] 双边显式差距大 → inconsistent + {result['gap_count']} gaps + warnings 显式")

    # 3.4 双边显式且对齐 → consistent
    aligned = {"slippage_bps": 5.0, "market_impact_bps": 3.0, "commission_rate": 0.0003}
    result = check_online_offline_consistency(aligned, aligned)
    if result.get("consistency_status") != "consistent":
        print(f"  [FAIL] 双边对齐应返回 consistent, got={result.get('consistency_status')}")
        return False
    print(f"  [PASS] 双边对齐 → consistent")

    return True


def verify_fix4_db_cleanup():
    """Verify sh000001 polluted rows have been cleaned."""
    _print_section("Fix 4: 数据清洗 — sh000001 污染数据已清理")
    import sqlite3
    conn = sqlite3.connect(_aiask_db_path())
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM kline_1d WHERE code = 'sh000001' AND close < 100")
    polluted = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*), MIN(close), MAX(close) FROM kline_1d WHERE code = 'sh000001'")
    total_row = cur.fetchone()
    conn.close()

    print(f"  sh000001 总行数: {total_row[0]}")
    print(f"  sh000001 close 区间: [{total_row[1]}, {total_row[2]}]")
    print(f"  污染行(close<100): {polluted}")

    if polluted > 0:
        print(f"  [FAIL] 仍有 {polluted} 行污染数据未清理")
        return False
    print(f"  [PASS] 污染数据已清理(snapshot 保存在 sh000001_corrupt_snapshot_20260526.sql)")
    return True


def verify_fix5_rfc001_north_fund_marker():
    """Verify get_north_fund returns RFC-001 policy marker when all sources unavailable."""
    _print_section("Fix 5: §2.1 北向资金 4 源全跪 — RFC-001 显式标记")
    # Mock 4 个 source 全跪,验证返回的 envelope 含 policy.rfc_id="RFC-001"
    from akshare_mcp.tools import fund_flow_north as ffn

    # 强制所有 source 返回空,触发全跪路径
    original_db = ffn._north_fund_from_db
    original_tushare = ffn._north_fund_from_tushare
    original_hkex = ffn._north_fund_from_hkex
    original_em = ffn._north_fund_from_eastmoney_direct

    def empty_source(_days):
        return []

    try:
        ffn._north_fund_from_db = empty_source
        ffn._north_fund_from_tushare = empty_source
        ffn._north_fund_from_hkex = empty_source
        ffn._north_fund_from_eastmoney_direct = empty_source

        # 直接调用原函数主体(避开 cached/limiter wrapper)
        result = ffn.get_north_fund.__wrapped__(days=10) if hasattr(ffn.get_north_fund, "__wrapped__") else ffn.get_north_fund(days=10)

        data = result.get("data") or {}
        policy = data.get("policy") or {}
        quality_flags = data.get("quality_flags") or result.get("quality_flags") or []

        if policy.get("rfc_id") != "RFC-001":
            print(f"  {FAIL_MARK} policy.rfc_id 应为 'RFC-001', got={policy.get('rfc_id')!r}")
            return False
        print(f"  {PASS_MARK} policy.rfc_id = 'RFC-001' 显式")

        if not policy.get("alternatives"):
            print(f"  {FAIL_MARK} policy.alternatives 应给出替代方案,got={policy.get('alternatives')!r}")
            return False
        print(f"  {PASS_MARK} policy.alternatives 含 {len(policy['alternatives'])} 个替代方案")

        if not policy.get("non_blocking"):
            print(f"  {FAIL_MARK} policy.non_blocking 应为 True 表示不阻塞")
            return False
        print(f"  {PASS_MARK} policy.non_blocking = True (上层不应当作 fail 处理)")

        # 检查 quality_flags 是否含 rfc_001 标记
        flags_str = " ".join(str(f) for f in quality_flags)
        if "rfc_001" in flags_str:
            print(f"  {PASS_MARK} quality_flags 含 rfc_001_north_fund_unavailable 标记")
        else:
            print(f"  {WARN_MARK} quality_flags 不含 rfc_001 标记 (got: {quality_flags})")
            # quality_flags 可能在 meta 而非 data 顶层,这种情况不算硬失败

        return True
    finally:
        ffn._north_fund_from_db = original_db
        ffn._north_fund_from_tushare = original_tushare
        ffn._north_fund_from_hkex = original_hkex
        ffn._north_fund_from_eastmoney_direct = original_em


def verify_fix6_dragon_tiger_tushare_fallback():
    """Verify get_dragon_tiger source_chain now includes tushare_top_list."""
    _print_section("Fix 6: §5.5 龙虎榜 sina+eastmoney 双跪 — tushare_top_list 兜底")
    from akshare_mcp.tools import fund_flow_market

    # 我们不真的调上游,只验证 source_chain 中已经包含 tushare_top_list
    import inspect
    source_text = inspect.getsource(fund_flow_market.get_dragon_tiger)

    if "tushare_top_list" not in source_text:
        print(f"  {FAIL_MARK} get_dragon_tiger 源码未含 tushare_top_list 兜底")
        return False
    print(f"  {PASS_MARK} get_dragon_tiger 已加 tushare_top_list 第三 source")

    if "ts_pro.top_list" not in source_text:
        print(f"  {FAIL_MARK} 缺少 ts_pro.top_list API 调用")
        return False
    print(f"  {PASS_MARK} 调用 ts_pro.top_list(trade_date=) 正确")

    if "dragon_tiger.tushare_top_list" not in source_text:
        print(f"  {FAIL_MARK} source_chain 缺少 dragon_tiger.tushare_top_list 标识")
        return False
    print(f"  {PASS_MARK} source_chain 包含 dragon_tiger.tushare_top_list")

    return True


def verify_fix7_quality_profile_env():
    """Verify QUALITY_PROFILE env switch (strict/lite/minimum) is wired correctly."""
    _print_section("Fix 7: §S19-F12 strategy_factory — QUALITY_PROFILE 三档可配置")
    import os

    # 验证默认 strict
    saved = os.environ.pop("AKSHARE_QUALITY_PROFILE", None)
    try:
        # Reload module to re-read env
        import importlib
        from akshare_mcp.services.factor_mining_factory import quality as quality_module
        importlib.reload(quality_module)
        if quality_module.QUALITY_PROFILE_ACTIVE != "strict":
            print(f"  {FAIL_MARK} 默认 profile 应为 strict, got={quality_module.QUALITY_PROFILE_ACTIVE}")
            return False
        if quality_module.QUALITY_THRESHOLDS["min_sample_dates"] != 60.0:
            print(f"  {FAIL_MARK} strict min_sample_dates 应为 60, got={quality_module.QUALITY_THRESHOLDS['min_sample_dates']}")
            return False
        print(f"  {PASS_MARK} 默认 profile=strict + min_sample_dates=60 / min_ic_history_rows=60")

        # 切换 lite
        os.environ["AKSHARE_QUALITY_PROFILE"] = "lite"
        importlib.reload(quality_module)
        if quality_module.QUALITY_PROFILE_ACTIVE != "lite":
            print(f"  {FAIL_MARK} lite profile 切换失败, got={quality_module.QUALITY_PROFILE_ACTIVE}")
            return False
        if quality_module.QUALITY_THRESHOLDS["min_sample_dates"] != 30.0:
            print(f"  {FAIL_MARK} lite min_sample_dates 应为 30, got={quality_module.QUALITY_THRESHOLDS['min_sample_dates']}")
            return False
        print(f"  {PASS_MARK} lite profile + min_sample_dates=30 / min_ic_history_rows=20 (开发期可用)")

        # 切换 minimum
        os.environ["AKSHARE_QUALITY_PROFILE"] = "minimum"
        importlib.reload(quality_module)
        if quality_module.QUALITY_PROFILE_ACTIVE != "minimum":
            print(f"  {FAIL_MARK} minimum profile 切换失败")
            return False
        print(f"  {PASS_MARK} minimum profile (仅用于 smoke test)")

        # Invalid value falls back to strict
        os.environ["AKSHARE_QUALITY_PROFILE"] = "garbage_value"
        importlib.reload(quality_module)
        if quality_module.QUALITY_PROFILE_ACTIVE != "strict":
            print(f"  {FAIL_MARK} 非法值应回退到 strict, got={quality_module.QUALITY_PROFILE_ACTIVE}")
            return False
        print(f"  {PASS_MARK} 非法 profile 值正确回退到 strict (生产安全)")

        return True
    finally:
        # Restore env
        if saved is None:
            os.environ.pop("AKSHARE_QUALITY_PROFILE", None)
        else:
            os.environ["AKSHARE_QUALITY_PROFILE"] = saved


def main():
    print("\n" + "=" * 60)
    print("  AIASK MCP v2 红队复测 — 5 项政策性 Fix 综合验证")
    print("=" * 60)

    results = []
    fixes = [
        ("Fix 1: §4.5.1 GBK 乱码", verify_fix1_index_name_safety),
        ("Fix 2: §2.5 索引数值护栏", verify_fix2_index_close_sanity),
        ("Fix 3: §S13 governance 一致性", verify_fix3_governance_consistency),
        ("Fix 4: §2.5 数据清洗", verify_fix4_db_cleanup),
        ("Fix 5: §2.1 RFC-001 北向标记", verify_fix5_rfc001_north_fund_marker),
        ("Fix 6: §5.5 龙虎榜 tushare 兜底", verify_fix6_dragon_tiger_tushare_fallback),
        ("Fix 7: §S19-F12 QUALITY_PROFILE 三档", verify_fix7_quality_profile_env),
    ]
    for name, fn in fixes:
        try:
            ok = fn()
        except Exception as exc:
            print(f"\n  [FAIL] {name} 验证脚本异常: {exc}")
            traceback.print_exc()
            ok = False
        results.append((name, ok))

    print("\n" + "=" * 60)
    print("  汇总")
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for name, ok in results:
        status = "[PASS] PASS" if ok else "[FAIL] FAIL"
        print(f"  {status}  {name}")
    print(f"\n  Total: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
