"""组装 FastMCP 实例、两个 MCP 工具、鉴权中间件、监控端点，得到最终的 ASGI 应用。"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from .auth.provider import AuthProvider, YamlAuthProvider
from .config import Settings
from .db.base import ConnectionPool
from .db.executor import PooledExecutor
from .db.mock_pool import MockConnectionPool
from .db.pymysql_pool import PyMySQLReadWritePools
from .handlers import handle_execute_sql, handle_get_database_info
from .middleware import ApiKeyAuthMiddleware, get_permission_from_context
from .monitoring.metrics import Metrics
from .monitoring.request_log import RequestLogger

logger = logging.getLogger(__name__)


class AppState:
    """持有整个服务运行期间的共享资源（连接池、鉴权、监控）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth_provider: AuthProvider = YamlAuthProvider(settings.resolve_path(settings.auth.apikeys_file))
        self.executor = PooledExecutor(max_workers=settings.database.executor_max_workers)
        self.metrics = Metrics()
        self.request_logger = RequestLogger(settings.resolve_path(settings.monitoring.audit_db_path))

        self._real_pools: PyMySQLReadWritePools | None = None
        self.read_pool: ConnectionPool
        self.write_pool: ConnectionPool

        driver = settings.database.driver
        if driver == "mock":
            logger.warning("database.driver=mock：当前使用构造数据，不会连接真实 StarRocks")
            self.read_pool = MockConnectionPool(role="read")
            self.write_pool = MockConnectionPool(role="write")
        elif driver == "pymysql":
            self._real_pools = PyMySQLReadWritePools(settings.database)
            self.read_pool = self._real_pools.read_pool
            self.write_pool = self._real_pools.write_pool
        else:
            raise ValueError(f"未知的 database.driver: {driver!r}，目前支持 mock / pymysql")

    def pool_for(self, pool_name: str) -> ConnectionPool:
        return self.read_pool if pool_name == "read" else self.write_pool

    def close(self) -> None:
        self.executor.shutdown()
        if self._real_pools is not None:
            self._real_pools.close()


def _register_tools(mcp: FastMCP, state: AppState) -> None:
    @mcp.tool()
    async def get_database_info_tool(
        ctx: Context,
        catalog: str | None = None,
        database: str | None = None,
        table: str | None = None,
    ) -> dict[str, Any]:
        """获取当前 StarRocks 集群的结构信息，帮助 LLM 理解可查询的数据。

        参数均可选，按层级传入：
        - 都不传：返回当前 apikey 可见的所有 catalog。
        - 只传 catalog：返回该 catalog 下的所有 database。
        - 传 catalog + database（或只传 database，catalog 默认为 default_catalog）：
          返回该 database 下的所有 table。
        - 传 catalog + database + table：返回该表的列结构；如果是 StarRocks 内部表，
          还会附带建表语句（含分区/分桶信息）；外部 catalog 的表则只有列信息。
        """
        permission = get_permission_from_context(ctx)
        return await handle_get_database_info(
            state, permission, catalog=catalog, database=database, table=table
        )

    @mcp.tool()
    async def execute_sql(
        ctx: Context,
        sql: str,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """执行一条 SQL 查询或写入语句。

        执行前会先做安全审计：
        - 按当前 apikey 的角色（read / readwrite）校验语句类型是否被允许；
        - 校验语句涉及的表是否在 apikey 的访问范围（scope）内；
        - 对 SELECT 语句强制注入/收紧 LIMIT，避免全表扫描。
        审计通过后按语句类型路由到只读或读写连接池执行，并记录监控指标与审计日志。

        `max_rows` / `timeout_seconds` 只能收紧限制，不能突破服务端配置的安全上限。
        """
        permission = get_permission_from_context(ctx)
        return await handle_execute_sql(
            state, permission, sql, max_rows=max_rows, timeout_seconds=timeout_seconds
        )


async def _health_endpoint(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_asgi_app(settings: Settings) -> tuple[ASGIApp, AppState]:
    """构建最终对外提供服务的 ASGI 应用：/metrics、/healthz 不鉴权，其余（MCP 协议）需要 apikey。"""
    state = AppState(settings)

    mcp = FastMCP(
        "starrocks-mcp",
        host=settings.server.host,
        port=settings.server.port,
        stateless_http=settings.server.stateless_http,
    )
    _register_tools(mcp, state)

    mcp_app = mcp.streamable_http_app()
    authenticated_mcp_app = ApiKeyAuthMiddleware(
        mcp_app,
        auth_provider=state.auth_provider,
        header_name=settings.auth.header_name,
    )

    routes = [Route("/healthz", _health_endpoint)]
    if settings.monitoring.metrics_enabled:
        routes.append(Route(settings.monitoring.metrics_path, state.metrics.endpoint))
    routes.append(Mount("/", app=authenticated_mcp_app))

    # mcp_app（streamable_http_app() 返回的子应用）自带的 lifespan 负责启动/关闭
    # StreamableHTTP 的会话管理后台任务；被当作子应用 Mount 进来后不会被外层
    # Starlette 自动调用，需要手动把它的 lifespan 接进外层应用的 lifespan 里，
    # 否则请求会因为会话管理器没有运行而失败。
    @asynccontextmanager
    async def combined_lifespan(_outer_app: Starlette):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            yield

    app = Starlette(routes=routes, lifespan=combined_lifespan)
    return app, state
