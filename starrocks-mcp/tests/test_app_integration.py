"""端到端集成测试：完整组装 ASGI 应用（鉴权中间件 + FastMCP SSE/streamable-http + 生命周期），
用 httpx.ASGITransport 在进程内驱动，不需要真实网络端口。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from starrocks_mcp.server import build_asgi_app

from .conftest import READONLY_KEY, READWRITE_KEY

INVALID_KEY = "definitely-not-a-valid-key"


def _httpx_client_factory(app):
    def factory(headers=None, timeout=None, auth=None):  # noqa: ARG001 - 兼容 mcp 客户端的工厂签名
        kwargs: dict = {"transport": httpx.ASGITransport(app=app), "follow_redirects": True}
        if headers:
            kwargs["headers"] = headers
        return httpx.AsyncClient(**kwargs)

    return factory


@asynccontextmanager
async def _mcp_session(app, api_key: str) -> AsyncIterator[ClientSession]:
    async with streamablehttp_client(
        "http://testserver/mcp",
        headers={"X-API-Key": api_key},
        httpx_client_factory=_httpx_client_factory(app),
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session

async def _post_mcp_initialize(client: httpx.AsyncClient, api_key: str | None = None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    return await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=headers,
    )


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
            resp = await _post_mcp_initialize(client)
            assert resp.status_code == 401
            sse_resp = await client.get("/sse", headers={"Accept": "text/event-stream"})
            assert sse_resp.status_code == 401


async def test_invalid_apikey_request_rejected(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await _post_mcp_initialize(client, api_key=INVALID_KEY)
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


async def test_sse_routes_are_mounted(app_and_state):
    app, _state = app_and_state
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/healthz" in paths
    assert "" in paths  # MCP 协议子应用 Mount（Starlette 根路径）

    mcp_mount = next(route for route in app.routes if getattr(route, "path", None) == "")
    mcp_app = mcp_mount.app.app  # ApiKeyAuthMiddleware -> Starlette
    mcp_paths = {getattr(route, "path", None) for route in mcp_app.routes}
    assert "/sse" in mcp_paths
    assert "/mcp" in mcp_paths
    message_mounts = [route for route in mcp_app.routes if getattr(route, "path", None) == "/messages"]
    assert message_mounts


async def test_authenticated_full_round_trip_matches_user_example(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READONLY_KEY) as session:
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


async def test_select_without_limit_is_allowed_and_gets_default_injected(app_and_state):
    """未写 LIMIT 的 SELECT 不会被拒绝，而是自动注入 default_row_limit。"""
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READONLY_KEY) as session:
            sql = (
                "select * from paimon.csc.ods_csc_log_audit "
                "where app_id='hids-identify-result-storage-svc' and year=2026"
            )
            result = await session.call_tool("execute_sql", {"sql": sql})
            assert not result.isError
            text = result.content[0].text
            assert '"limit_source": "default_injected"' in text
            assert '"limit_applied": 1000' in text
            assert '"statement_type": "SELECT"' in text


async def test_authenticated_write_requires_readwrite_role(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READONLY_KEY) as session:
            result = await session.call_tool(
                "execute_sql", {"sql": "INSERT INTO paimon.csc.ods_csc_log_audit VALUES (1)"}
            )
            assert result.isError

        async with _mcp_session(app, READWRITE_KEY) as session:
            result = await session.call_tool(
                "execute_sql", {"sql": "UPDATE dw.orders SET status='paid' WHERE order_id=1001"}
            )
            assert not result.isError
            assert '"pool_used": "write"' in result.content[0].text


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM paimon.csc.ods_csc_log_audit WHERE app_id='x'",
        "DROP TABLE paimon.csc.ods_csc_log_audit",
        "TRUNCATE TABLE paimon.csc.ods_csc_log_audit",
    ],
)
async def test_readonly_dangerous_sql_rejected_via_mcp(app_and_state, sql: str):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READONLY_KEY) as session:
            result = await session.call_tool("execute_sql", {"sql": sql})
            assert result.isError
            assert "安全审计" in result.content[0].text


async def test_ddl_rejected_even_for_readwrite_via_mcp(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READWRITE_KEY) as session:
            result = await session.call_tool("execute_sql", {"sql": "DROP TABLE dw.orders"})
            assert result.isError
            assert "安全审计" in result.content[0].text


async def test_readonly_out_of_scope_query_rejected_via_mcp(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READONLY_KEY) as session:
            result = await session.call_tool(
                "execute_sql", {"sql": "select * from default_catalog.dw.orders limit 1"}
            )
            assert result.isError
            assert "安全审计" in result.content[0].text


async def test_multi_statement_sql_rejected_via_mcp(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READWRITE_KEY) as session:
            result = await session.call_tool("execute_sql", {"sql": "SELECT 1; SELECT 2"})
            assert result.isError
            assert "安全审计" in result.content[0].text


async def test_readonly_schema_scope_violation_rejected_via_mcp(app_and_state):
    app, _state = app_and_state
    async with app.router.lifespan_context(app):
        async with _mcp_session(app, READONLY_KEY) as session:
            result = await session.call_tool(
                "get_database_info_tool",
                {"catalog": "default_catalog", "database": "dw"},
            )
            assert result.isError
            assert "无权访问" in result.content[0].text
