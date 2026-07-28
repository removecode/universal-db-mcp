"""端到端并发限制测试：验证连接池 maxconnections + executor_max_workers 生效。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from starrocks_mcp.config import Settings
from starrocks_mcp.db.mock_pool import MockConnectionPool
from starrocks_mcp.server import build_asgi_app

from .conftest import READONLY_KEY
from .test_app_integration import _httpx_client_factory

_CONCURRENT_QUERY_SQL = (
    "select * from paimon.csc.ods_csc_log_audit "
    "where app_id='hids-identify-result-storage-svc' limit 1"
)


@pytest.fixture
def concurrency_settings(tmp_path, apikeys_file):
    """将并发上限收紧为 2，并为 mock SQL 注入可观测延迟。"""
    s = Settings()
    s.base_dir = tmp_path
    s.auth.apikeys_file = str(apikeys_file)
    s.monitoring.audit_db_path = str(tmp_path / "audit.db")
    s.database.driver = "mock"
    s.database.executor_max_workers = 2
    s.database.read_pool.maxconnections = 2
    s.database.write_pool.maxconnections = 2
    s.database.mock_execute_delay_seconds = 0.15
    return s


async def _execute_sql_once(app, sql: str) -> None:
    async with streamablehttp_client(
        "http://testserver/mcp",
        headers={"X-API-Key": READONLY_KEY},
        httpx_client_factory=_httpx_client_factory(app),
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("execute_sql", {"sql": sql})
            assert not result.isError, result.content[0].text if result.content else result


async def test_execute_sql_concurrency_limited_to_two_e2e(concurrency_settings):
    """4 路并发 execute_sql 时，读池峰值并发不超过 2，且总耗时体现排队。"""
    app, state = build_asgi_app(concurrency_settings)
    state.read_pool.reset_peak_stats()

    try:
        async with app.router.lifespan_context(app):
            started = time.monotonic()
            await asyncio.gather(*[_execute_sql_once(app, _CONCURRENT_QUERY_SQL) for _ in range(4)])
            elapsed = time.monotonic() - started
    finally:
        state.close()

    assert state.read_pool.peak_active <= 2, (
        f"读池峰值并发 {state.read_pool.peak_active} 超过上限 2"
    )
    # 4 条查询、并发 2、每条 0.15s → 至少两批，总耗时应明显大于单条延迟
    assert elapsed >= 0.28, f"4 路并发在限流为 2 时耗时过短 ({elapsed:.3f}s)，限流可能未生效"


async def test_execute_sql_without_concurrency_limit_completes_faster(concurrency_settings):
    """对照组：放宽并发后，同样 4 路请求应更快完成。"""
    concurrency_settings.database.executor_max_workers = 8
    concurrency_settings.database.read_pool.maxconnections = 8
    concurrency_settings.database.mock_execute_delay_seconds = 0.12

    app, state = build_asgi_app(concurrency_settings)
    state.read_pool.reset_peak_stats()

    try:
        async with app.router.lifespan_context(app):
            started = time.monotonic()
            await asyncio.gather(*[_execute_sql_once(app, _CONCURRENT_QUERY_SQL) for _ in range(4)])
            elapsed = time.monotonic() - started
    finally:
        state.close()

    assert state.read_pool.peak_active >= 3, (
        f"放宽限制后峰值并发仅 {state.read_pool.peak_active}，未观察到并行执行"
    )
    assert elapsed < 0.35, f"放宽限制后仍耗时过长 ({elapsed:.3f}s)"


def test_mock_pool_semaphore_limits_peak_active():
    """单测 mock 池信号量：max_connections=2 时峰值并发不超过 2。"""
    pool = MockConnectionPool(
        role="read",
        max_connections=2,
        execute_delay_seconds=0.1,
    )
    pool.reset_peak_stats()

    def run_query() -> None:
        pool.execute("SELECT * FROM paimon.csc.ods_csc_log_audit LIMIT 1")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(run_query) for _ in range(6)]
        for future in futures:
            future.result()

    assert pool.peak_active <= 2
