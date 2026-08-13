"""Prometheus 指标与 SQLite 审计日志模块的独立单测。"""

from __future__ import annotations

from types import SimpleNamespace

from prometheus_client import generate_latest

from starrocks_mcp.db.base import ConnectionSlots
from starrocks_mcp.monitoring.metrics import Metrics
from starrocks_mcp.monitoring.request_log import RequestLogger


def test_metrics_record_execution_increments_counters():
    metrics = Metrics()
    metrics.record_execution(
        api_key_id="bi-team-readonly", statement_type="SELECT", status="success",
        duration_seconds=0.05, row_count=10,
    )
    text = generate_latest(metrics.registry).decode("utf-8")
    assert 'mcp_sql_requests_total{api_key_id="bi-team-readonly",statement_type="SELECT",status="success"} 1.0' in text
    assert "mcp_sql_duration_seconds_bucket" in text
    assert "mcp_sql_rows_returned_bucket" in text


def test_pool_gauges_read_live_pool_state():
    """指标必须直接读连接池状态。

    以前是调用前后各 inc(±1)，而减 1 在 finally 里：查询超时时计数跟着减掉，
    连接却还没归还——池子满了监控反而显示 0，正好在故障时给出相反的结论。
    """
    slots = ConnectionSlots(2, role="read")
    metrics = Metrics()
    metrics.bind_pool("read", SimpleNamespace(stats=slots.stats))

    with slots.hold(0.1):
        text = generate_latest(metrics.registry).decode("utf-8")
        assert 'mcp_pool_in_use{pool="read"} 1.0' in text
        assert 'mcp_pool_max_connections{pool="read"} 2.0' in text

    text = generate_latest(metrics.registry).decode("utf-8")
    assert 'mcp_pool_in_use{pool="read"} 0.0' in text


def test_request_logger_persists_and_queries_by_username(tmp_path):
    logger = RequestLogger(tmp_path / "audit.db")
    logger.log(
        username="alice", api_key_id="bi-team-readonly", statement_type="SELECT",
        sql_text="SELECT 1", pool="read", duration_ms=1.23, row_count=1, status="success",
    )
    logger.log(
        username="bob", api_key_id="etl-job", statement_type="UPDATE",
        sql_text="UPDATE t SET a=1", pool="write", duration_ms=5.0, row_count=0, status="success",
    )

    alice_entries = logger.get_recent(username="alice")
    assert len(alice_entries) == 1
    assert alice_entries[0].username == "alice"
    assert alice_entries[0].statement_type == "SELECT"

    all_entries = logger.get_recent()
    assert len(all_entries) == 2
    assert all_entries[0].username == "bob"


def test_request_logger_truncates_long_sql_text(tmp_path):
    logger = RequestLogger(tmp_path / "audit.db")
    long_sql = "SELECT " + "a" * 5000
    logger.log(
        username="alice", api_key_id="k", statement_type="SELECT", sql_text=long_sql,
        pool="read", duration_ms=1.0, row_count=0, status="success",
        sql_text_max_length=100,
    )
    entry = logger.get_recent(username="alice")[0]
    assert len(entry.sql_text) == 100
