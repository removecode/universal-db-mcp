"""Mock 连接池：不连接真实 StarRocks，按语句模式返回构造数据。

用于当前没有真实 StarRocks 环境时的开发、自动化测试和手动验证。
数据故意贴近用户提供的真实示例（`paimon.csc.ods_csc_log_audit`），
同时补了一个 default_catalog 下的原生表（`dw.orders`），用于演示
"内部表有分区/分桶信息、外部 catalog 表没有" 的差异。

通过配置 `database.driver: mock` 启用；接口与 `PyMySQLConnectionPool`
完全一致，可以随时切换到真实实现。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .base import ConnectionPool, ExecResult

# ---------------------------------------------------------------------------
# 构造的“数据库世界”：两个 catalog，一个是 StarRocks 内部表，一个是外部 Paimon catalog
# ---------------------------------------------------------------------------

_CATALOGS = ["default_catalog", "paimon"]

_DATABASES: dict[str, list[str]] = {
    "default_catalog": ["dw"],
    "paimon": ["csc"],
}

_TABLES: dict[tuple[str, str], list[str]] = {
    ("default_catalog", "dw"): ["orders", "users"],
    ("paimon", "csc"): ["ods_csc_log_audit"],
}

_COLUMNS: dict[tuple[str, str, str], list[dict[str, str | None]]] = {
    ("default_catalog", "dw", "orders"): [
        {"Field": "order_id", "Type": "bigint", "Null": "NO", "Key": "PRI", "Default": None, "Extra": ""},
        {"Field": "user_id", "Type": "bigint", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "amount", "Type": "decimal(18,2)", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "status", "Type": "varchar(32)", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "created_at", "Type": "datetime", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
    ],
    ("default_catalog", "dw", "users"): [
        {"Field": "user_id", "Type": "bigint", "Null": "NO", "Key": "PRI", "Default": None, "Extra": ""},
        {"Field": "name", "Type": "varchar(64)", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "created_at", "Type": "datetime", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
    ],
    ("paimon", "csc", "ods_csc_log_audit"): [
        {"Field": "app_id", "Type": "varchar(128)", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "year", "Type": "int", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "month", "Type": "int", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "day", "Type": "int", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "log_time", "Type": "datetime", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
        {"Field": "log_content", "Type": "string", "Null": "YES", "Key": "", "Default": None, "Extra": ""},
    ],
}

_CREATE_TABLE_DDL: dict[tuple[str, str, str], str] = {
    ("default_catalog", "dw", "orders"): (
        "CREATE TABLE `orders` (\n"
        "  `order_id` bigint NOT NULL,\n"
        "  `user_id` bigint NULL,\n"
        "  `amount` decimal(18,2) NULL,\n"
        "  `status` varchar(32) NULL,\n"
        "  `created_at` datetime NULL\n"
        ") ENGINE=OLAP\n"
        "PRIMARY KEY(`order_id`)\n"
        "PARTITION BY RANGE(`created_at`) ()\n"
        "DISTRIBUTED BY HASH(`order_id`) BUCKETS 16"
    ),
}

_SAMPLE_ROWS: dict[tuple[str, str, str], list[dict]] = {
    ("paimon", "csc", "ods_csc_log_audit"): [
        {
            "app_id": "hids-identify-result-storage-svc",
            "year": 2026,
            "month": 202606,
            "day": 20260610,
            "log_time": datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
            "log_content": "mock log content #1",
        },
        {
            "app_id": "hids-identify-result-storage-svc",
            "year": 2026,
            "month": 202606,
            "day": 20260610,
            "log_time": datetime(2026, 6, 10, 12, 5, 0, tzinfo=timezone.utc).isoformat(),
            "log_content": "mock log content #2",
        },
    ],
    ("default_catalog", "dw", "orders"): [
        {"order_id": 1001, "user_id": 1, "amount": "199.00", "status": "paid", "created_at": "2026-06-10 10:00:00"},
        {"order_id": 1002, "user_id": 2, "amount": "59.90", "status": "pending", "created_at": "2026-06-10 11:00:00"},
    ],
}


def _find_table_key(text: str) -> tuple[str, str, str] | None:
    """在 SQL 文本里找有没有出现某个已知的 mock 表（用于 SELECT / EXPLAIN 场景）。"""
    lowered = text.lower()
    for (catalog, database, table) in _COLUMNS:
        candidates = [
            f"{catalog}.{database}.{table}",
            f"{database}.{table}",
            table,
        ]
        for candidate in candidates:
            if candidate.lower() in lowered:
                return catalog, database, table
    return None


class MockConnectionPool(ConnectionPool):
    """按正则匹配常见的 SHOW/DESCRIBE/SELECT/INSERT 等语句，返回构造数据。"""

    def __init__(self, role: str = "read"):
        self.role = role

    def execute(self, sql: str) -> ExecResult:
        stripped = sql.strip().rstrip(";")
        upper = stripped.upper()

        if upper.startswith("SHOW CATALOGS"):
            return ExecResult(
                columns=["Catalog"],
                rows=[{"Catalog": c} for c in _CATALOGS],
                row_count=len(_CATALOGS),
            )

        m = re.match(r"SHOW\s+DATABASES(\s+FROM\s+(\w+))?", upper)
        if m:
            catalog = (m.group(2) or "default_catalog").lower()
            dbs = _DATABASES.get(catalog, [])
            return ExecResult(
                columns=["Database"],
                rows=[{"Database": d} for d in dbs],
                row_count=len(dbs),
            )

        m = re.match(r"SHOW\s+TABLES\s+FROM\s+([\w.]+)", upper)
        if m:
            ref = m.group(1).lower()
            parts = ref.split(".")
            catalog, database = (parts[0], parts[1]) if len(parts) == 2 else ("default_catalog", parts[0])
            tables = _TABLES.get((catalog, database), [])
            col_name = f"Tables_in_{database}"
            return ExecResult(
                columns=[col_name],
                rows=[{col_name: t} for t in tables],
                row_count=len(tables),
            )

        m = re.match(r"(DESCRIBE|DESC)\s+([\w.]+)", upper)
        if m:
            key = self._resolve_qualified_name(m.group(2))
            columns = _COLUMNS.get(key, [])
            if not columns:
                raise ValueError(f"表不存在: {m.group(2)}")
            return ExecResult(
                columns=["Field", "Type", "Null", "Key", "Default", "Extra"],
                rows=list(columns),
                row_count=len(columns),
            )

        m = re.match(r"SHOW\s+CREATE\s+TABLE\s+([\w.]+)", upper)
        if m:
            key = self._resolve_qualified_name(m.group(1))
            ddl = _CREATE_TABLE_DDL.get(key)
            table_name = m.group(1)
            if ddl is None:
                # 外部 catalog（如 paimon）通常没有 StarRocks 原生的 SHOW CREATE TABLE 语义
                ddl = f"-- {table_name} 来自外部 catalog，无 StarRocks 原生建表语句（分区/分桶信息不适用）"
            return ExecResult(
                columns=["Table", "Create Table"],
                rows=[{"Table": table_name, "Create Table": ddl}],
                row_count=1,
            )

        if upper.startswith("EXPLAIN"):
            return ExecResult(
                columns=["Explain String"],
                rows=[{"Explain String": f"MOCK PLAN for: {stripped[7:].strip()}"}],
                row_count=1,
            )

        if upper.startswith("SELECT"):
            return self._mock_select(stripped, upper)

        if upper.startswith(("INSERT", "UPDATE", "DELETE")):
            return ExecResult(columns=[], rows=[], row_count=0, affected_rows=1)

        raise ValueError(f"Mock 数据源不支持的语句: {stripped[:80]}")

    def _mock_select(self, stripped: str, upper: str) -> ExecResult:
        key = _find_table_key(stripped)
        limit_match = re.search(r"LIMIT\s+(\d+)", upper)
        limit = int(limit_match.group(1)) if limit_match else None

        if key is None:
            # 未知表：返回一个占位结果，方便调试而不是直接报错
            return ExecResult(columns=["result"], rows=[{"result": "mock: 未识别的表，返回空结果"}], row_count=0)

        rows = list(_SAMPLE_ROWS.get(key, []))
        if limit is not None:
            rows = rows[:limit]
        columns = list(rows[0].keys()) if rows else [c["Field"] for c in _COLUMNS.get(key, [])]
        return ExecResult(columns=columns, rows=rows, row_count=len(rows))

    @staticmethod
    def _resolve_qualified_name(ref: str) -> tuple[str, str, str]:
        parts = ref.lower().split(".")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return "default_catalog", parts[0], parts[1]
        raise ValueError(f"无法解析表名（需要 database.table 或 catalog.database.table 格式）: {ref}")

    def close(self) -> None:  # pragma: no cover - 无资源需要释放
        pass
