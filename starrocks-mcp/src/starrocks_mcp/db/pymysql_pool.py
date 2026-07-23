"""基于 pymysql + DBUtils.PooledDB 的真实 StarRocks 连接池实现。

StarRocks 兼容 MySQL 协议，pymysql 客户端可以直接复用（用户提供的连接示例即是
标准 pymysql 用法）。这里用 DBUtils.PooledDB 做连接池管理，读、写各建一个实例，
分别绑定只读账号和读写账号。
"""

from __future__ import annotations

import logging

import pymysql
from dbutils.pooled_db import PooledDB

from ..config import DatabaseSettings, DbAccountSettings, PoolSettings
from .base import ConnectionPool, ExecResult

logger = logging.getLogger(__name__)


class PyMySQLConnectionPool(ConnectionPool):
    """对某一个账号（只读或读写）建立的连接池，实现统一的 execute 接口。"""

    def __init__(
        self,
        host: str,
        port: int,
        account: DbAccountSettings,
        charset: str,
        pool_settings: PoolSettings,
    ) -> None:
        self._pool = PooledDB(
            creator=pymysql,
            host=host,
            port=port,
            user=account.user,
            password=account.password,
            charset=charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            mincached=pool_settings.mincached,
            maxcached=pool_settings.maxcached,
            maxconnections=pool_settings.maxconnections,
            blocking=True,
            # 只做存活性检测，不依赖 setsession 在断线重连后重新生效（pymysql 已知限制）
            ping=1,
        )

    def execute(self, sql: str) -> ExecResult:
        conn = self._pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                if cursor.description is not None:
                    rows = cursor.fetchall()
                    columns = [desc[0] for desc in cursor.description]
                    return ExecResult(columns=columns, rows=list(rows), row_count=len(rows))
                return ExecResult(columns=[], rows=[], row_count=0, affected_rows=cursor.rowcount)
        finally:
            conn.close()  # 归还连接池，而不是真正关闭物理连接

    def close(self) -> None:
        self._pool.close()


class PyMySQLReadWritePools:
    """同时持有只读池和读写池，供上层按语句类型选择。"""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.read_pool = PyMySQLConnectionPool(
            host=settings.host,
            port=settings.port,
            account=settings.read_account,
            charset=settings.charset,
            pool_settings=settings.read_pool,
        )
        self.write_pool = PyMySQLConnectionPool(
            host=settings.host,
            port=settings.port,
            account=settings.write_account,
            charset=settings.charset,
            pool_settings=settings.write_pool,
        )

    def close(self) -> None:
        self.read_pool.close()
        self.write_pool.close()
