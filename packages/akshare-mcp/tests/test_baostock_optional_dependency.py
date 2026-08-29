from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from akshare_mcp.tools import finance as finance_module


def test_get_financials_reports_missing_optional_baostock_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(finance_module.cache, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(finance_module.cache, "set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(finance_module, "resolve_existing_security_code_sync", lambda code=None, **_kwargs: (code, {}, None))
    monkeypatch.setattr(finance_module, "_get_financials_tdx", lambda _code: None)
    monkeypatch.setattr(finance_module, "_get_financials_tushare", lambda _code: None)
    monkeypatch.setattr(finance_module, "_get_financials_akshare", lambda _code: None)
    monkeypatch.setattr(
        finance_module,
        "baostock_client",
        SimpleNamespace(available=False, unavailable_reason="No module named 'baostock'"),
    )

    from akshare_mcp import storage as storage_module

    monkeypatch.setattr(storage_module, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("skip db")))

    result = asyncio.run(finance_module.get_financials.__wrapped__(code="600519"))

    assert result["success"] is False
    raw = json.dumps(result, ensure_ascii=False)
    assert "baostock_optional_dependency_missing" in raw
    assert "No module named 'baostock'" in raw
