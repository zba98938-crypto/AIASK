"""股票/指数 name 字段卫生(诊断报告 §4.5.1 P2-4.5.1 修复)。

历史问题:
- get_index_quote(000001).name = "????"
- get_index_quote(000300).name = "??300"
- macro_manager.market_overview.sz399001.name = "????"
GBK encoding 在某些数据源响应解码错误,直接展示给 AI 是误导。

修复:
- detect_mojibake(text):识别乱码字符比例
- sanitize_name(name, fallback=""):乱码比例过高时返回 fallback
- 提供 wrap_response_names(payload, ...) 一站式
"""
from __future__ import annotations

import re
from typing import Any


# 常见乱码字符:?? / ⊙ / 锟斤拷 / 0xfffd 替换符
_MOJIBAKE_PATTERNS = [
    re.compile(r'\?{2,}'),     # 多个 ? 连续
    re.compile(r'锟斤拷'),       # GBK→UTF-8 经典误码
    re.compile(r'\ufffd'),     # Unicode replacement char
    re.compile(r'â??|â€'),     # Latin1→UTF-8 误码
]


def detect_mojibake_ratio(text: str) -> float:
    """检测文本中乱码字符占比。"""
    if not text:
        return 0.0
    text = str(text).strip()
    if not text:
        return 0.0
    total = len(text)
    mojibake_chars = 0
    for pattern in _MOJIBAKE_PATTERNS:
        for m in pattern.finditer(text):
            mojibake_chars += len(m.group())
    return mojibake_chars / max(total, 1)


def is_mojibake(text: Any, *, threshold: float = 0.30) -> bool:
    """如果文本中乱码占比 >= threshold 视为乱码。"""
    if not text or not isinstance(text, str):
        return False
    return detect_mojibake_ratio(text) >= threshold


def sanitize_name(name: Any, *, fallback: str = "", threshold: float = 0.30) -> str:
    """规范化 name 字段:乱码 → fallback,正常 → 原值。

    Args:
        name: 原始 name 字段
        fallback: 乱码时返回的替代值,默认空字符串
        threshold: 乱码字符占比阈值,默认 30%

    Returns:
        清洗后的 name
    """
    if name is None:
        return fallback
    s = str(name).strip()
    if not s:
        return fallback
    if is_mojibake(s, threshold=threshold):
        return fallback
    return s


def wrap_response_with_clean_names(
    response: dict,
    *,
    name_keys: tuple = ("name", "stock_name", "indexName", "code_name"),
    fallback: str = "",
    threshold: float = 0.30,
) -> dict:
    """递归清洗 response 中所有 name 类字段(就地修改)。

    用法:
        response = wrap_response_with_clean_names(get_index_quote(...))
        # 顶层 + data 子结构所有 name 字段乱码 → 空串

    Returns:
        清洗后的 response(原对象,就地修改)
    """
    if not isinstance(response, dict):
        return response
    detected: list[str] = []
    _walk_clean(response, name_keys=name_keys, fallback=fallback, threshold=threshold, detected_log=detected)
    if detected:
        response.setdefault("quality_flags", []).append("name_mojibake_sanitized")
        response.setdefault("warnings", []).append(
            f"name_mojibake_sanitized: {len(detected)} fields had mojibake encoding errors"
        )
    return response


def _walk_clean(node: Any, *, name_keys: tuple, fallback: str, threshold: float, detected_log: list[str]):
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in name_keys and isinstance(value, str):
                if is_mojibake(value, threshold=threshold):
                    detected_log.append(f"{key}={value!r}")
                    node[key] = fallback
            else:
                _walk_clean(value, name_keys=name_keys, fallback=fallback, threshold=threshold, detected_log=detected_log)
    elif isinstance(node, list):
        for item in node:
            _walk_clean(item, name_keys=name_keys, fallback=fallback, threshold=threshold, detected_log=detected_log)
