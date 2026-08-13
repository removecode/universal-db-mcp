"""数据库连通性检测相关测试。"""

from __future__ import annotations

import pytest

from starrocks_mcp.db.base import ExecResult, PoolExhaustedError, PoolStats
from starrocks_mcp.db.connectivity import (
    check_connection_status,
    format_connection_error,
    is_connection_error,
)
from starrocks_mcp.db.mock_pool import MockConnectionPool
from starrocks_mcp.handlers import handle_execute_sql, handle_get_connection_status


class FailingPool:
    """模拟一个连不上数据库的连接池。"""

    role = "read"

    def ping(self) -> None:
        raise ConnectionError("Can't connect to MySQL server on '10.0.0.1'")

    def execute(self, sql: str) -> ExecResult:  # noqa: ARG002
        raise ConnectionError("Can't connect to MySQL server on '10.0.0.1'")

    def close(self) -> None:
        return None


class WriteFailingPool(FailingPool):
    role = "write"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionError("Can't connect to MySQL server"), True),
        (TimeoutError("timed out"), True),
        (OSError("Connection refused"), True),
        (ValueError("Unknown column 'foo'"), False),
        (RuntimeError("You have an error in your SQL syntax"), False),
    ],
)
def test_is_connection_error_heuristic(error, expected):
    assert is_connection_error(error) is expected


def test_format_connection_error_is_human_readable():
    message = format_connection_error(ConnectionError("Can't connect to MySQL server"))
    assert "数据库未连接" in message
    assert "Can't connect to MySQL server" in message


async def test_check_connection_status_mock_mode(app_state):
    status = await check_connection_status(
        read_pool=app_state.read_pool,
        write_pool=app_state.write_pool,
        executor=app_state.executor,
        driver="mock",
        host=None,
        port=None,
    )
    assert status.connected is True
    assert status.mode == "mock"
    assert "mock" in status.message.lower()
    assert all(p.connected for p in status.pools)


async def test_check_connection_status_reports_failure_when_pool_down(app_state):
    status = await check_connection_status(
        read_pool=FailingPool(),
        write_pool=WriteFailingPool(),
        executor=app_state.executor,
        driver="pymysql",
        host="10.0.0.1",
        port=5744,
    )
    assert status.connected is False
    assert status.mode == "live"
    assert "数据库未连接" in status.message
    assert all(not p.connected for p in status.pools)


async def test_handle_get_connection_status_tool(app_state):
    result = await handle_get_connection_status(app_state)
    assert result["connected"] is True
    assert result["driver"] == "mock"
    assert "pools" in result


async def test_execute_sql_wraps_connection_failure_as_clear_message(app_state):
    from starrocks_mcp.auth.models import ApiKeyPermission

    app_state.read_pool = FailingPool()
    permission = ApiKeyPermission(username="rw", key_id="rw", role="readwrite", scope=None)

    with pytest.raises(RuntimeError, match="数据库未连接"):
        await handle_execute_sql(
            app_state,
            permission,
            "SELECT * FROM paimon.csc.ods_csc_log_audit LIMIT 1",
        )


async def test_mock_pool_ping_succeeds():
    pool = MockConnectionPool(role="read")
    pool.ping()  # 不应抛异常


class ExhaustedReadPool:
    """模拟读池被慢查询占满：库是通的，只是拿不到空闲连接。"""

    role = "read"

    def ping(self) -> None:
        raise PoolExhaustedError("read 连接池已满（2/2 在用），等待 1.0s 仍未取到空闲连接")

    def execute(self, sql: str) -> ExecResult:  # noqa: ARG002
        raise PoolExhaustedError("read 连接池已满（2/2 在用），等待 5.0s 仍未取到空闲连接")

    def stats(self) -> PoolStats:
        return PoolStats(in_use=2, max_connections=2, waiting=1)

    def close(self) -> None:
        return None


async def test_pool_exhaustion_is_not_reported_as_disconnected(app_state):
    """池满和连不上必须区分开。

    真实故障里读池被占满后，探活只报了一个笼统的超时，看起来像是读账号或网络有
    问题——而实际上写池同账号同地址、一毫秒就能通。
    """
    status = await check_connection_status(
        read_pool=ExhaustedReadPool(),
        write_pool=app_state.write_pool,
        executor=app_state.health_executor,
        driver="pymysql",
        host="10.0.0.1",
        port=5744,
    )
    assert status.connected is True  # 库可达，别把运维引到网络/账号上去
    assert status.degraded is True
    assert "连接池已被占满" in status.message
    assert "数据库未连接" not in status.message

    read = next(p for p in status.pools if p.pool == "read")
    assert read.reason == "pool_exhausted"
    assert (read.in_use, read.max_connections, read.waiting) == (2, 2, 1)


async def test_pool_exhaustion_error_is_distinct_from_connection_error(app_state):
    from starrocks_mcp.auth.models import ApiKeyPermission

    app_state.read_pool = ExhaustedReadPool()
    permission = ApiKeyPermission(username="rw", key_id="rw", role="readwrite", scope=None)

    with pytest.raises(RuntimeError, match="连接池已满") as excinfo:
        await handle_execute_sql(
            app_state, permission, "SELECT * FROM paimon.csc.ods_csc_log_audit LIMIT 1"
        )
    assert "数据库未连接" not in str(excinfo.value)


def test_pool_exhausted_is_not_classified_as_connection_error():
    assert is_connection_error(PoolExhaustedError("read 连接池已满（2/2 在用）")) is False
