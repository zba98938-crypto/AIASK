"""用户 scope 标准化(诊断报告 §4.2.6 / §4.2.7 P2-4.2.6/4.2.7 修复)。

历史问题:
- alerts/watchlist/screener/save_strategy 4 个 manager 不传 user_id 时落到 'default' scope
- list_accounts / watchlist.list 不传 user_id 默认看不到当前用户
- AI Agent 写一个用户的数据,下次读取又看到全局 default 池,数据隔离失效

修复:
- 提供统一 require_user_id_or_warn helper
- 不传时尝试 from os.environ['AIASK_DEFAULT_USER_ID']
- 仍空 → 标 warning 'user_id_not_provided',不 silent 落 'default'
"""
from __future__ import annotations

import os
from typing import Any


def require_user_id_or_warn(
    kwargs: dict | None,
    *,
    explicit_user_id: str | None = None,
    fallback_default: str | None = None,
) -> tuple[str, list[str]]:
    """检查 kwargs / explicit / env 中的 user_id,返回 (resolved, warnings)。

    优先级:
        1. explicit_user_id 参数
        2. kwargs['user_id'] / kwargs['userId']
        3. 环境变量 AIASK_DEFAULT_USER_ID
        4. fallback_default 参数
        5. 'default'(并 emit warning)

    Args:
        kwargs: tool 入参字典(可能为 None)
        explicit_user_id: 顶层显式参数(优先级最高)
        fallback_default: 调用方指定的 fallback,在 env 缺失时使用

    Returns:
        (user_id, warnings):
        - 若解析到非 'default' 值 → warnings=[]
        - 若回退到 'default' → warnings=['user_id_not_provided_falling_back_to_default']
    """
    warnings: list[str] = []
    candidates = [
        explicit_user_id,
        (kwargs or {}).get("user_id") if kwargs else None,
        (kwargs or {}).get("userId") if kwargs else None,
        os.getenv("AIASK_DEFAULT_USER_ID"),
        fallback_default,
    ]
    for cand in candidates:
        if cand and str(cand).strip() and str(cand).strip().lower() != "default":
            return str(cand).strip(), warnings
    warnings.append(
        "user_id_not_provided_falling_back_to_default — "
        "建议显式传 user_id,避免数据落到全局 default scope。"
        "可通过环境变量 AIASK_DEFAULT_USER_ID 设置默认值。"
    )
    return "default", warnings


def resolve_scope_for_list(
    kwargs: dict | None,
    *,
    explicit_user_id: str | None = None,
) -> tuple[str | None, str, list[str]]:
    """list 类查询的 scope 解析(诊断报告 §4.2.7)。

    历史问题:list_accounts / watchlist.list 不传 user_id 默认 'default' scope,
    AI 看不到自己创建的账户。

    Returns:
        (user_id_filter, scope_kind, warnings):
        - user_id_filter: db 查询用 WHERE user_id = ?,'all' 表示不 filter
        - scope_kind: 'specific' / 'inherited' / 'all'
        - warnings: 提示信息
    """
    warnings: list[str] = []
    explicit = explicit_user_id or (kwargs or {}).get("user_id")
    if explicit and str(explicit).strip().lower() not in ("", "default"):
        return str(explicit).strip(), "specific", warnings

    inherited = os.getenv("AIASK_DEFAULT_USER_ID")
    if inherited and inherited.strip().lower() != "default":
        warnings.append(f"user_id_inherited_from_env: {inherited}")
        return inherited.strip(), "inherited", warnings

    # 未指定 user_id 也无 env,返回 None 表示不 filter(scope=all)
    warnings.append(
        "user_id_not_specified_listing_all_scopes — 当前查询会返回所有用户的数据。"
        "若仅想看自己的数据,请显式传 user_id 或设置 AIASK_DEFAULT_USER_ID。"
    )
    return None, "all", warnings
