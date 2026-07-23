"""`get_database_info` 工具的核心逻辑：按 catalog -> database -> table 分层查询结构信息。

StarRocks 支持多 catalog（内部 `default_catalog` + 外部 catalog，比如接入 Paimon/Hive），
命名方式是三段式 `catalog.database.table`，这一点与 Trino 类似，因此这里不能照搬
"只有一个 database" 的单库模型：每一层都要求上一层的定位信息，且不同 catalog 的能力
不同（比如外部 catalog 通常没有 StarRocks 原生的分区/分桶概念）。
"""

from __future__ import annotations

from typing import Any

from ..auth.models import ApiKeyPermission
from ..db.base import ConnectionPool
from ..db.executor import PooledExecutor

DEFAULT_CATALOG = "default_catalog"


class SchemaAccessError(PermissionError):
    """apikey 无权访问所请求的 catalog/database。"""


def _extract_column(rows: list[dict[str, Any]], preferred_key_prefix: str | None = None) -> list[str]:
    """从 SHOW 系列语句的结果行里提取出唯一一列的值（列名在不同数据库/版本上可能不同）。"""
    values: list[str] = []
    for row in rows:
        if preferred_key_prefix is not None:
            matched = next((v for k, v in row.items() if k.lower().startswith(preferred_key_prefix.lower())), None)
            if matched is not None:
                values.append(matched)
                continue
        # 兜底：取这一行的第一个值
        if row:
            values.append(next(iter(row.values())))
    return values


async def get_database_info(
    *,
    pool: ConnectionPool,
    executor: PooledExecutor,
    permission: ApiKeyPermission,
    catalog: str | None,
    database: str | None,
    table: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    # 参数归一化：只给 database/table、没给 catalog 时，默认落在 StarRocks 内部 catalog，
    # 方便习惯了"单库"心智模型的调用方（比如从 MySQL 迁移过来的使用场景）。
    if catalog is None and (database is not None or table is not None):
        catalog = DEFAULT_CATALOG

    if table is not None and database is None:
        raise ValueError("指定 table 时必须同时指定 database（可选 catalog，缺省为 default_catalog）")

    # Level 0: 都不传 -> 列出当前 apikey 可见的所有 catalog
    if catalog is None:
        result = await executor.execute(pool, "SHOW CATALOGS", timeout_seconds)
        catalogs = _extract_column(result.rows, preferred_key_prefix="Catalog")
        visible = permission.visible_catalogs()
        if visible is not None:
            catalogs = [c for c in catalogs if c in visible]
        return {"level": "catalogs", "catalogs": catalogs}

    if not permission.allows_catalog(catalog):
        raise SchemaAccessError(f"apikey `{permission.key_id}` 无权访问 catalog `{catalog}`")

    # Level 1: 只给 catalog -> 列出该 catalog 下的 database
    if database is None:
        result = await executor.execute(pool, f"SHOW DATABASES FROM {catalog}", timeout_seconds)
        databases = _extract_column(result.rows, preferred_key_prefix="Database")
        if not permission.is_unrestricted:
            databases = [d for d in databases if permission.allows_reference(catalog, d)]
        return {"level": "databases", "catalog": catalog, "databases": databases}

    if not permission.allows_reference(catalog, database):
        raise SchemaAccessError(f"apikey `{permission.key_id}` 无权访问 `{catalog}.{database}`")

    # Level 2: 给了 catalog + database -> 列出该 database 下的 table
    if table is None:
        result = await executor.execute(pool, f"SHOW TABLES FROM {catalog}.{database}", timeout_seconds)
        tables = _extract_column(result.rows, preferred_key_prefix="Tables_in")
        return {"level": "tables", "catalog": catalog, "database": database, "tables": tables}

    # Level 3: 给了 catalog + database + table -> 列结构（+ 内部表的建表语句）
    qualified = f"{catalog}.{database}.{table}"
    result = await executor.execute(pool, f"DESCRIBE {qualified}", timeout_seconds)
    info: dict[str, Any] = {
        "level": "columns",
        "catalog": catalog,
        "database": database,
        "table": table,
        "columns": result.rows,
    }

    if catalog == DEFAULT_CATALOG:
        try:
            ddl_result = await executor.execute(pool, f"SHOW CREATE TABLE {qualified}", timeout_seconds)
            if ddl_result.rows:
                row = ddl_result.rows[0]
                info["create_table"] = row.get("Create Table") or next(iter(row.values()), None)
        except Exception:  # noqa: BLE001 - 拿不到建表语句不应该影响主流程
            pass
    else:
        info["note"] = (
            "该表来自外部 catalog，通常不具备 StarRocks 原生表的主键/分区/分桶等概念，"
            "如需理解数据切分方式请参考底层数据源（如 Hive/Paimon）自身的元数据"
        )

    return info
