"""按用户名记录每次 execute_sql 调用的审计日志，落地到本地 SQLite。

用 SQLite 而不是纯文本日志，是为了能直接用 SQL 查询"某个用户最近的请求记录"，
不需要额外接日志系统就能满足审计需求；后续如果要接入集中式日志/审计系统，
可以在 `log()` 里追加一次转发，不影响调用方接口。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RequestLogEntry:
    id: int
    ts: str
    username: str
    api_key_id: str
    statement_type: str | None
    sql_text: str | None
    pool: str | None
    duration_ms: float | None
    row_count: int | None
    status: str
    error: str | None


class RequestLogger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    username TEXT NOT NULL,
                    api_key_id TEXT NOT NULL,
                    statement_type TEXT,
                    sql_text TEXT,
                    pool TEXT,
                    duration_ms REAL,
                    row_count INTEGER,
                    status TEXT NOT NULL,
                    error TEXT
                )
                """
            )
            # 兼容旧库：若早期版本没有 username 列，补上
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(request_log)").fetchall()
            }
            if "username" not in columns:
                conn.execute(
                    "ALTER TABLE request_log ADD COLUMN username TEXT NOT NULL DEFAULT ''"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_username ON request_log(username)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_apikey ON request_log(api_key_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts)")
            conn.commit()

    def log(
        self,
        *,
        username: str,
        api_key_id: str,
        statement_type: str | None,
        sql_text: str | None,
        pool: str | None,
        duration_ms: float | None,
        row_count: int | None,
        status: str,
        error: str | None = None,
        sql_text_max_length: int = 2000,
    ) -> None:
        text = (sql_text or "")[:sql_text_max_length]
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO request_log
                    (ts, username, api_key_id, statement_type, sql_text, pool, duration_ms, row_count, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    username,
                    api_key_id,
                    statement_type,
                    text,
                    pool,
                    duration_ms,
                    row_count,
                    status,
                    error,
                ),
            )
            conn.commit()

    def get_recent(
        self,
        username: str | None = None,
        api_key_id: str | None = None,
        limit: int = 50,
    ) -> list[RequestLogEntry]:
        with self._lock, self._connect() as conn:
            if username:
                cursor = conn.execute(
                    "SELECT * FROM request_log WHERE username = ? ORDER BY id DESC LIMIT ?",
                    (username, limit),
                )
            elif api_key_id:
                cursor = conn.execute(
                    "SELECT * FROM request_log WHERE api_key_id = ? ORDER BY id DESC LIMIT ?",
                    (api_key_id, limit),
                )
            else:
                cursor = conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,))
            return [RequestLogEntry(**dict(row)) for row in cursor.fetchall()]
