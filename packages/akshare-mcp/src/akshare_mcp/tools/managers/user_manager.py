"""用户管理器"""

from typing import Any
import json
import os
import time
from ...storage import get_db
from ...services.kyc_dynamic import kyc_service
from ..manager_protocol import (
    ERR_AUTH,
    ERR_NOT_FOUND,
    ERR_PARAM,
    normalize_manager_payload,
    fail_with_meta,
    ok_with_meta,
)

try:  # pragma: no cover - auth context is only available when HTTP auth is enabled
    from mcp.server.auth.middleware.auth_context import get_access_token
except Exception:  # pragma: no cover - stdio/default fallback
    get_access_token = None


def _normalize_limit(value, default: int = 50, minimum: int = 1, maximum: int = 200) -> int:
    try:
        limit = int(value)
    except Exception:
        limit = default
    return max(minimum, min(limit, maximum))


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_settings_blob(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _sanitize_profile(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "settings": _load_settings_blob(user.get("settings")),
        "created_at": user.get("created_at"),
    }


def _authenticated_actor_user_id() -> str | None:
    if get_access_token is None:
        return None
    try:
        access_token = get_access_token()
    except Exception:
        return None
    if access_token is None:
        return None
    text = str(getattr(access_token, "client_id", "") or "").strip()
    return text or None


def _resolve_user_scope(payload: dict[str, Any]) -> tuple[str, str, bool]:
    actor_user_id = str(payload.get("actor_user_id") or payload.get("current_user_id") or "").strip()
    if not actor_user_id:
        actor_user_id = _authenticated_actor_user_id() or "default"

    requested_user_id = str(payload.get("user_id") or "").strip() or actor_user_id
    allow_cross_user = _env_flag("AKSHARE_USER_MANAGER_ALLOW_IMPERSONATION", default=False) and bool(
        payload.get("allow_cross_user") or payload.get("internal_call")
    )

    if requested_user_id != actor_user_id and not allow_cross_user:
        raise PermissionError("user_id 与当前调用身份不匹配，禁止跨用户访问")

    return requested_user_id, actor_user_id, allow_cross_user


async def _upsert_minimal_user(conn, user_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO users (id, username, settings, created_at, updated_at)
        VALUES ($1, $1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        user_id,
    )


def register_user_manager(mcp):
    """注册用户管理器工具"""

    @mcp.tool()
    async def user_manager(action: str, params: dict | None = None, kwargs: Any = None, user_id: str | None = None, actor_user_id: str | None = None, preferences: dict | None = None, limit: int | None = None, allow_cross_user: bool | None = None):
        """用户管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/get_profile/update_preferences/list/list_users/assess_kyc
            kwargs: JSON 字符串，不同 action 所需参数:
                - help: 无需额外参数
                - get_profile: user_id(str, optional, 默认 "default")
                - update_preferences: user_id(str, optional), preferences(dict)
                - list / list_users: 无需额外参数
                - assess_kyc: user_id(str, optional, 默认 "default")

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            user_manager(action="help", kwargs="{}")
            # 获取用户信息
            user_manager(action="get_profile", kwargs='{"user_id":"default"}')
            # 更新偏好设置
            user_manager(action="update_preferences", kwargs='{"preferences":{"theme":"dark","risk_level":"balanced"}}')
            # 列出用户
            user_manager(action="list", kwargs="{}")
            # 动态KYC评估
            user_manager(action="assess_kyc", kwargs='{"user_id":"default"}')
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            _params = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "user_id": user_id,
                    "actor_user_id": actor_user_id,
                    "preferences": preferences,
                    "limit": limit,
                    "allow_cross_user": allow_cross_user,
                },
            )

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="user_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None, error_code: str | None = None):
                return fail_with_meta(
                    message,
                    tool_name="user_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    error_code=error_code,
                )

            SUPPORTED_ACTIONS = {
                'get_profile': '获取用户信息',
                'update_preferences': '更新偏好设置',
                'list': '列出用户（别名）',
                'list_users': '列出用户（别名）',
                'assess_kyc': '动态KYC适当性评估',
                'help': '显示帮助信息',
            }

            if action == 'help':
                return _ok({'supported_actions': SUPPORTED_ACTIONS}, source_chain=['user_manager'])
            
            elif action == 'get_profile':
                resolved_user_id, resolved_actor_user_id, privileged = _resolve_user_scope(_params)
                async with db.acquire() as conn:
                    user = await conn.fetchrow(
                        "SELECT id, username, settings, created_at FROM users WHERE id = $1",
                        resolved_user_id
                    )
                    if not user:
                        profile = {
                            "id": resolved_user_id,
                            "username": resolved_user_id,
                            "settings": {},
                            "created_at": None,
                            "profile_exists": False,
                            "source_chain": ["user_manager", "synthetic_profile"],
                            "degraded": True,
                            "fallback_used": True,
                            "fallback_reason": "profile not found; synthetic profile returned",
                        }
                    else:
                        profile = _sanitize_profile(dict(user))
                        profile["profile_exists"] = True
                    profile["scope"] = {
                        "actor_user_id": resolved_actor_user_id,
                        "requested_user_id": resolved_user_id,
                        "cross_user": bool(privileged and resolved_user_id != resolved_actor_user_id),
                    }
                
                return _ok(profile, source_chain=['user_manager', 'db.users'])
            
            elif action == 'update_preferences':
                resolved_user_id, _resolved_actor_user_id, _privileged = _resolve_user_scope(_params)
                preferences = _params.get('preferences', {})
                if not isinstance(preferences, dict):
                    return _fail('preferences 必须为对象', source_chain=['user_manager'], error_code=ERR_PARAM)
                
                async with db.acquire() as conn:
                    await _upsert_minimal_user(conn, resolved_user_id)
                    row = await conn.fetchrow(
                        "SELECT settings FROM users WHERE id = $1",
                        resolved_user_id,
                    )
                    existing = row.get('settings') if isinstance(row, dict) else row['settings']
                    current_settings = _load_settings_blob(existing)
                    merged_settings = {**current_settings, **preferences}
                    await conn.execute(
                        "UPDATE users SET settings = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                        json.dumps(merged_settings), resolved_user_id
                    )
                return _ok(
                    {'user_id': resolved_user_id, 'updated': True, 'preferences': merged_settings},
                    source_chain=['user_manager', 'db.users'],
                )
            
            elif action in ['list', 'list_users']:
                resolved_user_id, _resolved_actor_user_id, privileged = _resolve_user_scope(_params)
                query_limit = _normalize_limit(_params.get('limit', 50))
                async with db.acquire() as conn:
                    if privileged and bool(_params.get("list_all")):
                        rows = await conn.fetch(
                            "SELECT id, username, created_at FROM users ORDER BY created_at DESC LIMIT $1",
                            query_limit
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT id, username, created_at FROM users WHERE id = $1 LIMIT 1",
                            resolved_user_id,
                        )
                    users = [dict(row) for row in rows]
                return _ok({'users': users, 'count': len(users)}, source_chain=['user_manager', 'db.users'])

            elif action == 'assess_kyc':
                resolved_user_id, _resolved_actor_user_id, _privileged = _resolve_user_scope(_params)
                result = await kyc_service.assess_risk_level(resolved_user_id, db)
                # Persist KYC level to users.settings
                async with db.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT settings FROM users WHERE id = $1", resolved_user_id,
                    )
                    settings = _load_settings_blob(row['settings']) if row and row['settings'] else {}
                    settings['kyc_level'] = result['kyc_level']
                    await conn.execute(
                        "UPDATE users SET settings = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                        json.dumps(settings), resolved_user_id,
                    )
                return _ok(result, source_chain=['user_manager', 'kyc_service', 'db.users'])

            else:
                return _fail(
                    f'Unknown action: {action}. Supported: {", ".join(SUPPORTED_ACTIONS.keys())}',
                    source_chain=['user_manager'],
                    error_code=ERR_PARAM,
                )
        except PermissionError as exc:
            return fail_with_meta(
                str(exc),
                tool_name='user_manager',
                action=action,
                started_at=start_time,
                source_chain=['user_manager'],
                error_code=ERR_AUTH,
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='user_manager',
                action=action,
                started_at=start_time,
                source_chain=['user_manager'],
            )
