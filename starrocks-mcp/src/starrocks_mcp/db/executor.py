"""把同步连接池执行桥接进 asyncio，并施加超时控制。"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from .base import ConnectionPool, ExecResult

logger = logging.getLogger(__name__)

T = TypeVar("T")


class QueryTimeoutError(TimeoutError):
    """SQL 执行超过配置的超时时间。"""


class PooledExecutor:
    """用一个共享的线程池，把多个 ConnectionPool 的阻塞调用桥接进 asyncio。

    `asyncio.wait_for` 超时后只是让调用方提前拿到超时错误，工作线程里的
    `pool.execute()` 会继续跑到底层驱动返回为止。要让线程（以及它占着的那条
    连接）真的能被释放，必须同时满足：

    - pymysql 配了 `read_timeout`（见 `pymysql_pool`），保证 socket 读有上限；
    - SELECT 带上 `/*+ SET_VAR(query_timeout=N) */`（见 `security.audit`），
      让 StarRocks 侧也主动终止查询。

    这两条都在本项目里默认开启，因此超时的最坏影响被限制在 socket 超时之内，
    而不会像以前那样把连接永久扣住。
    """

    def __init__(self, max_workers: int = 32, thread_name_prefix: str = "db-exec"):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

    async def run(self, fn: Callable[[], T], timeout_seconds: float) -> T:
        """在线程池里执行一个同步调用，超时抛 QueryTimeoutError。"""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, fn)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise QueryTimeoutError(f"执行超过 {timeout_seconds}s 超时限制") from exc

    async def execute(
        self,
        pool: ConnectionPool,
        sql: str,
        timeout_seconds: float,
    ) -> ExecResult:
        try:
            return await self.run(functools.partial(pool.execute, sql), timeout_seconds)
        except QueryTimeoutError as exc:
            # 取连接有独立的等待上限（超时会抛 PoolExhaustedError），所以走到这里
            # 一定是 SQL 本身没跑完，而不是在排队等连接
            logger.warning("SQL 执行超时 (%.1fs)，数据库侧可能仍在收尾: %s", timeout_seconds, sql[:200])
            raise QueryTimeoutError(f"查询执行超过 {timeout_seconds}s 超时限制") from exc

    def shutdown(self, wait: bool = False) -> None:
        """关闭线程池。

        默认不等待：卡住的工作线程最长只会被 socket 超时拖住，让进程退出流程
        为它们无限期阻塞没有意义（以前 `wait=True` 会把 Ctrl-C 直接卡死）。
        """
        self._pool.shutdown(wait=wait, cancel_futures=True)
