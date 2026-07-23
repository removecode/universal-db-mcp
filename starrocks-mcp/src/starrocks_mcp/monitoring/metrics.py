"""Prometheus 监控指标。"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response


class Metrics:
    """把常用的执行指标包装成一个对象，方便在 server.py 里统一注入依赖。"""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.requests_total = Counter(
            "mcp_sql_requests_total",
            "execute_sql 工具调用次数",
            ["api_key_id", "statement_type", "status"],
            registry=self.registry,
        )
        self.duration_seconds = Histogram(
            "mcp_sql_duration_seconds",
            "SQL 执行耗时（秒）",
            ["api_key_id", "statement_type"],
            registry=self.registry,
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.rows_returned = Histogram(
            "mcp_sql_rows_returned",
            "单次查询返回的行数",
            ["api_key_id"],
            registry=self.registry,
            buckets=(0, 1, 5, 10, 50, 100, 500, 1000, 5000, 10000),
        )
        self.pool_in_use = Gauge(
            "mcp_pool_in_use",
            "当前连接池活跃使用计数（近似值，基于并发调用计数）",
            ["pool"],
            registry=self.registry,
        )

    def record_execution(
        self,
        *,
        api_key_id: str,
        statement_type: str,
        status: str,
        duration_seconds: float,
        row_count: int,
    ) -> None:
        self.requests_total.labels(api_key_id=api_key_id, statement_type=statement_type, status=status).inc()
        self.duration_seconds.labels(api_key_id=api_key_id, statement_type=statement_type).observe(duration_seconds)
        self.rows_returned.labels(api_key_id=api_key_id).observe(row_count)

    def track_pool_usage(self, pool: str, delta: int) -> None:
        self.pool_in_use.labels(pool=pool).inc(delta)

    async def endpoint(self, request: Request) -> Response:  # noqa: ARG002 - Starlette 路由签名要求
        data = generate_latest(self.registry)
        return Response(data, media_type=CONTENT_TYPE_LATEST)
