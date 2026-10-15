"""The neutral data-provider facade (Phase 5).

``akshare-mcp`` names one upstream source in a package that owns the MCP host,
the SQLite provider, the manager facade and the broker connectors. This facade
gives that role a vendor-neutral name without forking the implementation — one
implementation, two names, so adopting the neutral name carries no behavioural
risk.

Two boundaries these tests defend:

* the facade exposes **data-provider** concerns only; factory orchestration must
  stay behind ``strategy_factory.api``;
* it is not an unbounded passthrough — only the declared surface resolves.
"""

from __future__ import annotations

import pytest

import aiask_data_mcp as facade


# --- identity ------------------------------------------------------------------


def test_the_facade_declares_what_it_delegates_to() -> None:
    """Provenance must be inspectable; a silent alias is worse than no alias."""
    assert facade.BACKING_DISTRIBUTION == "akshare-mcp"
    assert facade.BACKING_MODULE == "akshare_mcp"
    assert facade.FACADE_PROTOCOL_VERSION == "1.0"


def test_status_reports_without_importing_the_provider() -> None:
    status = facade.facade_status()

    assert status["facade"] == "aiask-data-mcp"
    assert status["backing_module"] == "akshare_mcp"
    assert isinstance(status["backing_available"], bool)
    assert status["ready"] == status["backing_available"]


# --- the neutral surface -------------------------------------------------------


def test_the_surface_covers_data_provider_concerns() -> None:
    assert set(facade.NEUTRAL_SUBMODULES) == {
        "storage",
        "env_loader",
        "server",
    }


def test_the_surface_excludes_factory_orchestration() -> None:
    """A data provider must not become a route to the factory contract."""
    for forbidden in ("services", "adapters", "tools", "runtime"):
        assert forbidden not in facade.NEUTRAL_SUBMODULES


@pytest.mark.parametrize("name", ("services", "adapters", "tools", "core", "config"))
def test_undeclared_names_are_refused(name: str) -> None:
    """Not an unbounded passthrough to every provider internal."""
    with pytest.raises(AttributeError, match="not part of the neutral"):
        facade.resolve(name)


def test_the_refusal_message_lists_what_is_available() -> None:
    with pytest.raises(AttributeError) as excinfo:
        facade.resolve("nope")

    message = str(excinfo.value)
    for name in facade.NEUTRAL_SUBMODULES:
        assert name in message


# --- resolution ----------------------------------------------------------------


def test_storage_resolves_to_the_backing_implementation() -> None:
    pytest.importorskip("akshare_mcp.storage", reason="backing provider not installed")

    module = facade.resolve("storage")

    assert module.__name__ == "akshare_mcp.storage"
    assert callable(getattr(module, "get_db", None))


def test_attribute_access_matches_explicit_resolution() -> None:
    pytest.importorskip("akshare_mcp.storage", reason="backing provider not installed")

    assert facade.storage is facade.resolve("storage")


def test_resolution_is_lazy() -> None:
    """Importing the facade must not pull in pandas and friends.

    The provider package is heavy; a facade that imported it eagerly would make
    the neutral name more expensive than the legacy one.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(facade))
    module_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level_imports.add(node.module.split(".")[0])

    assert "akshare_mcp" not in module_level_imports, (
        "the backing provider must be imported lazily, not at module scope"
    )


# --- failure reporting ---------------------------------------------------------


def test_a_missing_provider_raises_a_named_error(monkeypatch) -> None:
    def _fail(_name):
        raise ImportError("simulated absence")

    monkeypatch.setattr(facade.importlib, "import_module", _fail)

    with pytest.raises(facade.BackingImplementationMissing) as excinfo:
        facade.resolve("storage")

    assert "akshare-mcp" in str(excinfo.value), (
        "the error must name the distribution an operator has to install"
    )


def test_missing_provider_is_an_import_error_subclass() -> None:
    assert issubclass(facade.BackingImplementationMissing, ImportError)


# --- entry point ---------------------------------------------------------------


def test_the_server_entry_point_delegates() -> None:
    from aiask_data_mcp import server

    assert callable(server.main)


def test_the_facades_own_submodule_is_not_shadowed_by_the_proxy() -> None:
    """``__getattr__`` must not hijack a name this package implements itself.

    ``from aiask_data_mcp import server`` first resolved to
    ``akshare_mcp.server`` because the proxy ran before submodule lookup — so the
    facade's own entry point was unreachable.
    """
    from aiask_data_mcp import server

    assert server.__name__ == "aiask_data_mcp.server"
    # ``resolve`` still reaches the backing implementation, which is what the
    # entry point itself needs.
    pytest.importorskip("akshare_mcp.server", reason="backing provider not installed")
    assert facade.resolve("server").__name__ == "akshare_mcp.server"


def test_the_entry_point_reports_a_missing_provider_instead_of_crashing(monkeypatch, capsys) -> None:
    from aiask_data_mcp import server

    def _fail(_name):
        raise facade.BackingImplementationMissing("simulated")

    monkeypatch.setattr(server, "resolve", _fail)

    assert server.main([]) == 1
    assert "aiask-data-mcp" in capsys.readouterr().err


def test_the_entry_point_reports_a_provider_without_main(monkeypatch, capsys) -> None:
    from aiask_data_mcp import server

    monkeypatch.setattr(server, "resolve", lambda _name: object())

    assert server.main([]) == 1
    assert "no server.main()" in capsys.readouterr().err


def test_the_entry_point_invokes_the_backing_main(monkeypatch) -> None:
    from aiask_data_mcp import server

    calls: list[object] = []

    class _Backing:
        @staticmethod
        def main():
            calls.append("called")
            return 0

    monkeypatch.setattr(server, "resolve", lambda _name: _Backing)

    assert server.main() == 0
    assert calls == ["called"]
