"""Live broker gateway adapters with safe default read-only mode."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..env_loader import load_mcp_env

_TERMINAL_EVENT_STATUSES = {"filled", "cancelled", "rejected"}

from ._live_broker_support import *
from ._live_broker_support import (
    _as_list,
    _build_broker_receipt,
    _coerce_bool,
    _dig,
    _normalize_broker_fill,
    _normalize_symbol,
    _safe_float,
    _safe_int,
    _safe_text,
)

class LiveBrokerError(RuntimeError):
    """Raised when the broker gateway request fails."""

    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

@dataclass(slots=True)
class LiveBrokerConfig:
    enabled: bool = False
    broker: str = "alpaca"
    base_url: str = ""
    api_key: str = ""
    api_secret: str = ""
    account_id: str = ""
    timeout_sec: float = 15.0
    connect_timeout_sec: float = 5.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    read_only: bool = True
    allow_write: bool = False
    paper: bool = True

    @classmethod
    def from_env(cls) -> "LiveBrokerConfig":
        load_mcp_env(
            override=False,
            only_prefixes=("LIVE_TRADING_", "BROKER_", "ALPACA_", "APCA_", "TRADIER_"),
        )

        def _env_text(name: str, *fallbacks: str, default: str = "") -> str:
            for key in (name, *fallbacks):
                raw = str(os.getenv(key, "") or "").strip()
                if raw:
                    return raw
            return str(default or "").strip()

        def _env_float(name: str, *fallbacks: str, default: float) -> float:
            text = _env_text(name, *fallbacks, default=str(default))
            try:
                return float(text)
            except Exception:
                return float(default)

        def _env_bool(name: str, *fallbacks: str, default: bool) -> bool:
            for key in (name, *fallbacks):
                raw = os.getenv(key)
                if raw is None:
                    continue
                return _coerce_bool(raw, default)
            return bool(default)

        broker = _env_text("LIVE_TRADING_BROKER", "BROKER_PROVIDER", default="alpaca").lower() or "alpaca"
        paper = _env_bool("LIVE_TRADING_PAPER", "BROKER_PAPER", "ALPACA_PAPER", "TRADIER_PAPER", default=True)

        if broker == "tradier":
            base_url = _env_text(
                "LIVE_TRADING_BASE_URL",
                "BROKER_BASE_URL",
                "TRADIER_BASE_URL",
                default="https://sandbox.tradier.com/v1" if paper else "https://api.tradier.com/v1",
            )
            api_key = _env_text(
                "LIVE_TRADING_API_KEY",
                "TRADIER_ACCESS_TOKEN",
                "TRADIER_TOKEN",
                "TRADIER_API_TOKEN",
            )
            api_secret = _env_text("LIVE_TRADING_API_SECRET", "TRADIER_API_SECRET", default=api_key)
            account_id = _env_text("LIVE_TRADING_ACCOUNT_ID", "BROKER_ACCOUNT_ID", "TRADIER_ACCOUNT_ID")
        else:
            base_url = _env_text(
                "LIVE_TRADING_BASE_URL",
                "BROKER_BASE_URL",
                "ALPACA_BASE_URL",
                default="https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets",
            )
            api_key = _env_text("LIVE_TRADING_API_KEY", "ALPACA_API_KEY", "APCA_API_KEY_ID")
            api_secret = _env_text("LIVE_TRADING_API_SECRET", "ALPACA_API_SECRET", "APCA_API_SECRET_KEY")
            account_id = _env_text("LIVE_TRADING_ACCOUNT_ID", "BROKER_ACCOUNT_ID")

        allow_write = _env_bool("LIVE_TRADING_ALLOW_WRITE", "BROKER_ALLOW_WRITE", default=False)
        read_only = _env_bool("LIVE_TRADING_READ_ONLY", "BROKER_READ_ONLY", default=not allow_write)
        configured_default = bool(base_url and api_key and (broker == "tradier" or api_secret))
        enabled = _env_bool("LIVE_TRADING_ENABLED", "BROKER_ENABLED", default=configured_default)
        timeout_sec = max(5.0, _env_float("LIVE_TRADING_TIMEOUT_SEC", "BROKER_TIMEOUT_SEC", default=15.0))
        return cls(
            enabled=enabled,
            broker=broker,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            api_secret=api_secret,
            account_id=account_id,
            timeout_sec=timeout_sec,
            connect_timeout_sec=max(1.0, _env_float("LIVE_TRADING_CONNECT_TIMEOUT_SEC", default=min(timeout_sec, 5.0))),
            write_timeout_sec=max(1.0, _env_float("LIVE_TRADING_WRITE_TIMEOUT_SEC", default=min(timeout_sec, 10.0))),
            pool_timeout_sec=max(1.0, _env_float("LIVE_TRADING_POOL_TIMEOUT_SEC", default=min(timeout_sec, 5.0))),
            read_only=read_only,
            allow_write=allow_write,
            paper=paper,
        )

    def is_configured(self) -> bool:
        if self.broker == "tradier":
            return bool(self.enabled and self.base_url and (self.api_key or self.api_secret))
        return bool(self.enabled and self.base_url and self.api_key and self.api_secret)

    def can_write(self) -> bool:
        return bool(self.is_configured() and self.allow_write and not self.read_only)

class BaseBrokerAdapter:
    """Common async broker adapter surface."""

    provider_name = "base"

    def __init__(self, config: Optional[LiveBrokerConfig] = None):
        self.config = config or LiveBrokerConfig.from_env()
        self._client = httpx.AsyncClient(follow_redirects=True, http2=False)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    def is_configured(self) -> bool:
        return self.config.is_configured()

    def can_write(self) -> bool:
        return self.config.can_write()

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "multi_broker": True,
            "order_events": True,
            "fills": True,
            "broker_receipts": True,
            "write_enabled": self.can_write(),
        }

    def _timeout(self) -> httpx.Timeout:
        timeout = max(float(self.config.timeout_sec or 15.0), 5.0)
        return httpx.Timeout(
            connect=max(1.0, min(float(self.config.connect_timeout_sec or timeout), timeout)),
            read=timeout,
            write=max(1.0, min(float(self.config.write_timeout_sec or timeout), timeout)),
            pool=max(1.0, min(float(self.config.pool_timeout_sec or timeout), timeout)),
        )

    async def gateway_status(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_account(self) -> dict[str, Any]:
        raise NotImplementedError

    async def list_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def list_orders(
        self,
        *,
        status: str = "open",
        limit: int = 50,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def list_order_events(self, *, order_id: str, limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def list_fills(
        self,
        *,
        order_id: str | None = None,
        limit: int = 50,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_order_receipt(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

class AlpacaBrokerAdapter(BaseBrokerAdapter):
    """Alpaca Trading API adapter.

    Verified against Alpaca Trading API account/orders/positions endpoints.
    """

    provider_name = "alpaca"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "APCA-API-KEY-ID": self.config.api_key,
            "APCA-API-SECRET-KEY": self.config.api_secret,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_write: bool = False,
    ) -> Any:
        if not self.is_configured():
            raise LiveBrokerError("live broker gateway not configured")
        if allow_write and not self.can_write():
            raise LiveBrokerError("live broker gateway is in read-only mode")
        url = f"{self.config.base_url}{path}"
        response = await self._client.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            headers=self._headers(),
            timeout=self._timeout(),
        )
        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            message = None
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("error") or "").strip() or None
            if message is None:
                message = str(response.text or f"{response.status_code} request failed").strip()
            raise LiveBrokerError(message, status_code=response.status_code, payload=payload)
        if response.status_code == 204 or not response.text:
            return {}
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    @staticmethod
    def _normalize_account(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": "alpaca",
            "source": "alpaca.account",
            "account_id": _safe_text(payload.get("id"), payload.get("account_id")) or "",
            "status": payload.get("status"),
            "currency": payload.get("currency"),
            "buying_power": _safe_float(payload.get("buying_power")),
            "cash": _safe_float(payload.get("cash")),
            "portfolio_value": _safe_float(payload.get("portfolio_value")),
            "equity": _safe_float(payload.get("equity")),
            "last_equity": _safe_float(payload.get("last_equity")),
            "pattern_day_trader": bool(payload.get("pattern_day_trader")),
            "trading_blocked": bool(payload.get("trading_blocked")),
            "transfers_blocked": bool(payload.get("transfers_blocked")),
            "account_blocked": bool(payload.get("account_blocked")),
            "created_at": payload.get("created_at"),
            "raw": payload,
        }

    @staticmethod
    def _normalize_position(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": "alpaca",
            "source": "alpaca.positions",
            "symbol": _normalize_symbol(payload.get("symbol")) or "",
            "asset_id": payload.get("asset_id"),
            "exchange": payload.get("exchange"),
            "asset_class": payload.get("asset_class"),
            "side": payload.get("side"),
            "qty": _safe_float(payload.get("qty")),
            "avg_entry_price": _safe_float(payload.get("avg_entry_price")),
            "market_value": _safe_float(payload.get("market_value")),
            "cost_basis": _safe_float(payload.get("cost_basis")),
            "current_price": _safe_float(payload.get("current_price")),
            "lastday_price": _safe_float(payload.get("lastday_price")),
            "unrealized_pl": _safe_float(payload.get("unrealized_pl")),
            "unrealized_plpc": _safe_float(payload.get("unrealized_plpc")),
            "change_today": _safe_float(payload.get("change_today")),
            "raw": payload,
        }

    @staticmethod
    def _normalize_order(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": "alpaca",
            "source": "alpaca.orders",
            "account_id": None,
            "order_id": _safe_text(payload.get("id"), payload.get("order_id")) or "",
            "client_order_id": payload.get("client_order_id"),
            "symbol": _normalize_symbol(payload.get("symbol")),
            "asset_id": payload.get("asset_id"),
            "asset_class": payload.get("asset_class"),
            "side": payload.get("side"),
            "order_type": payload.get("order_type") or payload.get("type"),
            "time_in_force": payload.get("time_in_force"),
            "status": _safe_text(payload.get("status")) or "unknown",
            "qty": _safe_float(payload.get("qty")),
            "filled_qty": _safe_float(payload.get("filled_qty")),
            "filled_avg_price": _safe_float(payload.get("filled_avg_price")),
            "limit_price": _safe_float(payload.get("limit_price")),
            "stop_price": _safe_float(payload.get("stop_price")),
            "notional": _safe_float(payload.get("notional")),
            "submitted_at": payload.get("submitted_at"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "filled_at": payload.get("filled_at"),
            "extended_hours": bool(payload.get("extended_hours")),
            "raw": payload,
        }

    async def gateway_status(self) -> dict[str, Any]:
        result = {
            "provider": self.provider_name,
            "configured": self.is_configured(),
            "enabled": bool(self.config.enabled),
            "paper": bool(self.config.paper),
            "base_url": self.config.base_url,
            "read_only": bool(self.config.read_only or not self.config.allow_write),
            "allow_write": bool(self.config.allow_write),
            "capabilities": self.capabilities(),
            "connected": False,
            "account": None,
            "clock": None,
            "error": None,
        }
        if not self.is_configured():
            result["error"] = "broker gateway not configured"
            return result
        try:
            account_payload = await self._request("GET", "/v2/account")
            result["account"] = self._normalize_account(account_payload if isinstance(account_payload, dict) else {})
            try:
                clock_payload = await self._request("GET", "/v2/clock")
                result["clock"] = {
                    "timestamp": clock_payload.get("timestamp"),
                    "is_open": bool(clock_payload.get("is_open")),
                    "next_open": clock_payload.get("next_open"),
                    "next_close": clock_payload.get("next_close"),
                }
            except Exception as exc:
                result["clock"] = {"error": str(exc)}
            result["connected"] = True
            return result
        except Exception as exc:
            result["error"] = str(exc)
            return result

    async def get_account(self) -> dict[str, Any]:
        payload = await self._request("GET", "/v2/account")
        return self._normalize_account(payload if isinstance(payload, dict) else {})

    async def list_positions(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/v2/positions")
        rows = payload if isinstance(payload, list) else []
        return [self._normalize_position(dict(item or {})) for item in rows if isinstance(item, dict)]

    async def list_orders(
        self,
        *,
        status: str = "open",
        limit: int = 50,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "status": _safe_text(status) or "open",
            "limit": max(1, min(int(limit or 50), 500)),
            "nested": "true",
        }
        if symbols:
            params["symbols"] = ",".join(str(item).strip() for item in symbols if str(item).strip())
        payload = await self._request("GET", "/v2/orders", params=params)
        rows = payload if isinstance(payload, list) else []
        return [self._normalize_order(dict(item or {})) for item in rows if isinstance(item, dict)]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/v2/orders/{order_id}")
        return self._normalize_order(payload if isinstance(payload, dict) else {})

    async def list_order_events(self, *, order_id: str, limit: int = 50) -> list[dict[str, Any]]:
        order = await self.get_order(order_id)
        fills = await self.list_fills(order_id=order_id, limit=limit)
        receipt = await self.get_order_receipt(order_id)
        return build_live_order_events(order, fills=fills, receipt=receipt)[: max(1, int(limit))]

    async def list_fills(
        self,
        *,
        order_id: str | None = None,
        limit: int = 50,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "direction": "desc",
            "page_size": max(1, min(int(limit or 50), 200)),
        }
        payload = await self._request("GET", "/v2/account/activities/FILL", params=params)
        rows = payload if isinstance(payload, list) else []
        normalized = [
            _normalize_broker_fill(
                self.provider_name,
                dict(item or {}),
                order_id=order_id,
                symbol=_safe_text((item or {}).get("symbol")),
                source="alpaca.account_activities",
            )
            for item in rows
            if isinstance(item, dict)
        ]
        if order_id:
            normalized = [item for item in normalized if _safe_text(item.get("order_id")) == _safe_text(order_id)]
        if symbols:
            target_symbols = {_normalize_symbol(item) for item in symbols if _normalize_symbol(item)}
            normalized = [item for item in normalized if _normalize_symbol(item.get("symbol")) in target_symbols]
        return normalized[: max(1, int(limit))]

    async def get_order_receipt(self, order_id: str) -> dict[str, Any]:
        order = await self.get_order(order_id)
        return _build_broker_receipt(self.provider_name, order, source="alpaca.order_receipt")

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "symbol": _safe_text(payload.get("symbol"), payload.get("code")) or "",
            "side": (_safe_text(payload.get("side"), payload.get("direction"), "buy") or "buy").lower(),
            "type": (_safe_text(payload.get("type"), payload.get("order_type"), "market") or "market").lower(),
            "time_in_force": (_safe_text(payload.get("time_in_force"), "day") or "day").lower(),
        }
        qty = payload.get("qty")
        if qty in (None, ""):
            qty = payload.get("quantity")
        notional = payload.get("notional")
        if qty not in (None, ""):
            normalized["qty"] = str(qty)
        elif notional not in (None, ""):
            normalized["notional"] = str(notional)
        else:
            raise LiveBrokerError("submit_order requires qty or notional")
        if not normalized["symbol"]:
            raise LiveBrokerError("submit_order requires symbol/code")
        if payload.get("limit_price") not in (None, ""):
            normalized["limit_price"] = str(payload.get("limit_price"))
        if payload.get("stop_price") not in (None, ""):
            normalized["stop_price"] = str(payload.get("stop_price"))
        if payload.get("client_order_id"):
            normalized["client_order_id"] = _safe_text(payload.get("client_order_id"))
        if payload.get("extended_hours") is not None:
            normalized["extended_hours"] = bool(payload.get("extended_hours"))
        if payload.get("order_class"):
            normalized["order_class"] = (_safe_text(payload.get("order_class")) or "").lower()
        response = await self._request("POST", "/v2/orders", json_body=normalized, allow_write=True)
        return self._normalize_order(response if isinstance(response, dict) else {})

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        payload = await self._request("DELETE", f"/v2/orders/{order_id}", allow_write=True)
        return {
            "order_id": _safe_text(order_id) or "",
            "cancelled": True,
            "response": payload if isinstance(payload, dict) else {},
        }

class TradierBrokerAdapter(BaseBrokerAdapter):
    """Tradier Brokerage API adapter."""

    provider_name = "tradier"

    def __init__(self, config: Optional[LiveBrokerConfig] = None):
        super().__init__(config)
        self._resolved_account_id: str | None = None
        self._profile_cache: dict[str, Any] | None = None

    def _token(self) -> str:
        token = _safe_text(self.config.api_key, self.config.api_secret)
        if not token:
            raise LiveBrokerError("tradier access token not configured")
        return token

    def _headers(self, *, form: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token()}",
        }
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        allow_write: bool = False,
    ) -> Any:
        if not self.is_configured():
            raise LiveBrokerError("live broker gateway not configured")
        if allow_write and not self.can_write():
            raise LiveBrokerError("live broker gateway is in read-only mode")
        url = f"{self.config.base_url}{path}"
        response = await self._client.request(
            method=method.upper(),
            url=url,
            params=params,
            data=form_body,
            headers=self._headers(form=form_body is not None),
            timeout=self._timeout(),
        )
        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            message = None
            if isinstance(payload, dict):
                errors = payload.get("errors")
                if isinstance(errors, dict):
                    message = _safe_text(errors.get("error"), errors.get("message"))
                if message is None:
                    message = _safe_text(payload.get("message"), payload.get("error"))
            if message is None:
                message = str(response.text or f"{response.status_code} request failed").strip()
            raise LiveBrokerError(message, status_code=response.status_code, payload=payload)
        if response.status_code == 204 or not response.text:
            return {}
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    async def _get_profile(self) -> dict[str, Any]:
        if self._profile_cache is not None:
            return dict(self._profile_cache)
        payload = await self._request("GET", "/user/profile")
        profile = dict(payload.get("profile") or payload) if isinstance(payload, dict) else {}
        self._profile_cache = dict(profile)
        return profile

    async def _resolve_account_id(self) -> str:
        if _safe_text(self._resolved_account_id):
            return self._resolved_account_id or ""
        if _safe_text(self.config.account_id):
            self._resolved_account_id = self.config.account_id
            return self._resolved_account_id
        profile = await self._get_profile()
        account_candidates = []
        account_candidates.extend(_as_list(profile.get("account")))
        account_candidates.extend(_as_list(_dig(profile, "accounts", "account")))
        account_candidates.extend(_as_list(profile.get("accounts")))
        for item in account_candidates:
            account_id = _safe_text(item.get("account_number"), item.get("account_id"), item.get("id"), item.get("number"))
            if account_id:
                self._resolved_account_id = account_id
                return account_id
        raise LiveBrokerError("tradier account_id not configured and could not be discovered from profile")

    @staticmethod
    def _normalize_account(profile: dict[str, Any], balances_payload: dict[str, Any], account_id: str) -> dict[str, Any]:
        balances = dict(balances_payload.get("balances") or balances_payload or {})
        margin = dict(balances.get("margin") or {})
        cash_block = dict(balances.get("cash") or {})
        buying_power = _safe_float(
            margin.get("stock_buying_power")
            or margin.get("option_buying_power")
            or balances.get("buying_power")
        )
        cash = _safe_float(
            cash_block.get("cash_available")
            or cash_block.get("total_cash")
            or balances.get("cash")
        )
        total_equity = _safe_float(balances.get("total_equity") or balances.get("equity"))
        return {
            "provider": "tradier",
            "source": "tradier.balances",
            "account_id": account_id,
            "status": _safe_text(balances.get("status"), _dig(profile, "status")) or "active",
            "currency": _safe_text(balances.get("currency")) or "USD",
            "buying_power": buying_power,
            "cash": cash,
            "portfolio_value": total_equity,
            "equity": total_equity,
            "last_equity": None,
            "pattern_day_trader": bool(balances.get("pattern_day_trader")),
            "trading_blocked": bool(balances.get("trading_blocked") or balances.get("account_blocked")),
            "transfers_blocked": False,
            "account_blocked": bool(balances.get("account_blocked")),
            "created_at": None,
            "raw": {
                "profile": profile,
                "balances": balances_payload,
            },
        }

    @staticmethod
    def _normalize_position(payload: dict[str, Any]) -> dict[str, Any]:
        qty = _safe_float(payload.get("quantity") or payload.get("qty"))
        cost_basis = _safe_float(payload.get("cost_basis"))
        market_value = _safe_float(payload.get("market_value"))
        avg_entry_price = None
        current_price = None
        if qty not in (None, 0) and cost_basis is not None:
            avg_entry_price = float(cost_basis) / float(qty)
        if qty not in (None, 0) and market_value is not None:
            current_price = float(market_value) / float(qty)
        return {
            "provider": "tradier",
            "source": "tradier.positions",
            "symbol": _normalize_symbol(payload.get("symbol")) or "",
            "asset_id": payload.get("cusip"),
            "exchange": payload.get("exchange"),
            "asset_class": payload.get("asset_type") or "equity",
            "side": "long" if (qty or 0) >= 0 else "short",
            "qty": qty,
            "avg_entry_price": avg_entry_price,
            "market_value": market_value,
            "cost_basis": cost_basis,
            "current_price": current_price,
            "lastday_price": _safe_float(payload.get("close_price")),
            "unrealized_pl": _safe_float(payload.get("gain_loss")),
            "unrealized_plpc": _safe_float(payload.get("gain_loss_percent")),
            "change_today": _safe_float(payload.get("change_percentage")),
            "raw": payload,
        }

    @staticmethod
    def _normalize_order(payload: dict[str, Any], *, account_id: str | None = None) -> dict[str, Any]:
        status = _safe_text(payload.get("status")) or "unknown"
        return {
            "provider": "tradier",
            "source": "tradier.orders",
            "account_id": account_id,
            "order_id": _safe_text(payload.get("id"), payload.get("order_id")) or "",
            "client_order_id": _safe_text(payload.get("tag"), payload.get("client_id")),
            "symbol": _normalize_symbol(payload.get("symbol")),
            "asset_id": payload.get("cusip"),
            "asset_class": payload.get("class") or "equity",
            "side": _safe_text(payload.get("side"), payload.get("transaction")),
            "order_type": _safe_text(payload.get("type")),
            "time_in_force": _safe_text(payload.get("duration")),
            "status": status.lower(),
            "qty": _safe_float(payload.get("quantity")),
            "filled_qty": _safe_float(payload.get("exec_quantity") or payload.get("filled_qty")),
            "filled_avg_price": _safe_float(payload.get("avg_fill_price") or payload.get("average_fill_price")),
            "limit_price": _safe_float(payload.get("price")),
            "stop_price": _safe_float(payload.get("stop_price") or payload.get("stop")),
            "notional": _safe_float(payload.get("amount")),
            "submitted_at": _safe_text(payload.get("create_date"), payload.get("transaction_date")),
            "created_at": _safe_text(payload.get("create_date"), payload.get("transaction_date")),
            "updated_at": _safe_text(payload.get("transaction_date"), payload.get("last_update")),
            "filled_at": _safe_text(payload.get("exec_date"), payload.get("transaction_date")) if status.lower() == "filled" else None,
            "extended_hours": False,
            "raw": payload,
        }

    async def gateway_status(self) -> dict[str, Any]:
        result = {
            "provider": self.provider_name,
            "configured": self.is_configured(),
            "enabled": bool(self.config.enabled),
            "paper": bool(self.config.paper),
            "base_url": self.config.base_url,
            "read_only": bool(self.config.read_only or not self.config.allow_write),
            "allow_write": bool(self.config.allow_write),
            "capabilities": self.capabilities(),
            "connected": False,
            "account": None,
            "clock": None,
            "error": None,
        }
        if not self.is_configured():
            result["error"] = "broker gateway not configured"
            return result
        try:
            profile = await self._get_profile()
            account_id = await self._resolve_account_id()
            balances = await self._request("GET", f"/accounts/{account_id}/balances")
            result["account"] = self._normalize_account(profile, balances if isinstance(balances, dict) else {}, account_id)
            result["connected"] = True
            return result
        except Exception as exc:
            result["error"] = str(exc)
            return result

    async def get_account(self) -> dict[str, Any]:
        profile = await self._get_profile()
        account_id = await self._resolve_account_id()
        balances = await self._request("GET", f"/accounts/{account_id}/balances")
        return self._normalize_account(profile, balances if isinstance(balances, dict) else {}, account_id)

    async def list_positions(self) -> list[dict[str, Any]]:
        account_id = await self._resolve_account_id()
        payload = await self._request("GET", f"/accounts/{account_id}/positions")
        rows = _as_list(_dig(payload, "positions", "position"))
        return [self._normalize_position(item) for item in rows]

    async def list_orders(
        self,
        *,
        status: str = "open",
        limit: int = 50,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        account_id = await self._resolve_account_id()
        normalized_status = (_safe_text(status) or "open").lower()
        tradier_status = "open" if normalized_status == "open" else "closed" if normalized_status == "closed" else "all"
        payload = await self._request(
            "GET",
            f"/accounts/{account_id}/orders",
            params={"includeTags": "true", "status": tradier_status},
        )
        rows = _as_list(_dig(payload, "orders", "order"))
        normalized_rows = [self._normalize_order(item, account_id=account_id) for item in rows]
        if normalized_status == "all":
            filtered = normalized_rows
        elif normalized_status == "closed":
            filtered = [item for item in normalized_rows if _safe_text(item.get("status")) in {"filled", "cancelled", "expired", "rejected"}]
        else:
            filtered = [item for item in normalized_rows if _safe_text(item.get("status")) not in {"filled", "cancelled", "expired", "rejected"}]
        if symbols:
            target_symbols = {_normalize_symbol(item) for item in symbols if _normalize_symbol(item)}
            filtered = [item for item in filtered if _normalize_symbol(item.get("symbol")) in target_symbols]
        filtered.sort(key=lambda item: _safe_text(item.get("updated_at"), item.get("created_at")) or "", reverse=True)
        return filtered[: max(1, int(limit))]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        account_id = await self._resolve_account_id()
        payload = await self._request("GET", f"/accounts/{account_id}/orders/{order_id}")
        order_payload = dict(payload.get("order") or payload) if isinstance(payload, dict) else {}
        return self._normalize_order(order_payload, account_id=account_id)

    async def list_order_events(self, *, order_id: str, limit: int = 50) -> list[dict[str, Any]]:
        order = await self.get_order(order_id)
        fills = await self.list_fills(order_id=order_id, limit=limit)
        receipt = await self.get_order_receipt(order_id)
        return build_live_order_events(order, fills=fills, receipt=receipt)[: max(1, int(limit))]

    async def list_fills(
        self,
        *,
        order_id: str | None = None,
        limit: int = 50,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        account_id = await self._resolve_account_id()
        if order_id:
            orders = [await self.get_order(order_id)]
        else:
            orders = await self.list_orders(status="all", limit=max(limit, 100), symbols=symbols)
        fills = []
        for order in orders:
            filled_qty = _safe_float(order.get("filled_qty"))
            if filled_qty is None or filled_qty <= 0:
                continue
            fill = _normalize_broker_fill(
                self.provider_name,
                {
                    "order_id": order.get("order_id"),
                    "symbol": order.get("symbol"),
                    "transaction": order.get("side"),
                    "type": order.get("order_type"),
                    "price": order.get("filled_avg_price") or order.get("limit_price"),
                    "qty": filled_qty,
                    "filled_at": order.get("filled_at") or order.get("updated_at"),
                },
                account_id=account_id,
                order_id=_safe_text(order.get("order_id")),
                symbol=_safe_text(order.get("symbol")),
                source="tradier.orders",
            )
            fills.append(fill)
        fills.sort(key=lambda item: _safe_text(item.get("occurred_at")) or "", reverse=True)
        return fills[: max(1, int(limit))]

    async def get_order_receipt(self, order_id: str) -> dict[str, Any]:
        account_id = await self._resolve_account_id()
        order = await self.get_order(order_id)
        return _build_broker_receipt(self.provider_name, order, account_id=account_id, source="tradier.order_receipt")

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = await self._resolve_account_id()
        symbol = _safe_text(payload.get("symbol"), payload.get("code"))
        qty = payload.get("qty")
        if qty in (None, ""):
            qty = payload.get("quantity")
        if not symbol:
            raise LiveBrokerError("submit_order requires symbol/code")
        if qty in (None, ""):
            raise LiveBrokerError("tradier submit_order requires qty/quantity")
        order_type = (_safe_text(payload.get("type"), payload.get("order_type"), "market") or "market").lower()
        duration = (_safe_text(payload.get("time_in_force"), "day") or "day").lower()
        form_body = {
            "class": "equity",
            "symbol": symbol.upper(),
            "side": (_safe_text(payload.get("side"), payload.get("direction"), "buy") or "buy").lower(),
            "quantity": str(qty),
            "type": order_type,
            "duration": duration,
        }
        if payload.get("limit_price") not in (None, ""):
            form_body["price"] = str(payload.get("limit_price"))
        if payload.get("stop_price") not in (None, ""):
            form_body["stop"] = str(payload.get("stop_price"))
        if payload.get("client_order_id"):
            form_body["tag"] = _safe_text(payload.get("client_order_id")) or ""
        response = await self._request(
            "POST",
            f"/accounts/{account_id}/orders",
            form_body=form_body,
            allow_write=True,
        )
        order_ref = _safe_text(_dig(response, "order", "id"), response.get("id") if isinstance(response, dict) else None)
        if order_ref:
            try:
                return await self.get_order(order_ref)
            except Exception:
                pass
        response_payload = dict(response.get("order") or response) if isinstance(response, dict) else {}
        response_payload.setdefault("symbol", symbol.upper())
        response_payload.setdefault("quantity", qty)
        response_payload.setdefault("type", order_type)
        response_payload.setdefault("duration", duration)
        response_payload.setdefault("side", form_body["side"])
        return self._normalize_order(response_payload, account_id=account_id)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        account_id = await self._resolve_account_id()
        payload = await self._request("DELETE", f"/accounts/{account_id}/orders/{order_id}", allow_write=True)
        return {
            "order_id": _safe_text(order_id) or "",
            "cancelled": True,
            "response": payload if isinstance(payload, dict) else {},
        }

def get_live_broker_adapter(config: Optional[LiveBrokerConfig] = None) -> BaseBrokerAdapter:
    resolved = config or LiveBrokerConfig.from_env()
    broker = (_safe_text(resolved.broker) or "alpaca").lower()
    if broker == "alpaca":
        return AlpacaBrokerAdapter(resolved)
    if broker == "tradier":
        return TradierBrokerAdapter(resolved)
    raise LiveBrokerError(f"unsupported live broker provider: {broker}")

def summarize_broker_order_preview(payload: dict[str, Any]) -> dict[str, Any]:
    preview = {
        "symbol": _safe_text(payload.get("symbol"), payload.get("code")) or "",
        "side": (_safe_text(payload.get("side"), payload.get("direction"), "buy") or "buy").lower(),
        "order_type": (_safe_text(payload.get("type"), payload.get("order_type"), "market") or "market").lower(),
        "time_in_force": (_safe_text(payload.get("time_in_force"), "day") or "day").lower(),
        "qty": _safe_float(payload.get("qty") if payload.get("qty") is not None else payload.get("quantity")),
        "notional": _safe_float(payload.get("notional")),
        "limit_price": _safe_float(payload.get("limit_price")),
        "stop_price": _safe_float(payload.get("stop_price")),
        "client_order_id": _safe_text(payload.get("client_order_id")),
        "extended_hours": bool(payload.get("extended_hours")) if payload.get("extended_hours") is not None else None,
    }
    return {key: value for key, value in preview.items() if value not in (None, "", [])}
