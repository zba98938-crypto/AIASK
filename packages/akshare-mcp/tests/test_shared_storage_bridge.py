from __future__ import annotations


def test_akshare_storage_reexports_shared_sqlite_adapter():
    import akshare_mcp.storage as legacy_storage
    import akshare_mcp.storage.sqlite.schema_base as legacy_schema_base
    import aiask_quant_core.storage as shared_storage
    import aiask_quant_core.storage.sqlite.schema_base as shared_schema_base

    assert legacy_storage.SQLiteAdapter is shared_storage.SQLiteAdapter
    assert legacy_storage.get_db is shared_storage.get_db
    assert legacy_schema_base is shared_schema_base


def test_strategy_factory_runtime_db_provider_is_shared_storage():
    from aiask_quant_core.storage import get_db
    from akshare_mcp.adapters.strategy_factory_runtime import get_strategy_factory_db_provider

    assert get_strategy_factory_db_provider() is get_db
