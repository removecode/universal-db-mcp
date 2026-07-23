"""Prometheus 指标与 SQLite 审计日志模块的独立单测。"""

from __future__ import annotations

from prometheus_client import generate_latest

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


def test_metrics_pool_usage_gauge_tracks_delta():
    metrics = Metrics()
    metrics.track_pool_usage("read", 1)
    metrics.track_pool_usage("read", 1)
    metrics.track_pool_usage("read", -1)
    text = generate_latest(metrics.registry).decode("utf-8")
    assert 'mcp_pool_in_use{pool="read"} 1.0' in text


def test_request_logger_persists_and_queries_by_api_key(tmp_path):
    logger = RequestLogger(tmp_path / "audit.db")
    logger.log(
        api_key_id="bi-team-readonly", statement_type="SELECT", sql_text="SELECT 1",
        pool="read", duration_ms=1.23, row_count=1, status="success",
    )
    logger.log(
        api_key_id="etl-job", statement_type="UPDATE", sql_text="UPDATE t SET a=1",
        pool="write", duration_ms=5.0, row_count=0, status="success",
    )

    ro_entries = logger.get_recent(api_key_id="bi-team-readonly")
    assert len(ro_entries) == 1
    assert ro_entries[0].statement_type == "SELECT"

    all_entries = logger.get_recent()
    assert len(all_entries) == 2
    # 按 id 倒序返回（最近的在前）
    assert all_entries[0].api_key_id == "etl-job"


def test_request_logger_truncates_long_sql_text(tmp_path):
    logger = RequestLogger(tmp_path / "audit.db")
    long_sql = "SELECT " + "a" * 5000
    logger.log(
        api_key_id="k", statement_type="SELECT", sql_text=long_sql,
        pool="read", duration_ms=1.0, row_count=0, status="success",
        sql_text_max_length=100,
    )
    entry = logger.get_recent(api_key_id="k")[0]
    assert len(entry.sql_text) == 100
