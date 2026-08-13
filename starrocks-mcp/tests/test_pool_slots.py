"""连接池并发槽位的行为测试。

这些用例锁的是一次真实故障：慢查询超时后连接没有归还，读池被占满，之后每一次
取连接都停在 DBUtils 那个没有超时的 `Condition.wait()` 上——包括健康检查的
`SELECT 1`。表现为"读写同账号，读探活超时、写正常"，极易被误判成账号或网络问题。
"""

from __future__ import annotations

import threading
import time

import pytest

from starrocks_mcp.config import PoolSettings
from starrocks_mcp.db.base import ConnectionSlots, PoolExhaustedError
from starrocks_mcp.db.executor import PooledExecutor, QueryTimeoutError
from starrocks_mcp.db.pymysql_pool import _normalize_pool_sizes


def test_slots_are_released_after_use():
    slots = ConnectionSlots(2, role="read")
    with slots.hold(0.1):
        assert slots.stats().in_use == 1
    assert slots.stats().in_use == 0


def test_slot_released_even_when_body_raises():
    slots = ConnectionSlots(1, role="read")
    with pytest.raises(ValueError):
        with slots.hold(0.1):
            raise ValueError("boom")
    assert slots.stats().in_use == 0
    # 还能继续借出，说明槽位没有泄漏
    with slots.hold(0.1):
        pass


def test_exhausted_pool_fails_fast_instead_of_blocking_forever():
    slots = ConnectionSlots(1, role="read")
    with slots.hold(0.1):
        start = time.monotonic()
        with pytest.raises(PoolExhaustedError, match="池已满"):
            with slots.hold(0.05):
                pass
        # 关键：在等待上限内返回，而不是永久挂起
        assert time.monotonic() - start < 1.0


def test_waiting_count_is_visible_while_blocked():
    """池满时排队数要能被观测到，否则监控上看不出是"卡在等连接"。"""
    slots = ConnectionSlots(1, role="read")
    observed: list[int] = []

    def _waiter() -> None:
        try:
            with slots.hold(0.5):
                pass
        except PoolExhaustedError:
            pass

    with slots.hold(1.0):
        thread = threading.Thread(target=_waiter)
        thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if slots.stats().waiting == 1:
                observed.append(1)
                break
            time.sleep(0.01)
        thread.join()

    assert observed == [1]


def test_slot_capacity_is_actually_enforced():
    slots = ConnectionSlots(2, role="read")
    with slots.hold(0.1), slots.hold(0.1):
        assert slots.stats().in_use == 2
        with pytest.raises(PoolExhaustedError):
            with slots.hold(0.01):
                pass


def test_maxcached_cannot_silently_raise_the_concurrency_cap():
    """DBUtils 会把 maxconnections 抬到不小于 maxcached，导致"配了 2 实际是 5"。"""
    mincached, maxcached, maxconnections = _normalize_pool_sizes(
        PoolSettings(mincached=1, maxcached=5, maxconnections=2), role="read"
    )
    assert maxconnections == 2
    assert maxcached == 2
    assert mincached == 1


def test_invalid_maxconnections_falls_back_instead_of_unbounded():
    # DBUtils 把 0 解释成“连接数无上限”，那正是要避免的情况
    _, _, maxconnections = _normalize_pool_sizes(
        PoolSettings(mincached=1, maxcached=5, maxconnections=0), role="read"
    )
    assert maxconnections > 0


async def test_shutdown_is_not_blocked_by_a_stuck_worker():
    """关闭线程池不能为卡住的工作线程无限等待。

    以前 shutdown 用的是 wait=True：查询超时后线程还堵在 cursor.execute 上，
    进程退出流程就跟着卡死，Ctrl-C 和 SIGTERM 都没反应，只能 kill -9。
    """
    executor = PooledExecutor(max_workers=2)
    release = threading.Event()
    try:
        with pytest.raises(QueryTimeoutError):
            await executor.run(lambda: release.wait(timeout=30), timeout_seconds=0.05)

        start = time.monotonic()
        executor.shutdown()
        assert time.monotonic() - start < 2.0
    finally:
        release.set()
