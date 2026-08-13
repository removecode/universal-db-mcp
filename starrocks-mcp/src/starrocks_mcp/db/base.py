"""连接池统一接口。

真实的 pymysql 实现和 mock 实现都遵循这个接口，上层（安全审计、MCP 工具、
线程池桥接）不关心具体是哪种数据源。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


class PoolExhaustedError(RuntimeError):
    """连接池已达并发上限，在等待时限内没有取到空闲连接。

    刻意与"数据库连不上"区分开：池满说明库是通的、只是当前并发被占满，
    两者的处置手段完全不同（扩容/限流 vs 检查网络与账号）。
    """


@dataclass
class PoolStats:
    """连接池的实时占用情况，供监控与连通性诊断使用。"""

    in_use: int = 0
    max_connections: int = 0
    waiting: int = 0


class ConnectionSlots:
    """带等待上限的并发槽位，用来替代连接池自带的"无限期排队"。

    DBUtils 在 `blocking=True` 时用的是没有超时参数的 `Condition.wait()`：池子一满，
    后续每一次取连接都会永久挂起，既不报错也不打日志，现象只是调用方莫名其妙地卡住。
    这里把"等多久"变成显式参数，等不到就抛 `PoolExhaustedError`，顺带把占用情况
    暴露出来供监控和诊断使用。
    """

    def __init__(self, max_connections: int, role: str = "") -> None:
        self._role = role
        self._max = max_connections
        self._semaphore = threading.BoundedSemaphore(max_connections)
        self._lock = threading.Lock()
        self._in_use = 0
        self._waiting = 0

    @contextmanager
    def hold(self, timeout_seconds: float) -> Iterator[None]:
        with self._lock:
            self._waiting += 1
        try:
            acquired = self._semaphore.acquire(timeout=timeout_seconds)
        finally:
            with self._lock:
                self._waiting -= 1

        if not acquired:
            raise PoolExhaustedError(
                f"{self._role or '连接'}池已满（{self._in_use}/{self._max} 在用），"
                f"等待 {timeout_seconds:.1f}s 仍未取到空闲连接"
            )

        with self._lock:
            self._in_use += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_use -= 1
            self._semaphore.release()

    def stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(in_use=self._in_use, max_connections=self._max, waiting=self._waiting)


@dataclass
class ExecResult:
    """一次 SQL 执行的结果。"""

    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    # 仅 INSERT/UPDATE/DELETE 有意义
    affected_rows: int | None = None


class ConnectionPool(Protocol):
    """同步、阻塞式的连接池执行接口。调用方负责在线程池中执行以避免阻塞事件循环。"""

    def execute(self, sql: str) -> ExecResult:
        """执行一条 SQL 语句并返回结果。调用前应已完成安全审计。"""
        ...

    def ping(self) -> None:
        """探测连接是否可用。成功则静默返回，失败则抛出异常。"""
        ...

    def stats(self) -> PoolStats:
        """返回当前占用情况。"""
        ...

    def close(self) -> None:
        """释放连接池占用的资源（进程退出时调用）。"""
        ...
