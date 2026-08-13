"""基于 pymysql + DBUtils.PooledDB 的真实 StarRocks 连接池实现。

StarRocks 兼容 MySQL 协议，pymysql 客户端可以直接复用（用户提供的连接示例即是
标准 pymysql 用法）。这里用 DBUtils.PooledDB 做连接池管理，读、写各建一个实例，
分别绑定只读账号和读写账号。

关于超时的两条硬性约束（缺一就会让整个池子不可自愈）：

1. pymysql 必须配 `connect_timeout` / `read_timeout` / `write_timeout`。
   不配的话 socket 读会无限阻塞：上层 `asyncio.wait_for` 超时后取消不了工作线程，
   线程一直卡在 `cursor.execute()` 上，连接就永远还不回池子。
2. 取连接必须有等待上限。DBUtils 在 `blocking=True` 时用的是没有超时的
   `Condition.wait()`，池子一满就是永久挂起且不打任何日志。这里改成由外层
   信号量控制并发（带超时），PooledDB 自身设 `blocking=False` 只作兜底。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import pymysql
from dbutils.pooled_db import PooledDB

from ..config import DatabaseSettings, DbAccountSettings, PoolSettings
from .base import ConnectionPool, ConnectionSlots, ExecResult, PoolStats

logger = logging.getLogger(__name__)

# 探活取连接用更短的等待：池子满时应该立刻如实上报，而不是陪着业务一起卡住
PING_ACQUIRE_TIMEOUT_SECONDS = 1.0

_FALLBACK_MAX_CONNECTIONS = 10


def _normalize_pool_sizes(pool_settings: PoolSettings, role: str) -> tuple[int, int, int]:
    """把 mincached/maxcached/maxconnections 校正成自洽的一组值。

    DBUtils 会把 `maxconnections` 悄悄抬到不小于 `maxcached`（pooled_db 里的
    `maxconnections = max(maxconnections, maxcached)`），于是"配了 2 实际是 5"。
    这里反过来把 maxcached 压到 maxconnections 以下，让配置里写的数就是真实上限。
    """
    maxconnections = pool_settings.maxconnections
    if maxconnections <= 0:
        logger.warning(
            "%s 池 maxconnections=%s 无效（不支持无上限连接池），回退为 %s",
            role, maxconnections, _FALLBACK_MAX_CONNECTIONS,
        )
        maxconnections = _FALLBACK_MAX_CONNECTIONS

    maxcached = pool_settings.maxcached
    if maxcached > maxconnections:
        logger.warning(
            "%s 池 maxcached=%s 大于 maxconnections=%s，已压到 %s（否则 DBUtils 会反过来把并发上限抬到 %s）",
            role, maxcached, maxconnections, maxconnections, maxcached,
        )
        maxcached = maxconnections

    mincached = max(0, min(pool_settings.mincached, maxcached))
    return mincached, maxcached, maxconnections


class PyMySQLConnectionPool(ConnectionPool):
    """对某一个账号（只读或读写）建立的连接池，实现统一的 execute 接口。"""

    def __init__(
        self,
        host: str,
        port: int,
        account: DbAccountSettings,
        charset: str,
        pool_settings: PoolSettings,
        role: str = "read",
        connect_timeout: float = 10.0,
        socket_timeout: float = 90.0,
        acquire_timeout: float = 5.0,
    ) -> None:
        self.role = role
        self._acquire_timeout = acquire_timeout

        mincached, maxcached, maxconnections = _normalize_pool_sizes(pool_settings, role)
        self._slots = ConnectionSlots(maxconnections, role=role)

        self._pool = PooledDB(
            creator=pymysql,
            host=host,
            port=port,
            user=account.user,
            password=account.password,
            charset=charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            mincached=mincached,
            maxcached=maxcached,
            maxconnections=maxconnections,
            # 并发上限由 self._slots 控制（它有超时）；这里设 False 只是兜底，
            # 真越界时快速失败，而不是落进 DBUtils 那个没有超时的 Condition.wait()
            blocking=False,
            # 只做存活性检测，不依赖 setsession 在断线重连后重新生效（pymysql 已知限制）
            ping=1,
            # 没有这三个超时，socket 读会无限阻塞，超时的查询将永久占住连接
            connect_timeout=connect_timeout,
            read_timeout=socket_timeout,
            write_timeout=socket_timeout,
        )

    @contextmanager
    def _borrow(self, acquire_timeout: float | None = None) -> Iterator[object]:
        """借一条连接并保证归还，取不到时在时限内抛 PoolExhaustedError。"""
        wait_seconds = self._acquire_timeout if acquire_timeout is None else acquire_timeout
        with self._slots.hold(wait_seconds):
            conn = self._pool.connection()
            try:
                yield conn
            finally:
                try:
                    conn.close()  # 归还连接池，而不是真正关闭物理连接
                except Exception:  # noqa: BLE001 - 归还失败不应掩盖原始异常
                    logger.warning("%s 池归还连接失败", self.role, exc_info=True)

    def ping(self) -> None:
        """从连接池取一条连接执行 SELECT 1，验证账号与网络都可用。"""
        with self._borrow(acquire_timeout=PING_ACQUIRE_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchall()

    def execute(self, sql: str) -> ExecResult:
        with self._borrow() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                if cursor.description is not None:
                    rows = cursor.fetchall()
                    columns = [desc[0] for desc in cursor.description]
                    return ExecResult(columns=columns, rows=list(rows), row_count=len(rows))
                return ExecResult(columns=[], rows=[], row_count=0, affected_rows=cursor.rowcount)

    def stats(self) -> PoolStats:
        return self._slots.stats()

    def close(self) -> None:
        self._pool.close()


class PyMySQLReadWritePools:
    """同时持有只读池和读写池，供上层按语句类型选择。"""

    def __init__(self, settings: DatabaseSettings, socket_timeout: float) -> None:
        common = {
            "host": settings.host,
            "port": settings.port,
            "charset": settings.charset,
            "connect_timeout": settings.connect_timeout,
            "socket_timeout": socket_timeout,
            "acquire_timeout": settings.pool_acquire_timeout_seconds,
        }
        self.read_pool = PyMySQLConnectionPool(
            account=settings.read_account,
            pool_settings=settings.read_pool,
            role="read",
            **common,
        )
        self.write_pool = PyMySQLConnectionPool(
            account=settings.write_account,
            pool_settings=settings.write_pool,
            role="write",
            **common,
        )

    def close(self) -> None:
        self.read_pool.close()
        self.write_pool.close()
