"""把同步连接池执行桥接进 asyncio，并施加超时控制。"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from .base import ConnectionPool, ExecResult

logger = logging.getLogger(__name__)


class QueryTimeoutError(TimeoutError):
    """SQL 执行超过配置的超时时间。"""


class PooledExecutor:
    """用一个共享的线程池，把多个 ConnectionPool 的阻塞调用桥接进 asyncio。

    注意（已知限制）：`asyncio.wait_for` 超时后只是让调用方提前拿到超时错误，
    工作线程里的 `pool.execute()` 可能仍在数据库侧继续运行，直到底层驱动/数据库
    自身超时或连接被复用前完成。生产环境建议同时在数据库侧设置会话级查询超时
    作为兜底（StarRocks 可通过账号/资源组级别的 query timeout 配置）。
    """

    def __init__(self, max_workers: int = 32):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="db-exec")

    async def execute(
        self,
        pool: ConnectionPool,
        sql: str,
        timeout_seconds: float,
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, pool.execute, sql)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            logger.warning("SQL 执行超时 (%.1fs): %s", timeout_seconds, sql[:200])
            raise QueryTimeoutError(f"查询执行超过 {timeout_seconds}s 超时限制") from exc

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
