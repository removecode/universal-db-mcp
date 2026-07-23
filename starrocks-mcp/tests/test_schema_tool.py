"""`get_database_info` 分层查询逻辑测试（基于 MockConnectionPool）。"""

from __future__ import annotations

import pytest

from starrocks_mcp.auth.models import ApiKeyPermission, ScopeRule
from starrocks_mcp.db.executor import PooledExecutor
from starrocks_mcp.db.mock_pool import MockConnectionPool
from starrocks_mcp.schema.catalog import SchemaAccessError, get_database_info

SCOPED_PERM = ApiKeyPermission(username="alice", key_id="ro", role="read", scope=[ScopeRule(catalog="paimon", database="csc")])
UNRESTRICTED_PERM = ApiKeyPermission(username="bob", key_id="rw", role="readwrite", scope=None)


@pytest.fixture
def pool():
    return MockConnectionPool()


@pytest.fixture
def executor():
    ex = PooledExecutor(max_workers=2)
    yield ex
    ex.shutdown()


async def test_no_params_lists_scoped_catalogs(pool, executor):
    result = await get_database_info(
        pool=pool, executor=executor, permission=SCOPED_PERM,
        catalog=None, database=None, table=None, timeout_seconds=5,
    )
    assert result["level"] == "catalogs"
    assert result["catalogs"] == ["paimon"]


async def test_no_params_unrestricted_lists_all_catalogs(pool, executor):
    result = await get_database_info(
        pool=pool, executor=executor, permission=UNRESTRICTED_PERM,
        catalog=None, database=None, table=None, timeout_seconds=5,
    )
    assert result["level"] == "catalogs"
    assert set(result["catalogs"]) == {"default_catalog", "paimon"}


async def test_catalog_only_lists_databases(pool, executor):
    result = await get_database_info(
        pool=pool, executor=executor, permission=UNRESTRICTED_PERM,
        catalog="paimon", database=None, table=None, timeout_seconds=5,
    )
    assert result["level"] == "databases"
    assert result["databases"] == ["csc"]


async def test_catalog_and_database_lists_tables(pool, executor):
    result = await get_database_info(
        pool=pool, executor=executor, permission=UNRESTRICTED_PERM,
        catalog="paimon", database="csc", table=None, timeout_seconds=5,
    )
    assert result["level"] == "tables"
    assert "ods_csc_log_audit" in result["tables"]


async def test_full_qualified_describes_external_table_without_ddl(pool, executor):
    result = await get_database_info(
        pool=pool, executor=executor, permission=UNRESTRICTED_PERM,
        catalog="paimon", database="csc", table="ods_csc_log_audit", timeout_seconds=5,
    )
    assert result["level"] == "columns"
    field_names = [c["Field"] for c in result["columns"]]
    assert "app_id" in field_names
    assert "create_table" not in result
    assert "note" in result  # 提示外部 catalog 没有原生分区/分桶概念


async def test_database_and_table_without_catalog_defaults_to_internal(pool, executor):
    result = await get_database_info(
        pool=pool, executor=executor, permission=UNRESTRICTED_PERM,
        catalog=None, database="dw", table="orders", timeout_seconds=5,
    )
    assert result["catalog"] == "default_catalog"
    assert "create_table" in result
    assert "DISTRIBUTED BY" in result["create_table"]


async def test_scope_violation_raises_schema_access_error(pool, executor):
    with pytest.raises(SchemaAccessError):
        await get_database_info(
            pool=pool, executor=executor, permission=SCOPED_PERM,
            catalog="default_catalog", database="dw", table=None, timeout_seconds=5,
        )


async def test_table_without_database_raises_value_error(pool, executor):
    with pytest.raises(ValueError):
        await get_database_info(
            pool=pool, executor=executor, permission=UNRESTRICTED_PERM,
            catalog=None, database=None, table="orders", timeout_seconds=5,
        )
