"""`execute_sql` 工具的端到端行为测试（走 handlers.handle_execute_sql，MockConnectionPool 后端）。"""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import generate_latest

from starrocks_mcp.auth.models import ApiKeyPermission, ScopeRule
from starrocks_mcp.handlers import handle_execute_sql

SCOPED_READ_PERM = ApiKeyPermission(username="alice", key_id="ro", role="read", scope=[ScopeRule(catalog="paimon", database="csc")])
UNRESTRICTED_READWRITE_PERM = ApiKeyPermission(username="bob", key_id="rw", role="readwrite", scope=None)


async def test_readonly_select_within_scope_succeeds(app_state):
    sql = (
        "select * from paimon.csc.ods_csc_log_audit "
        "where app_id='hids-identify-result-storage-svc' and year=2026 limit 2"
    )
    result = await handle_execute_sql(app_state, SCOPED_READ_PERM, sql)
    assert result["row_count"] == 2
    assert result["pool_used"] == "read"
    assert result["statement_type"] == "SELECT"
    assert result["limit_applied"] == 2


async def test_readonly_insert_is_rejected(app_state):
    with pytest.raises(ValueError, match="安全审计"):
        await handle_execute_sql(app_state, SCOPED_READ_PERM, "INSERT INTO paimon.csc.ods_csc_log_audit VALUES (1)")


async def test_readwrite_update_uses_write_pool(app_state):
    result = await handle_execute_sql(
        app_state, UNRESTRICTED_READWRITE_PERM, "UPDATE dw.orders SET status='paid' WHERE order_id=1001"
    )
    assert result["pool_used"] == "write"
    assert result["affected_rows"] == 1
    assert result["statement_type"] == "UPDATE"


async def test_out_of_scope_query_is_rejected(app_state):
    with pytest.raises(ValueError, match="安全审计"):
        await handle_execute_sql(app_state, SCOPED_READ_PERM, "select * from default_catalog.dw.orders")


async def test_ddl_is_always_rejected(app_state):
    with pytest.raises(ValueError, match="安全审计"):
        await handle_execute_sql(app_state, UNRESTRICTED_READWRITE_PERM, "DROP TABLE dw.orders")


async def test_max_rows_trims_client_side(app_state):
    result = await handle_execute_sql(
        app_state,
        UNRESTRICTED_READWRITE_PERM,
        "select * from paimon.csc.ods_csc_log_audit limit 2",
        max_rows=1,
    )
    assert result["row_count"] == 1
    assert len(result["rows"]) == 1


async def test_successful_execution_records_metrics_and_audit_log(app_state):
    await handle_execute_sql(
        app_state, UNRESTRICTED_READWRITE_PERM, "select * from paimon.csc.ods_csc_log_audit limit 1"
    )
    # 审计日志写入被丢进了默认线程池执行，给它一点时间落盘
    await asyncio.sleep(0.2)

    entries = app_state.request_logger.get_recent(username="bob", limit=5)
    assert len(entries) >= 1
    assert entries[0].status == "success"
    assert entries[0].username == "bob"
    assert entries[0].statement_type == "SELECT"

    metrics_text = generate_latest(app_state.metrics.registry).decode("utf-8")
    assert 'api_key_id="rw"' in metrics_text
    assert 'status="success"' in metrics_text


async def test_rejected_execution_records_audit_log_with_statement_type(app_state):
    with pytest.raises(ValueError):
        await handle_execute_sql(app_state, UNRESTRICTED_READWRITE_PERM, "DROP TABLE dw.orders")
    await asyncio.sleep(0.2)

    entries = app_state.request_logger.get_recent(username="bob", limit=5)
    assert entries[0].status == "rejected"
    assert entries[0].username == "bob"
    # 即使被拒绝，也应该尽量记录识别出的语句类型，而不是笼统的 UNKNOWN
    assert entries[0].statement_type == "DROP"
    assert entries[0].error is not None
