"""自选股管理器"""

from typing import Any
import json
import uuid

from ...storage import get_db
from ...utils import ok, fail
from ..manager_protocol import normalize_manager_payload


DEFAULT_GROUP_ID = "default"
DEFAULT_GROUP_NAME = "我的自选"
DEFAULT_GROUP_COLOR = "#6366f1"


def _normalize_kwargs(kwargs: dict) -> dict:
    """统一解析 kwargs 参数（兼容 params / JSON 字符串 kwargs / dict kwargs）。"""
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}

    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass

    if "code" not in kwargs or not kwargs.get("code"):
        kwargs["code"] = kwargs.get("stock_code") or kwargs.get("symbol")

    if "group_id" not in kwargs or not kwargs.get("group_id"):
        kwargs["group_id"] = kwargs.get("group") or kwargs.get("watchlist_name") or DEFAULT_GROUP_ID

    if "group_name" not in kwargs or not kwargs.get("group_name"):
        kwargs["group_name"] = kwargs.get("name") or kwargs.get("groupName")

    return kwargs


def _normalize_codes(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [item.strip() for item in text.split(",") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _safe_group_id(value) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_GROUP_ID


def _safe_group_name(value, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


async def _ensure_group(conn, user_id: str, group_id: str, group_name: str | None = None, color: str | None = None) -> None:
    if group_id == DEFAULT_GROUP_ID:
        return
    await conn.execute(
        """
        INSERT INTO watchlist_groups (id, name, user_id, color, sort_order, created_at)
        VALUES ($1, $2, $3, COALESCE($4, $5), COALESCE((
          SELECT MAX(sort_order) + 1 FROM watchlist_groups WHERE user_id = $3
        ), 1), CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO UPDATE SET
          user_id = COALESCE(NULLIF(EXCLUDED.user_id, ''), watchlist_groups.user_id),
          name = COALESCE(NULLIF(EXCLUDED.name, ''), watchlist_groups.name),
          color = COALESCE(NULLIF(EXCLUDED.color, ''), watchlist_groups.color)
        """,
        group_id,
        _safe_group_name(group_name, group_id),
        user_id,
        str(color).strip() if color is not None else None,
        DEFAULT_GROUP_COLOR,
    )


def _group_sort_key(group: dict) -> tuple[int, str]:
    return (int(group.get("sort_order") or 0), str(group.get("name") or ""))


def register_watchlist_manager(mcp):
    """注册自选股管理器工具"""

    @mcp.tool()
    async def watchlist_manager(action: str, params: dict | None = None, kwargs: Any = None, user_id: str | None = None, group_id: str | None = None, code: str | None = None, codes: list[str] | None = None, name: str | None = None, note: str | None = None, color: str | None = None, limit: int | None = None, sort_order: int | None = None):
        """自选股管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/list/create_group/delete_group/add_stocks/add/remove/remove_stock/reorder
            kwargs: 支持 structured ``params``、JSON 字符串 ``kwargs`` 或关键字参数

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}
        """
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "user_id": user_id,
                    "group_id": group_id,
                    "code": code,
                    "codes": codes,
                    "name": name,
                    "note": note,
                    "color": color,
                    "limit": limit,
                    "sort_order": sort_order,
                },
            )
            user_id = str(kwargs.get("user_id") or "default").strip() or "default"
            # P2-4.2.6/4.2.7 fix: 用 user_scope 标准化 + emit warning(诊断报告 §4.2.6)
            from ...services.user_scope import require_user_id_or_warn
            resolved_user_id, scope_warnings = require_user_id_or_warn(kwargs)
            user_id = resolved_user_id

            if action == "list":
                async with db.acquire() as conn:
                    group_rows = await conn.fetch(
                        """
                        SELECT id, name, user_id, color, sort_order, created_at
                        FROM watchlist_groups
                        WHERE COALESCE(user_id, 'default') = $1
                           OR (COALESCE(user_id, 'default') = 'default' AND id = $2)
                        ORDER BY sort_order ASC, created_at ASC
                        """,
                        user_id,
                        DEFAULT_GROUP_ID,
                    )
                    item_rows = await conn.fetch(
                        """
                        SELECT id, user_id, code, name, group_id, sort_order, note, added_at
                        FROM watchlist
                        WHERE user_id = $1
                        ORDER BY group_id ASC, sort_order ASC, added_at DESC
                        """,
                        user_id,
                    )

                groups: dict[str, dict] = {}
                for row in group_rows:
                    item = dict(row)
                    group_id = _safe_group_id(item.get("id"))
                    groups[group_id] = {
                        "id": group_id,
                        "name": _safe_group_name(item.get("name"), DEFAULT_GROUP_NAME if group_id == DEFAULT_GROUP_ID else group_id),
                        "color": str(item.get("color") or DEFAULT_GROUP_COLOR),
                        "sort_order": int(item.get("sort_order") or 0),
                        "created_at": item.get("created_at"),
                        "items": [],
                    }

                if DEFAULT_GROUP_ID not in groups:
                    groups[DEFAULT_GROUP_ID] = {
                        "id": DEFAULT_GROUP_ID,
                        "name": DEFAULT_GROUP_NAME,
                        "color": DEFAULT_GROUP_COLOR,
                        "sort_order": 0,
                        "created_at": None,
                        "items": [],
                    }

                try:
                    from ...data_source import data_source
                except Exception:
                    data_source = None

                for row in item_rows:
                    item = dict(row)
                    group_id = _safe_group_id(item.get("group_id"))
                    if group_id not in groups:
                        groups[group_id] = {
                            "id": group_id,
                            "name": group_id,
                            "color": DEFAULT_GROUP_COLOR,
                            "sort_order": 999,
                            "created_at": item.get("added_at"),
                            "items": [],
                        }

                    name = item.get("name")
                    if (not name) and data_source is not None:
                        try:
                            name = data_source._get_stock_name(item.get("code", ""))
                        except Exception:
                            name = None

                    groups[group_id]["items"].append(
                        {
                            "code": str(item.get("code") or ""),
                            "name": str(name or ""),
                            "group_id": group_id,
                            "group": group_id,
                            "added_at": item.get("added_at"),
                            "sort_order": int(item.get("sort_order") or 0),
                            "sortOrder": int(item.get("sort_order") or 0),
                            "note": item.get("note"),
                        }
                    )

                group_list = sorted(groups.values(), key=_group_sort_key)
                for group in group_list:
                    group["items"] = sorted(
                        group["items"],
                        key=lambda item: (int(item.get("sort_order") or 0), str(item.get("code") or "")),
                    )

                return ok({"groups": group_list, "count": len(group_list)})

            if action == "create_group":
                group_id = _safe_group_id(kwargs.get("group_id"))
                if group_id == DEFAULT_GROUP_ID and str(kwargs.get("group_name") or kwargs.get("name") or "").strip():
                    group_id = str(kwargs.get("id") or f"group_{uuid.uuid4().hex[:8]}")
                group_name = _safe_group_name(kwargs.get("group_name") or kwargs.get("name"), group_id)
                color = str(kwargs.get("color") or DEFAULT_GROUP_COLOR)

                async with db.acquire() as conn:
                    await _ensure_group(conn, user_id, group_id, group_name, color)

                return ok({"group_id": group_id, "name": group_name, "color": color, "created": True})

            if action == "delete_group":
                group_id = _safe_group_id(kwargs.get("group_id"))
                group_name = str(kwargs.get("group_name") or kwargs.get("name") or "").strip()
                if group_id == DEFAULT_GROUP_ID and group_name:
                    async with db.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT id FROM watchlist_groups WHERE user_id = $1 AND name = $2",
                            user_id,
                            group_name,
                        )
                    if row:
                        group_id = _safe_group_id(dict(row).get("id"))

                if group_id == DEFAULT_GROUP_ID:
                    return fail("默认分组不可删除")

                async with db.acquire() as conn:
                    await conn.execute(
                        "UPDATE watchlist SET group_id = $1, sort_order = 0 WHERE user_id = $2 AND group_id = $3",
                        DEFAULT_GROUP_ID,
                        user_id,
                        group_id,
                    )
                    await conn.execute(
                        "DELETE FROM watchlist_groups WHERE user_id = $1 AND id = $2",
                        user_id,
                        group_id,
                    )

                return ok({"group_id": group_id, "deleted": True})

            if action in {"add_stocks", "add"}:
                codes = _normalize_codes(kwargs.get("codes"))
                if not codes and kwargs.get("code"):
                    codes = [str(kwargs.get("code")).strip()]
                codes = [code for code in codes if code]
                if not codes:
                    return fail("需要提供 code 或 codes 参数（股票代码）")

                # F-N32-1 fix (诊断报告 §N32): add_stocks 必须校验代码合法性。
                # 历史问题: ZZZ999/BADCODE 等任意字符串直接入库，自选股变成垃圾桶。
                # 修复: 逐个用 resolve_existing_security_code_async 校验存在性，
                # 非法/不存在的代码拒绝入库并回显，已存在的标准化后入库。
                from ...utils import resolve_existing_security_code_async

                validated_codes: list[str] = []
                invalid_codes: list[dict[str, str]] = []
                for raw in codes:
                    resolved, _info, err = await resolve_existing_security_code_async(raw)
                    if err or not resolved:
                        invalid_codes.append({"code": raw, "reason": err or "代码不存在"})
                    else:
                        if resolved not in validated_codes:
                            validated_codes.append(resolved)
                if invalid_codes:
                    return fail(
                        "以下代码非法或不存在，已拒绝入库（自选股不接受未知代码）："
                        + ", ".join(f"{item['code']}({item['reason']})" for item in invalid_codes)
                    )
                codes = validated_codes

                group_id = _safe_group_id(kwargs.get("group_id"))
                group_name = str(kwargs.get("group_name") or kwargs.get("name") or group_id).strip() or group_id
                color = str(kwargs.get("color")).strip() if kwargs.get("color") is not None else None

                async with db.acquire() as conn:
                    await _ensure_group(conn, user_id, group_id, group_name, color)
                    for index, code in enumerate(codes):
                        await conn.execute(
                            """
                            INSERT INTO watchlist (user_id, code, group_id, sort_order, note, added_at)
                            VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                            ON CONFLICT (user_id, code) DO UPDATE SET
                              group_id = EXCLUDED.group_id,
                              sort_order = EXCLUDED.sort_order,
                              note = EXCLUDED.note
                            """,
                            user_id,
                            code,
                            group_id,
                            index,
                            kwargs.get("note", ""),
                        )

                return ok({"group_id": group_id, "codes": codes, "added": True, "count": len(codes)})

            if action in {"remove_stock", "remove"}:
                code = str(kwargs.get("code") or "").strip()
                if not code:
                    return fail("需要提供 code 参数（股票代码）")

                async with db.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM watchlist WHERE user_id = $1 AND code = $2",
                        user_id,
                        code,
                    )
                return ok({"code": code, "removed": True})

            if action == "reorder":
                codes = _normalize_codes(kwargs.get("codes"))
                group_id = _safe_group_id(kwargs.get("group_id"))
                if not codes:
                    return fail("需要提供 codes 参数")

                async with db.acquire() as conn:
                    for index, code in enumerate(codes):
                        await conn.execute(
                            """
                            UPDATE watchlist
                            SET sort_order = $1
                            WHERE user_id = $2 AND code = $3 AND group_id = $4
                            """,
                            index,
                            user_id,
                            code,
                            group_id,
                        )
                return ok({"group_id": group_id, "codes": codes, "reordered": True})

            if action == "help":
                return ok(
                    {
                        "supported_actions": {
                            "list": "列出分组与分组内股票",
                            "create_group": "创建分组（需要 group_id/name，可选 color）",
                            "delete_group": "删除分组（默认分组不可删除）",
                            "add_stocks": "添加股票到分组（需要 group_id, codes）",
                            "remove_stock": "删除股票（需要 code）",
                            "reorder": "更新分组内排序（需要 group_id, codes）",
                            "add": "向默认分组添加单只股票（兼容别名）",
                            "remove": "删除单只股票（兼容别名）",
                            "help": "显示帮助信息",
                        }
                    }
                )

            return fail("Unknown action: %s. Supported: list, create_group, delete_group, add_stocks, remove_stock, reorder, add, remove, help" % action)
        except Exception as e:
            return fail(str(e))
