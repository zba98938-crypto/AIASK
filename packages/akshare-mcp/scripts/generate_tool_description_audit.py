#!/usr/bin/env python3
from __future__ import annotations

import argparse
import inspect
import os
import re
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
PACKAGE_ROOT = SCRIPT_PATH.parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from akshare_mcp.server import mcp
from akshare_mcp.tool_registry import build_tool_registry


def norm(value) -> str:
    return "" if value is None else str(value).replace("\r\n", "\n").strip()


def short(text: str, limit: int = 44) -> str:
    collapsed = re.sub(r"\s+", " ", text or "")
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def score(desc: str, doc: str, fn, name: str):
    text = (desc or "") + "\n" + (doc or "")
    sig = inspect.signature(fn)
    params = [param for param in sig.parameters.values() if param.name not in ("self", "cls")]
    accuracy = "❌" if not (desc or doc) else ("⚠️" if len((desc or doc).strip()) < 18 else "✅")
    param_quality = (
        "✅"
        if not params
        else (
            "❌"
            if not (("Args:" in text) or ("参数" in text))
            else ("⚠️" if ("YYYY" not in text and "默认" not in text and "格式" not in text and "如" not in text) else "✅")
        )
    )
    returns = "✅" if (("Returns:" in text) or ("返回" in text)) else ("⚠️" if text.strip() else "❌")
    examples = "✅" if (("Example" in text) or ("Examples" in text) or ("示例" in text)) else "❌"
    bad = [accuracy, param_quality, returns, examples].count("❌")
    warn = [accuracy, param_quality, returns, examples].count("⚠️")
    core = any(
        keyword in name
        for keyword in [
            "kline",
            "quote",
            "order_book",
            "backtest",
            "manager",
            "sync",
            "alert",
            "portfolio",
            "valuation",
        ]
    )
    if bad >= 2 or (bad == 1 and (core or returns == "❌" or param_quality == "❌")):
        priority = "P0"
    elif bad == 1 or warn >= 2 or examples == "❌":
        priority = "P1"
    else:
        priority = "P2"

    issues = []
    if accuracy == "❌":
        issues.append("描述缺失")
    elif accuracy == "⚠️":
        issues.append("描述偏短")
    if param_quality == "❌":
        issues.append("参数规范缺失")
    elif param_quality == "⚠️":
        issues.append("参数约束不足")
    if returns == "❌":
        issues.append("返回结构缺失")
    elif returns == "⚠️":
        issues.append("返回字段未细化")
    if examples == "❌":
        issues.append("无示例")

    advice = []
    if accuracy != "✅":
        advice.append("补1句场景化定义")
    if param_quality != "✅":
        advice.append("补Args类型/默认/格式")
    if returns != "✅":
        advice.append("补Returns与错误返回")
    if examples != "✅":
        advice.append("补1个可运行示例")
    if "compat" in name or "manager" in name:
        advice.append("补Node映射说明")

    return (
        accuracy,
        param_quality,
        returns,
        examples,
        priority,
        "；".join(issues) if issues else "说明较完整",
        "；".join(advice[:3]),
    )


def _collect_rows():
    tool_map = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
    registry_rows = build_tool_registry(mcp)
    rows = []
    for registry_row in registry_rows:
        name = registry_row["name"]
        tool = tool_map.get(name)
        fn = getattr(tool, "fn", None)
        unwrapped = inspect.unwrap(fn) if fn else None
        desc = norm(getattr(tool, "description", None) if tool else registry_row.get("description"))
        doc = norm(inspect.getdoc(unwrapped or fn) if fn else "")
        accuracy, params, returns, examples, priority, issue, advice = score(desc, doc, unwrapped or fn, name)
        rows.append((name, desc, doc, accuracy, params, returns, examples, priority, issue, advice))
    return rows


def build_md(rows) -> str:
    tool_count = len(rows)
    p0 = sum(1 for row in rows if row[7] == "P0")
    p1 = sum(1 for row in rows if row[7] == "P1")
    p2 = sum(1 for row in rows if row[7] == "P2")
    empty_desc = sum(1 for row in rows if row[1] == "")

    lines = [
        f"# AKShare MCP 工具描述全面审查与改进计划（{tool_count} 工具逐条矩阵）",
        "",
        f"> 审查时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "> 审查范围：`packages/akshare-mcp/src/akshare_mcp/tools/` + `server.py`",
        f"> 工具总数：**{tool_count}**（运行时可用工具）",
        f"> 空 description：**{empty_desc}**",
        "",
        "## 一、总体问题汇总",
        "",
        "### 1.1 四维审查标准",
        "- 准确性与完整性",
        "- 参数规范性",
        "- 返回完整性",
        "- 示例实用性",
        "",
        "### 1.2 优先级统计",
        f"- **P0：{p0}**",
        f"- **P1：{p1}**",
        f"- **P2：{p2}**",
        "",
        "## 二、分类审查结果",
        "",
        "- 市场/行情/资金流：补时效与源优先级。",
        "- 技术分析/公式回退：补参数范围与异常行为。",
        "- 回测/组合/风险：补输出指标与字段。",
        "- managers：补 action/kwargs schema。",
        "",
        f"## 三、{tool_count} 工具逐条问题矩阵",
        "",
        "|#|工具|当前description|当前docstring(首句)|准确性|参数|返回|示例|优先级|问题摘要|改进建议|",
        "|---:|---|---|---|:---:|:---:|:---:|:---:|:---:|---|---|",
    ]
    for index, row in enumerate(rows, 1):
        name, desc, doc, accuracy, params, returns, examples, priority, issue, advice = row
        first_line = doc.splitlines()[0] if doc else "[空]"
        lines.append(
            f"|{index}|`{name}`|{short(desc) if desc else '[空]'}|"
            f"{short(first_line, 34)}|{accuracy}|{params}|{returns}|{examples}|{priority}|{issue}|{advice}|"
        )

    lines.extend(
        [
            "",
            "## 四、通用改进建议",
            "",
            "1. 统一 Docstring 模板：功能/时效/Args/Returns/Example。",
            "2. 返回结构统一建议含 success/error/source/stale/timestamp。",
            "3. 兼容工具补 Node.js 参数映射示例。",
            "",
            "## 五、实施计划",
            "",
            "1. 先处理 P0。",
            "2. 再处理 P1。",
            "3. 最后处理 P2 与文风统一。",
            "",
            "> 说明：仅输出审查结果，不修改业务代码。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate tool description audit report from runtime registry.")
    parser.add_argument(
        "--output",
        default=str(PACKAGE_ROOT / "TOOL_DESCRIPTION_IMPROVEMENT_PLAN.md"),
        help="Output markdown path.",
    )
    parser.add_argument("--expected-tools", type=int, default=0, help="Optional runtime tool count guard.")
    args = parser.parse_args()

    rows = _collect_rows()
    if args.expected_tools and len(rows) != int(args.expected_tools):
        raise RuntimeError(f"工具数异常：{len(rows)} (expected {args.expected_tools})")

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_md(rows), encoding="utf-8")
    print("written", output_path, "rows", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
