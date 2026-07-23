"""MCP 工具的业务逻辑实现（脱离 FastMCP/Context，方便单测直接调用）。

`server.py` 里的 `@mcp.tool()` 只负责：从 Context 里取出 apikey 权限，然后调用这里的
函数并把结果原样返回。所有安全审计、连接池选择、监控埋点、审计日志的逻辑都在这里。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from .auth.models import ApiKeyPermission
from .db.connectivity import (
    check_connection_status,
    format_connection_error,
    is_connection_error,
)
from .db.executor import QueryTimeoutError
from .schema.catalog import get_database_info
from .security.audit import SqlAuditError, audit_sql, classify_statement, split_statements

if TYPE_CHECKING:
    from .server import AppState


async def handle_get_connection_status(state: "AppState") -> dict[str, Any]:
    """探测读/写连接池是否可用，返回结构化连通性状态。"""
    settings = state.settings
    status = await check_connection_status(
        read_pool=state.read_pool,
        write_pool=state.write_pool,
        executor=state.executor,
        driver=settings.database.driver,
        host=None if settings.database.driver == "mock" else settings.database.host,
        port=None if settings.database.driver == "mock" else settings.database.port,
        timeout_seconds=min(5.0, settings.security.query_timeout_seconds),
    )
    return status.to_dict()


async def handle_get_database_info(
    state: "AppState",
    permission: ApiKeyPermission,
    catalog: str | None = None,
    database: str | None = None,
    table: str | None = None,
) -> dict[str, Any]:
    try:
        return await get_database_info(
            pool=state.read_pool,
            executor=state.executor,
            permission=permission,
            catalog=catalog,
            database=database,
            table=table,
            timeout_seconds=state.settings.security.query_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        if is_connection_error(exc):
            raise RuntimeError(format_connection_error(exc)) from exc
        raise


def _schedule_request_log(state: "AppState", **kwargs: Any) -> None:
    """把审计日志写入丢到线程池执行，避免阻塞事件循环（SQLite 写入很快，但仍是磁盘 I/O）。"""
    log_kwargs = {"sql_text_max_length": state.settings.monitoring.sql_text_max_length, **kwargs}
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, lambda: state.request_logger.log(**log_kwargs))
    except RuntimeError:  # pragma: no cover - 理论上工具只在事件循环内被调用
        state.request_logger.log(**log_kwargs)


async def handle_execute_sql(
    state: "AppState",
    permission: ApiKeyPermission,
    sql: str,
    max_rows: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    settings = state.settings
    start = time.monotonic()
    statement_type = "UNKNOWN"
    pool_name = "read"
    status = "error"
    row_count = 0
    error_message: str | None = None

    try:
        audit_result = audit_sql(sql, permission, settings.security)
        statement_type = audit_result.statement_type
        pool_name = audit_result.pool

        effective_timeout = settings.security.query_timeout_seconds
        if timeout_seconds is not None and timeout_seconds > 0:
            effective_timeout = min(timeout_seconds, settings.security.query_timeout_seconds)

        pool = state.pool_for(pool_name)
        state.metrics.track_pool_usage(pool_name, 1)
        try:
            result = await state.executor.execute(pool, audit_result.sql_to_execute, effective_timeout)
        finally:
            state.metrics.track_pool_usage(pool_name, -1)

        rows = result.rows
        if max_rows is not None and max_rows >= 0:
            rows = rows[:max_rows]

        row_count = len(rows) if result.affected_rows is None else 0
        status = "success"

        return {
            "columns": result.columns,
            "rows": rows,
            "row_count": row_count,
            "affected_rows": result.affected_rows,
            "statement_type": statement_type,
            "pool_used": pool_name,
            "limit_applied": audit_result.limit_applied,
            "limit_source": audit_result.limit_source,
            "truncated": audit_result.truncated,
            "execution_time_ms": round((time.monotonic() - start) * 1000, 2),
        }
    except SqlAuditError as exc:
        status = "rejected"
        error_message = str(exc)
        try:
            stmts = split_statements(sql)
            if len(stmts) == 1:
                statement_type = classify_statement(stmts[0])
        except Exception:  # noqa: BLE001
            pass
        raise ValueError(f"SQL 未通过安全审计: {exc}") from exc
    except QueryTimeoutError as exc:
        status = "timeout"
        error_message = str(exc)
        raise
    except Exception as exc:  # noqa: BLE001 - 需要统一记录后再向上抛出
        if is_connection_error(exc):
            status = "disconnected"
            error_message = format_connection_error(exc)
            raise RuntimeError(error_message) from exc
        error_message = str(exc)
        raise
    finally:
        duration_seconds = time.monotonic() - start
        state.metrics.record_execution(
            api_key_id=permission.key_id,
            statement_type=statement_type,
            status=status,
            duration_seconds=duration_seconds,
            row_count=row_count,
        )
        _schedule_request_log(
            state,
            api_key_id=permission.key_id,
            statement_type=statement_type,
            sql_text=sql,
            pool=pool_name,
            duration_ms=round(duration_seconds * 1000, 2),
            row_count=row_count,
            status=status,
            error=error_message,
        )
