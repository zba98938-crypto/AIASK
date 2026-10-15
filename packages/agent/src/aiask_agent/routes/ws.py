"""WebSocket route for real-time run event streaming.

轻量订阅模式:连接后定时轮询 session_store.list_run_events 增量推送,
与现有 SSE (/v1/runs/{run_id}/events) 同构,但提供双向长连接。
不引入 EventBus,多客户端各自维护 last_event_id watermark。

仅 ASGI (FastAPI/uvicorn) 模式可用;legacy-http (fallback_server) 对 /ws 返回 501。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..route_auth import is_loopback, token_matches
import logging

logger = logging.getLogger(__name__)


def _ws_authorized(websocket: WebSocket, token: str | None) -> bool:
    """WS 鉴权:loopback 放行;非 loopback 比对 AIASK_AGENT_API_TOKEN。

    浏览器 WebSocket 无法自定义 header,因此 token 通过 query param 传递,
    与 route_auth.api_authorized 的 loopback 假设一致。
    """
    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    if is_loopback(client_host):
        return True
    expected = str(os.getenv("AIASK_AGENT_API_TOKEN", "")).strip()
    if not expected:
        return True  # 未配置 token(loopback 模式),放行
    return token_matches(token, expected)


def create_ws_router(
    *,
    session_store: Any,
    normalize_run_event: Callable[[dict[str, Any]], dict[str, Any]],
    poll_interval: float = 0.5,
    heartbeat_interval: float = 15.0,
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def run_events_ws(
        websocket: WebSocket,
        run_id: str = "",
        after: int = 0,
        token: str | None = None,
    ) -> None:
        if not _ws_authorized(websocket, token):
            await websocket.close(code=4401)  # 自定义鉴权失败码
            return
        if not run_id:
            await websocket.accept()
            await websocket.send_json({"type": "error", "data": {"message": "run_id is required"}, "timestamp": int(time.time() * 1000)})
            await websocket.close(code=4400)
            return

        await websocket.accept()
        last_event_id = after
        last_heartbeat = time.monotonic()
        loop = asyncio.get_event_loop()

        try:
            while True:
                # list_run_events 是同步 SQLite 查询,放到线程池避免阻塞事件循环
                events = await loop.run_in_executor(
                    None, lambda: session_store.list_run_events(run_id, after_event_id=last_event_id)
                )
                if events:
                    for raw in events:
                        event = normalize_run_event(raw)
                        try:
                            eid = int(event.get("id") or 0)
                            if eid > last_event_id:
                                last_event_id = eid
                        except (TypeError, ValueError) as _swallowed:
                            logger.warning("packages/agent/src/aiask_agent/routes/ws.py:82: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                            pass
                        await websocket.send_json({
                            "type": "run_event",
                            "data": event,
                            "timestamp": int(time.time() * 1000),
                        })

                # 心跳:防止反向代理掐空闲连接,同时给客户端 connected 信号
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_interval:
                    await websocket.send_json({"type": "ping", "data": {"run_id": run_id}, "timestamp": int(time.time() * 1000)})
                    last_heartbeat = now

                await asyncio.sleep(poll_interval)
        except WebSocketDisconnect as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/routes/ws.py:97: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return  # 客户端正常断开
        except Exception:  # noqa: BLE001 - WS 长连接需兜底,避免协程泄漏
            try:
                await websocket.close()
            except Exception as _swallowed:
                logger.warning("packages/agent/src/aiask_agent/routes/ws.py:102: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
            return

    return router
