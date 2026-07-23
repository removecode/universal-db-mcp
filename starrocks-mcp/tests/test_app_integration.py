"""端到端集成测试：完整组装 ASGI 应用（鉴权中间件 + FastMCP streamable-http + 生命周期），
用 httpx.ASGITransport 在进程内驱动，不需要真实网络端口。
"""

from __future__ import annotations

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from starrocks_mcp.server import build_asgi_app

from .conftest import READONLY_KEY, READWRITE_KEY


def _httpx_client_factory(app):
    def factory(headers=None, timeout=None, auth=None):  # noqa: ARG001 - 兼容 mcp 客户端的工厂签名
        kwargs: dict = {"transport": httpx.ASGITransport(app=app), "follow_redirects": True}
        if headers:
            kwargs["headers"] = headers
        return httpx.AsyncClient(**kwargs)

    return factory


@pytest.fixture
def app_and_state(settings):
    # 注意：lifespan 的进入/退出必须发生在同一个 asyncio Task 内（anyio 的
    # task group 有这个硬性要求），所以这里不把 "async with lifespan_context"
    # 放进 async 生成器 fixture 里（pytest-asyncio 的 setup/teardown 有可能
    # 跨 Task），而是让每个测试函数自己在同一个协程里完成 enter/exit。
    app, state = build_asgi_app(settings)
    yield app, state
    state.close()


async def test_unauthenticated_request_rejected(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert resp.status_code == 401


async def test_health_and_metrics_endpoints_not_gated_by_auth(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            body = health.json()
            assert body["status"] == "ok"
            assert body["connected"] is True
            assert body["mode"] == "mock"
            assert "message" in body

            metrics = await client.get("/metrics")
            assert metrics.status_code == 200
            assert b"mcp_sql_requests_total" in metrics.content


async def test_authenticated_full_round_trip_matches_user_example(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with streamablehttp_client(
            "http://testserver/mcp",
            headers={"X-API-Key": READONLY_KEY},
            httpx_client_factory=_httpx_client_factory(app),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                assert tool_names == {
                    "get_connection_status",
                    "get_database_info_tool",
                    "execute_sql",
                }

                status = await session.call_tool("get_connection_status", {})
                assert not status.isError
                assert '"connected": true' in status.content[0].text

                info = await session.call_tool("get_database_info_tool", {})
                assert not info.isError
                assert '"paimon"' in info.content[0].text

                sql = (
                    "select * from paimon.csc.ods_csc_log_audit "
                    "where app_id='hids-identify-result-storage-svc' and year=2026 "
                    "and month=202606 and day=20260610 limit 2"
                )
                result = await session.call_tool("execute_sql", {"sql": sql})
                assert not result.isError
                assert '"row_count": 2' in result.content[0].text


async def test_authenticated_write_requires_readwrite_role(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with streamablehttp_client(
            "http://testserver/mcp",
            headers={"X-API-Key": READONLY_KEY},
            httpx_client_factory=_httpx_client_factory(app),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_sql", {"sql": "INSERT INTO paimon.csc.ods_csc_log_audit VALUES (1)"}
                )
                assert result.isError

        async with streamablehttp_client(
            "http://testserver/mcp",
            headers={"X-API-Key": READWRITE_KEY},
            httpx_client_factory=_httpx_client_factory(app),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_sql", {"sql": "UPDATE dw.orders SET status='paid' WHERE order_id=1001"}
                )
                assert not result.isError
                assert '"pool_used": "write"' in result.content[0].text
