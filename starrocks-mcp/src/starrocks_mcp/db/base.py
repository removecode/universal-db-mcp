"""连接池统一接口。

真实的 pymysql 实现和 mock 实现都遵循这个接口，上层（安全审计、MCP 工具、
线程池桥接）不关心具体是哪种数据源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


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

    def close(self) -> None:
        """释放连接池占用的资源（进程退出时调用）。"""
        ...
