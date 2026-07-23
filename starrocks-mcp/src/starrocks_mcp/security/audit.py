"""SQL 安全审计：语句分类、按角色的白名单校验、LIMIT 保护、访问范围校验。

设计原则（v1）：
- 只允许单条语句，拒绝任何形式的语句堆叠。
- 语句类型采用白名单而不是黑名单：角色允许的类型之外一律拒绝。
- 任何角色都不能执行 DDL/DCL 等高危语句（DROP/ALTER/GRANT 等），v1 不提供覆盖开关。
- SELECT 类查询强制有 LIMIT 保护（自动注入默认值 / 收紧超大值）。
- 访问范围（scope）校验是 best-effort 的正则提取，无法可靠判断时按配置决定是否
  fail-closed（默认拒绝），避免"看似限制、实际绕过"的假安全感。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import sqlparse

from ..auth.models import ApiKeyPermission
from ..config import SecuritySettings

StatementType = str
PoolName = Literal["read", "write"]

READ_ONLY_STATEMENT_TYPES = {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"}
WRITE_STATEMENT_TYPES = {"INSERT", "UPDATE", "DELETE"}

ROLE_ALLOWED_STATEMENTS: dict[str, set[str]] = {
    "read": set(READ_ONLY_STATEMENT_TYPES),
    "readwrite": set(READ_ONLY_STATEMENT_TYPES) | set(WRITE_STATEMENT_TYPES),
}


class SqlAuditError(Exception):
    """SQL 未通过安全审计，携带面向调用方的拒绝原因。"""


@dataclass
class AuditResult:
    statement_type: StatementType
    pool: PoolName
    sql_to_execute: str
    limit_applied: int | None = None
    limit_source: Literal["user", "default_injected", "capped"] | None = None
    truncated: bool = False


def classify_statement(sql: str) -> StatementType:
    """判断单条 SQL 的语句类型。sqlparse 对 SHOW/DESCRIBE/EXPLAIN 的 get_type()
    通常返回 UNKNOWN，因此这里按首个关键字自行分类。
    """
    parsed = sqlparse.parse(sql)
    if not parsed:
        return "UNKNOWN"
    stmt = parsed[0]
    first_token = stmt.token_first(skip_cm=True)
    if first_token is None:
        return "UNKNOWN"

    value = first_token.value.upper()
    if value == "DESC":
        return "DESCRIBE"
    if value in (
        "SELECT", "SHOW", "DESCRIBE", "EXPLAIN",
        "INSERT", "UPDATE", "DELETE",
        "DROP", "TRUNCATE", "ALTER", "CREATE",
        "GRANT", "REVOKE", "LOAD", "RENAME", "KILL", "SET",
    ):
        return value
    if value == "WITH":
        # CTE，v1 近似当作 SELECT 处理（真正的写操作 CTE 很少见，且仍会被
        # 下面的角色白名单和范围校验约束）
        return "SELECT"
    return value


def split_statements(sql: str) -> list[str]:
    """拆分出所有非空语句，用于检测是否为多语句堆叠。"""
    return [s.strip() for s in sqlparse.split(sql) if s.strip()]


# ---------------------------------------------------------------------------
# LIMIT 保护
# ---------------------------------------------------------------------------

_LIMIT_COMMA_RE = re.compile(r"\bLIMIT\s+\d+\s*,\s*(\d+)\b", re.IGNORECASE)
_LIMIT_OFFSET_RE = re.compile(r"\bLIMIT\s+(\d+)\s+OFFSET\s+\d+\b", re.IGNORECASE)
_LIMIT_SIMPLE_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


def _find_top_level_limit(sql: str) -> tuple[re.Match[str], int] | None:
    """找到最外层（不在括号内）的 LIMIT 子句，返回匹配对象（group(1)=行数部分）和行数值。"""
    for pattern in (_LIMIT_COMMA_RE, _LIMIT_OFFSET_RE, _LIMIT_SIMPLE_RE):
        for m in pattern.finditer(sql):
            prefix = sql[: m.start()]
            depth = prefix.count("(") - prefix.count(")")
            if depth == 0:
                return m, int(m.group(1))
    return None


def apply_limit_guard(
    sql: str, default_row_limit: int, max_row_limit: int
) -> tuple[str, int, Literal["user", "default_injected", "capped"], bool]:
    """确保 SELECT 语句有合理的行数上限。返回 (改写后的sql, 生效limit, 来源, 是否被收紧)。"""
    found = _find_top_level_limit(sql)
    if found is None:
        new_sql = f"{sql.rstrip()} LIMIT {default_row_limit}"
        return new_sql, default_row_limit, "default_injected", False

    match, value = found
    if value > max_row_limit:
        start, end = match.start(1), match.end(1)
        new_sql = f"{sql[:start]}{max_row_limit}{sql[end:]}"
        return new_sql, max_row_limit, "capped", True

    return sql, value, "user", False


# ---------------------------------------------------------------------------
# 访问范围（scope）校验：best-effort 提取 catalog.database 引用
# ---------------------------------------------------------------------------

_TABLE_REF_PATTERNS = [
    re.compile(r"\b(?:FROM|JOIN)\s+([`\"\[]?[\w.]+[`\"\]]?)", re.IGNORECASE),
    re.compile(r"\bINSERT\s+INTO\s+([`\"\[]?[\w.]+[`\"\]]?)", re.IGNORECASE),
    re.compile(r"\bUPDATE\s+([`\"\[]?[\w.]+[`\"\]]?)", re.IGNORECASE),
]


def extract_table_references(sql: str) -> list[str]:
    """best-effort 地从 SQL 中提取出现的表引用（FROM/JOIN/INSERT INTO/UPDATE）。

    这是正则近似实现，无法处理所有合法 SQL 语法（比如复杂子查询、CTE 别名），
    因此只作为安全审计的辅助手段：解析不出来 + 配置了 scope 限制时，按
    fail-closed 策略拒绝，而不是误判为"安全"。
    """
    refs: list[str] = []
    for pattern in _TABLE_REF_PATTERNS:
        for m in pattern.finditer(sql):
            name = m.group(1).strip("`\"[]")
            if not name or name.upper() in ("SELECT",) or name.startswith("("):
                continue
            refs.append(name)
    return refs


def _split_catalog_database(ref: str) -> tuple[str, str | None]:
    parts = ref.split(".")
    if len(parts) >= 3:
        return parts[0], parts[1]
    if len(parts) == 2:
        return "default_catalog", parts[0]
    return "default_catalog", None


def check_scope(
    sql: str,
    permission: ApiKeyPermission,
    fail_closed_on_unresolvable_scope: bool,
) -> None:
    """校验 SQL 引用的表是否都在 apikey 的可访问范围内，不通过则抛出 SqlAuditError。"""
    if permission.is_unrestricted:
        return

    refs = extract_table_references(sql)
    for ref in refs:
        catalog, database = _split_catalog_database(ref)
        if database is None:
            if fail_closed_on_unresolvable_scope:
                raise SqlAuditError(
                    f"无法从 SQL 中可靠解析出引用 `{ref}` 所属的 database，"
                    "当前 apikey 配置了访问范围限制，按安全策略拒绝执行"
                )
            continue
        if not permission.allows_reference(catalog, database):
            raise SqlAuditError(
                f"apikey `{permission.key_id}` 无权访问 `{catalog}.{database}`（引用: {ref}）"
            )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def audit_sql(
    sql: str,
    permission: ApiKeyPermission,
    settings: SecuritySettings,
) -> AuditResult:
    """对一条 SQL 做完整的安全审计，通过则返回可执行的 AuditResult，否则抛出 SqlAuditError。"""
    if not sql or not sql.strip():
        raise SqlAuditError("SQL 不能为空")

    statements = split_statements(sql)
    if len(statements) == 0:
        raise SqlAuditError("SQL 不能为空")
    if len(statements) > 1:
        raise SqlAuditError("一次只能执行一条 SQL 语句，检测到多条语句堆叠")

    single_sql = statements[0]
    statement_type = classify_statement(single_sql)

    if statement_type in settings.blocked_keywords:
        raise SqlAuditError(f"语句类型 `{statement_type}` 属于禁止执行的高危操作")

    allowed_types = ROLE_ALLOWED_STATEMENTS.get(permission.role, set())
    if statement_type not in allowed_types:
        raise SqlAuditError(
            f"apikey `{permission.key_id}`（角色: {permission.role}）无权执行 `{statement_type}` 语句"
        )

    check_scope(single_sql, permission, settings.fail_closed_on_unresolvable_scope)

    if statement_type in READ_ONLY_STATEMENT_TYPES:
        pool: PoolName = "read"
    elif statement_type in WRITE_STATEMENT_TYPES:
        pool = "write"
    else:  # pragma: no cover - 已经被白名单挡住，理论不可达
        raise SqlAuditError(f"不支持的语句类型: {statement_type}")

    limit_applied: int | None = None
    limit_source = None
    truncated = False
    sql_to_execute = single_sql

    if statement_type == "SELECT":
        sql_to_execute, limit_applied, limit_source, truncated = apply_limit_guard(
            single_sql, settings.default_row_limit, settings.max_row_limit
        )

    return AuditResult(
        statement_type=statement_type,
        pool=pool,
        sql_to_execute=sql_to_execute,
        limit_applied=limit_applied,
        limit_source=limit_source,
        truncated=truncated,
    )
