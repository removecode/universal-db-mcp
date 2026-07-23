"""数据库连通性检测：判断 MCP 是否能连上 StarRocks。

设计：
- 连接池统一提供 `ping()`：真实驱动执行 `SELECT 1`，mock 驱动永远成功并标明 mode=mock。
- MCP 工具 `get_connection_status`：给 LLM 在对话里主动检查。
- HTTP `GET /healthz`：给运维/探针使用；真实驱动连不上库时返回 503。
- `get_database_info` / `execute_sql` 遇到连接类错误时，改写成明确的
  “数据库未连接”提示，而不是直接抛出底层驱动堆栈。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .base import ConnectionPool


class DatabaseConnectionError(RuntimeError):
    """数据库连通性异常：连接失败、连接中断、认证失败等。

    上层应把这类错误转成面向调用方的明确提示，而不是暴露底层驱动细节。
    """


# 常见的连接/网络类错误关键字（pymysql、操作系统 errno、DBUtils 包装后的文案）
_CONNECTION_ERROR_PATTERNS = (
    "can't connect",
    "cannot connect",
    "connection refused",
    "connection reset",
    "connection timed out",
    "connect timeout",
    "timed out",
    "timeout",
    "broken pipe",
    "server has gone away",
    "lost connection",
    "connection lost",
    "not connected",
    "closed state",
    "econnrefused",
    "econnreset",
    "etimedout",
    "epipe",
    "network is unreachable",
    "name or service not known",
    "nodename nor servname",
    "access denied",
    "authentication",
    "protocol_connection_lost",
)


def is_connection_error(error: BaseException) -> bool:
    """启发式判断某个异常是否属于“数据库连不上”这一类。"""
    if isinstance(error, DatabaseConnectionError):
        return True
    # pymysql 的 OperationalError / InterfaceError 通常是连接问题
    name = type(error).__name__.lower()
    if name in {"operationalerror", "interfaceerror", "databaseerror"}:
        # DatabaseError 范围偏大，还要结合消息判断；Operational/Interface 基本可认定
        if name in {"operationalerror", "interfaceerror"}:
            return True
    message = str(error).lower()
    return any(pattern in message for pattern in _CONNECTION_ERROR_PATTERNS)


def format_connection_error(error: BaseException) -> str:
    """把底层异常改写成面向调用方的明确提示。"""
    detail = str(error).strip() or type(error).__name__
    return (
        "数据库未连接或当前不可用。"
        f"请检查 StarRocks 地址/端口/账号密码、网络连通性以及 MCP 服务端配置"
        f"（database.driver / host / port / read_account / write_account）。"
        f"底层错误: {detail}"
    )


@dataclass
class PoolPingResult:
    pool: str
    connected: bool
    latency_ms: float | None = None
    error: str | None = None


@dataclass
class ConnectionStatus:
    """整体连通性状态，供 MCP 工具和 /healthz 共用。"""

    connected: bool
    driver: str
    mode: str  # "mock" | "live"
    host: str | None
    port: int | None
    pools: list[PoolPingResult]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "driver": self.driver,
            "mode": self.mode,
            "host": self.host,
            "port": self.port,
            "pools": [
                {
                    "pool": p.pool,
                    "connected": p.connected,
                    "latency_ms": p.latency_ms,
                    "error": p.error,
                }
                for p in self.pools
            ],
            "message": self.message,
        }


def ping_pool(pool: ConnectionPool) -> PoolPingResult:
    """同步探测单个连接池。调用方应在线程池中执行，避免阻塞事件循环。"""
    pool_name = getattr(pool, "role", None) or getattr(pool, "name", "unknown")
    start = time.monotonic()
    try:
        pool.ping()
        return PoolPingResult(
            pool=str(pool_name),
            connected=True,
            latency_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except Exception as exc:  # noqa: BLE001 - ping 失败本身就是结果的一部分
        return PoolPingResult(
            pool=str(pool_name),
            connected=False,
            latency_ms=round((time.monotonic() - start) * 1000, 2),
            error=str(exc),
        )


async def check_connection_status(
    *,
    read_pool: ConnectionPool,
    write_pool: ConnectionPool,
    executor: Any,
    driver: str,
    host: str | None,
    port: int | None,
    timeout_seconds: float = 5.0,
) -> ConnectionStatus:
    """异步探测读/写两个连接池，汇总成 ConnectionStatus。"""
    import asyncio

    loop = asyncio.get_running_loop()

    async def _ping_one(pool: ConnectionPool) -> PoolPingResult:
        future = loop.run_in_executor(executor._pool, ping_pool, pool)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            pool_name = getattr(pool, "role", None) or "unknown"
            return PoolPingResult(
                pool=str(pool_name),
                connected=False,
                latency_ms=round(timeout_seconds * 1000, 2),
                error=f"连通性探测超过 {timeout_seconds}s 超时",
            )

    read_result, write_result = await asyncio.gather(
        _ping_one(read_pool),
        _ping_one(write_pool),
    )
    pools = [read_result, write_result]
    connected = all(p.connected for p in pools)
    mode = "mock" if driver == "mock" else "live"

    if driver == "mock":
        message = "当前运行在 mock 模式，未连接真实 StarRocks；服务可用，返回的是构造数据。"
        connected = True  # mock 模式对调用方来说“服务可用”
    elif connected:
        message = f"已成功连接到 StarRocks（{host}:{port}），只读池与读写池均可用。"
    else:
        failed = [p for p in pools if not p.connected]
        details = "; ".join(f"{p.pool}: {p.error}" for p in failed)
        message = (
            f"数据库未连接或当前不可用（{host}:{port}）。"
            f"失败的连接池: {details}。"
            "请检查数据库地址、端口、账号密码以及网络连通性。"
        )

    return ConnectionStatus(
        connected=connected,
        driver=driver,
        mode=mode,
        host=host,
        port=port,
        pools=pools,
        message=message,
    )
