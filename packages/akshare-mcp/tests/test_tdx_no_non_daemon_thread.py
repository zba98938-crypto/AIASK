"""The TDX client must not leave a thread the interpreter waits for.

`TdxHq_API(heartbeat=True)` starts a **non-daemon** thread on every successful
connect. CPython's shutdown calls ``threading._shutdown()`` — which waits for
every non-daemon thread — *before* it runs atexit callbacks, so nothing
registered at exit can stop it. Any process that ever opened an online TDX
connection therefore hung forever *after* finishing its work: a probe returned
``get_realtime_quote`` in 0.1 s and then never terminated, with the stack parked
at ``pytdx/heartbeat.py:24`` under ``threading.py:1543 _shutdown``.

That failure is invisible to a unit test — it happens at interpreter shutdown —
so this asserts the mechanism that prevents it: no heartbeat thread is requested.
"""

from __future__ import annotations

import sys
import types

import pytest

from akshare_mcp.data_source import tdx_local


class _FakeApi:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def connect(self, *_args, **_kwargs):
        return self


@pytest.fixture()
def fake_pytdx(monkeypatch):
    created: list[_FakeApi] = []

    def _factory(**kwargs):
        api = _FakeApi(**kwargs)
        created.append(api)
        return api

    module = types.ModuleType("pytdx.hq")
    module.TdxHq_API = _factory
    package = types.ModuleType("pytdx")
    package.hq = module
    monkeypatch.setitem(sys.modules, "pytdx", package)
    monkeypatch.setitem(sys.modules, "pytdx.hq", module)
    return created


def test_the_hq_client_is_built_without_a_heartbeat_thread(monkeypatch, fake_pytdx) -> None:
    source = tdx_local.TdxLocalSource()
    # ``TdxLocalSource`` is a singleton whose env-derived flags are read once at
    # first construction. Other tests in the suite set ``TDX_LOCAL_ONLY=1`` or
    # trigger a real connect failure (which starts the 30 s cooldown), so
    # without this reset the client is never constructed and the assertion
    # below would pass or fail depending on test order rather than behaviour.
    monkeypatch.setattr(source, "_local_only", False, raising=False)
    monkeypatch.setattr(source, "_hq_api", None, raising=False)
    monkeypatch.setattr(source, "_hq_server", None, raising=False)
    monkeypatch.setattr(source, "_last_hq_failure_at", 0.0, raising=False)
    monkeypatch.setattr(source, "_servers", [("127.0.0.1", 7709)], raising=False)

    api = source._ensure_hq()

    assert api is not None, "the fake connect always succeeds"
    assert fake_pytdx, "the client was constructed"
    assert fake_pytdx[0].kwargs.get("heartbeat") is False, (
        "heartbeat=True starts a non-daemon thread that CPython's shutdown waits "
        "for before atexit runs, so the process can never exit"
    )


def test_an_explicit_reset_disconnects_the_client() -> None:
    """The socket is still closed on demand; only the keep-alive thread is gone."""

    source = tdx_local.TdxLocalSource()
    disconnected: list[bool] = []

    class _Api:
        def disconnect(self):
            disconnected.append(True)

    source._hq_api = _Api()
    source._hq_server = ("127.0.0.1", 7709)

    source.reset_hq()

    assert disconnected == [True]
    assert source._hq_api is None and source._hq_server is None
