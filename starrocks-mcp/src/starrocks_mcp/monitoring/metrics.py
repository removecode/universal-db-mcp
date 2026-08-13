"""Prometheus 监控指标。"""

from __future__ import annotations

from typing import Any

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
            "当前被借出的连接数（直接读连接池状态）",
            ["pool"],
            registry=self.registry,
        )
        self.pool_waiting = Gauge(
            "mcp_pool_waiting",
            "正在排队等待空闲连接的调用数",
            ["pool"],
            registry=self.registry,
        )
        self.pool_max_connections = Gauge(
            "mcp_pool_max_connections",
            "连接池并发上限",
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

    def bind_pool(self, name: str, pool: Any) -> None:
        """让连接池指标直接反映池子的真实状态。

        之前是在调用前后各 inc(±1)，而减 1 放在 `finally` 里：查询超时时计数会跟着
        减掉，可那条连接其实还没归还。于是池子已经满了，监控上却显示接近 0——
        恰好在故障时给出相反的结论。
        """
        self.pool_in_use.labels(pool=name).set_function(lambda: pool.stats().in_use)
        self.pool_waiting.labels(pool=name).set_function(lambda: pool.stats().waiting)
        self.pool_max_connections.labels(pool=name).set_function(lambda: pool.stats().max_connections)

    async def endpoint(self, request: Request) -> Response:  # noqa: ARG002 - Starlette 路由签名要求
        data = generate_latest(self.registry)
        return Response(data, media_type=CONTENT_TYPE_LATEST)
