"""数据库连通性检测：判断 MCP 是否能连上 StarRocks。

设计：
- 连接池统一提供 `ping()`：真实驱动执行 `SELECT 1`，mock 驱动永远成功并标明 mode=mock。
- MCP 工具 `get_connection_status`：给 LLM 在对话里主动检查。
- HTTP `GET /healthz`：给运维/探针使用；真实驱动连不上库时返回 503。
- `get_database_info` / `execute_sql` 遇到连接类错误时，改写成明确的
  “数据库未连接”提示，而不是直接抛出底层驱动堆栈。

探活必须能区分"连不上库"和"池子被占满"：两者的现象都是 ping 失败，但处置手段
完全不同。以前这里只报一个笼统的超时，导致池满被误判成账号或网络问题。
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from typing import Any

from .base import ConnectionPool, PoolExhaustedError, PoolStats

# 探活的失败原因，决定了给运维的结论完全不同
REASON_OK = "ok"
REASON_EXHAUSTED = "pool_exhausted"
REASON_UNREACHABLE = "unreachable"
REASON_TIMEOUT = "timeout"


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
    if isinstance(error, PoolExhaustedError):
        # 池满说明库是通的，只是并发被占满，不能混进“连不上”里
        return False
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
    reason: str = REASON_OK
    in_use: int = 0
    max_connections: int = 0
    waiting: int = 0


@dataclass
class ConnectionStatus:
    """整体连通性状态，供 MCP 工具和 /healthz 共用。

    `connected` 只回答"库还连得上吗"，`degraded` 表示"连得上但当前没有余量"。
    分开是因为处置完全不同：连不上要查网络/账号，池满要查慢查询——而且探针不该
    因为一次并发打满就去重启进程，重启只会打断正在跑的查询，帮倒忙。
    """

    connected: bool
    driver: str
    mode: str  # "mock" | "live"
    host: str | None
    port: int | None
    pools: list[PoolPingResult]
    message: str
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "degraded": self.degraded,
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
                    "reason": p.reason,
                    "in_use": p.in_use,
                    "max_connections": p.max_connections,
                    "waiting": p.waiting,
                }
                for p in self.pools
            ],
            "message": self.message,
        }


def _safe_stats(pool: ConnectionPool) -> PoolStats:
    getter = getattr(pool, "stats", None)
    if getter is None:
        return PoolStats()
    try:
        return getter()
    except Exception:  # noqa: BLE001 - 取不到统计不应让探活本身失败
        return PoolStats()


def ping_pool(pool: ConnectionPool) -> PoolPingResult:
    """同步探测单个连接池。调用方应在线程池中执行，避免阻塞事件循环。"""
    pool_name = str(getattr(pool, "role", None) or getattr(pool, "name", "unknown"))
    start = time.monotonic()

    def _build(connected: bool, error: str | None, reason: str) -> PoolPingResult:
        stats = _safe_stats(pool)
        return PoolPingResult(
            pool=pool_name,
            connected=connected,
            latency_ms=round((time.monotonic() - start) * 1000, 2),
            error=error,
            reason=reason,
            in_use=stats.in_use,
            max_connections=stats.max_connections,
            waiting=stats.waiting,
        )

    try:
        pool.ping()
        return _build(True, None, REASON_OK)
    except PoolExhaustedError as exc:
        return _build(False, str(exc), REASON_EXHAUSTED)
    except Exception as exc:  # noqa: BLE001 - ping 失败本身就是结果的一部分
        return _build(False, str(exc), REASON_UNREACHABLE)


def _summarize(
    pools: list[PoolPingResult], host: str | None, port: int | None
) -> tuple[bool, bool, str]:
    """汇总成 (connected, degraded, message)。"""
    failed = [p for p in pools if not p.connected]
    if not failed:
        return True, False, f"已成功连接到 StarRocks（{host}:{port}），只读池与读写池均可用。"

    details = "; ".join(f"{p.pool}: {p.error}" for p in failed)
    exhausted = [p for p in failed if p.reason == REASON_EXHAUSTED]
    if len(exhausted) == len(failed):
        # 全部失败都是池满：数据库是通的，问题在并发占用，不要误导成网络/账号问题
        usage = "，".join(f"{p.pool} {p.in_use}/{p.max_connections} 在用、{p.waiting} 个等待中" for p in exhausted)
        return True, True, (
            f"数据库可达（{host}:{port}），但连接池已被占满：{usage}。"
            "通常是有慢查询长时间占着连接；请检查 StarRocks 上是否有长时间运行的查询，"
            "必要时提高 database.pool.*.maxconnections 或收紧 security.query_timeout_seconds。"
            f"详情: {details}"
        )

    return False, False, (
        f"数据库未连接或当前不可用（{host}:{port}）。"
        f"失败的连接池: {details}。"
        "请检查数据库地址、端口、账号密码以及网络连通性。"
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
    """异步探测读/写两个连接池，汇总成 ConnectionStatus。

    `executor` 应该是一个专供健康检查使用的 PooledExecutor：探活和业务查询共用
    线程池时，业务把线程占满会让健康检查跟着失灵，恰好在最需要它说真话的时候。
    """
    import asyncio

    async def _ping_one(pool: ConnectionPool) -> PoolPingResult:
        try:
            return await executor.run(functools.partial(ping_pool, pool), timeout_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            stats = _safe_stats(pool)
            return PoolPingResult(
                pool=str(getattr(pool, "role", None) or "unknown"),
                connected=False,
                latency_ms=round(timeout_seconds * 1000, 2),
                error=f"连通性探测超过 {timeout_seconds}s 超时",
                reason=REASON_TIMEOUT,
                in_use=stats.in_use,
                max_connections=stats.max_connections,
                waiting=stats.waiting,
            )

    read_result, write_result = await asyncio.gather(
        _ping_one(read_pool),
        _ping_one(write_pool),
    )
    pools = [read_result, write_result]
    mode = "mock" if driver == "mock" else "live"

    if driver == "mock":
        return ConnectionStatus(
            connected=True,  # mock 模式对调用方来说“服务可用”
            driver=driver,
            mode=mode,
            host=host,
            port=port,
            pools=pools,
            message="当前运行在 mock 模式，未连接真实 StarRocks；服务可用，返回的是构造数据。",
        )

    connected, degraded, message = _summarize(pools, host, port)
    return ConnectionStatus(
        connected=connected,
        degraded=degraded,
        driver=driver,
        mode=mode,
        host=host,
        port=port,
        pools=pools,
        message=message,
    )
