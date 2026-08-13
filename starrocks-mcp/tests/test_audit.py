"""SQL 安全审计测试：语句分类、角色白名单、LIMIT 保护、范围校验、多语句拒绝。"""

from __future__ import annotations

import pytest

from starrocks_mcp.auth.models import ApiKeyPermission, ScopeRule
from starrocks_mcp.config import SecuritySettings
from starrocks_mcp.security.audit import SqlAuditError, audit_sql, classify_statement

SETTINGS = SecuritySettings()

READ_PERM = ApiKeyPermission(
    username="alice", key_id="ro", role="read", scope=[ScopeRule(catalog="paimon", database="csc")]
)
READWRITE_PERM_UNRESTRICTED = ApiKeyPermission(username="bob", key_id="rw", role="readwrite", scope=None)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT"),
        ("select * from t", "SELECT"),
        ("SHOW CATALOGS", "SHOW"),
        ("SHOW DATABASES FROM paimon", "SHOW"),
        ("DESCRIBE dw.orders", "DESCRIBE"),
        ("DESC dw.orders", "DESCRIBE"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET a=1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("DROP TABLE t", "DROP"),
        ("TRUNCATE TABLE t", "TRUNCATE"),
        ("ALTER TABLE t ADD COLUMN a INT", "ALTER"),
        ("GRANT SELECT ON t TO u", "GRANT"),
    ],
)
def test_classify_statement(sql, expected):
    assert classify_statement(sql) == expected


def test_select_without_limit_gets_default_injected():
    result = audit_sql("SELECT * FROM paimon.csc.ods_csc_log_audit WHERE app_id='x'", READ_PERM, SETTINGS)
    assert result.statement_type == "SELECT"
    assert result.pool == "read"
    assert result.limit_source == "default_injected"
    assert result.limit_applied == SETTINGS.default_row_limit
    assert f"LIMIT {SETTINGS.default_row_limit}" in result.sql_to_execute


def test_select_with_small_limit_kept_as_is():
    result = audit_sql("SELECT * FROM paimon.csc.ods_csc_log_audit LIMIT 5", READ_PERM, SETTINGS)
    assert result.limit_source == "user"
    assert result.limit_applied == 5
    assert not result.truncated
    assert result.sql_to_execute.rstrip().endswith("LIMIT 5")


def test_select_with_huge_limit_gets_capped():
    result = audit_sql("SELECT * FROM paimon.csc.ods_csc_log_audit LIMIT 999999", READ_PERM, SETTINGS)
    assert result.limit_source == "capped"
    assert result.limit_applied == SETTINGS.max_row_limit
    assert result.truncated is True
    assert f"LIMIT {SETTINGS.max_row_limit}" in result.sql_to_execute


def test_read_role_cannot_write():
    with pytest.raises(SqlAuditError):
        audit_sql("INSERT INTO paimon.csc.ods_csc_log_audit VALUES (1)", READ_PERM, SETTINGS)


def test_readwrite_role_can_write_and_uses_write_pool():
    result = audit_sql("UPDATE dw.orders SET status='paid' WHERE order_id=1", READWRITE_PERM_UNRESTRICTED, SETTINGS)
    assert result.pool == "write"
    assert result.statement_type == "UPDATE"


def test_ddl_always_blocked_even_for_readwrite():
    with pytest.raises(SqlAuditError):
        audit_sql("DROP TABLE dw.orders", READWRITE_PERM_UNRESTRICTED, SETTINGS)


def test_multi_statement_rejected():
    with pytest.raises(SqlAuditError):
        audit_sql("SELECT 1; SELECT 2", READWRITE_PERM_UNRESTRICTED, SETTINGS)


def test_out_of_scope_table_rejected():
    with pytest.raises(SqlAuditError):
        audit_sql("SELECT * FROM other_catalog.dw.orders", READ_PERM, SETTINGS)


def test_in_scope_table_allowed():
    result = audit_sql("SELECT * FROM paimon.csc.ods_csc_log_audit LIMIT 1", READ_PERM, SETTINGS)
    assert result.statement_type == "SELECT"


def test_unresolvable_scope_fails_closed_by_default():
    # 只有表名，没有 database，无法确定是否在 scope 内 -> 按配置 fail-closed 拒绝
    with pytest.raises(SqlAuditError):
        audit_sql("SELECT * FROM ods_csc_log_audit", READ_PERM, SETTINGS)


def test_unresolvable_scope_allowed_when_fail_closed_disabled():
    lenient_settings = SecuritySettings(fail_closed_on_unresolvable_scope=False)
    result = audit_sql("SELECT * FROM ods_csc_log_audit", READ_PERM, lenient_settings)
    assert result.statement_type == "SELECT"


def test_unrestricted_key_bypasses_scope_check():
    result = audit_sql("SELECT * FROM anything.anything.anything LIMIT 1", READWRITE_PERM_UNRESTRICTED, SETTINGS)
    assert result.statement_type == "SELECT"


def test_empty_sql_rejected():
    with pytest.raises(SqlAuditError):
        audit_sql("   ", READWRITE_PERM_UNRESTRICTED, SETTINGS)


# ---------------------------------------------------------------------------
# LIMIT 注入的改写正确性
#
# 以下四种写法都曾让 LIMIT 保护失效：前两种拼出语法错误，后两种更危险——
# 查询实际无上限全表扫，返回给调用方的 limit_applied 却仍是 1000。
# ---------------------------------------------------------------------------

NO_LIMIT = 1000


def test_trailing_semicolon_does_not_produce_broken_sql():
    result = audit_sql("SELECT str2 FROM paimon.csc.t WHERE day=20260803;", READ_PERM, SETTINGS)
    assert "; LIMIT" not in result.sql_to_execute
    assert ";" not in result.sql_to_execute
    assert result.sql_to_execute.rstrip().endswith(f"LIMIT {NO_LIMIT}")


def test_trailing_line_comment_cannot_swallow_injected_limit():
    result = audit_sql("SELECT str2 FROM paimon.csc.t -- 查一下\n", READ_PERM, SETTINGS)
    lines = result.sql_to_execute.splitlines()
    # LIMIT 必须落在注释所在行之后，否则整条被注释掉
    assert lines[-1].strip() == f"LIMIT {NO_LIMIT}"
    assert result.limit_applied == NO_LIMIT


def test_limit_inside_string_literal_is_not_mistaken_for_user_limit():
    sql = "SELECT str2 FROM paimon.csc.t WHERE log_content LIKE '%LIMIT 10%'"
    result = audit_sql(sql, READ_PERM, SETTINGS)
    assert result.limit_source == "default_injected"
    assert result.limit_applied == NO_LIMIT
    assert result.sql_to_execute.rstrip().endswith(f"LIMIT {NO_LIMIT}")


def test_unbalanced_paren_inside_string_does_not_double_inject_limit():
    sql = "SELECT str2 FROM paimon.csc.t WHERE str1 = '(' LIMIT 5"
    result = audit_sql(sql, READ_PERM, SETTINGS)
    assert result.limit_source == "user"
    assert result.limit_applied == 5
    assert result.sql_to_execute.upper().count("LIMIT") == 1


def test_limit_in_subquery_does_not_count_as_top_level():
    sql = "SELECT a FROM (SELECT a FROM paimon.csc.t LIMIT 3) x"
    result = audit_sql(sql, READ_PERM, SETTINGS)
    assert result.limit_source == "default_injected"
    assert result.sql_to_execute.rstrip().endswith(f"LIMIT {NO_LIMIT}")


def test_table_reference_inside_string_literal_is_ignored():
    # 字符串里的 from 不是真实表引用，不应该因此触发 fail-closed 拒绝
    sql = "SELECT str2 FROM paimon.csc.t WHERE log_content LIKE '%from other_db.x%' LIMIT 1"
    result = audit_sql(sql, READ_PERM, SETTINGS)
    assert result.statement_type == "SELECT"


# ---------------------------------------------------------------------------
# 数据库侧超时 hint
# ---------------------------------------------------------------------------


def test_select_gets_query_timeout_hint_below_wall_clock_budget():
    result = audit_sql(
        "SELECT * FROM paimon.csc.t LIMIT 1", READ_PERM, SETTINGS, query_timeout_seconds=60
    )
    # 数据库侧要比客户端墙钟先到期，这样连接能被及时归还
    assert result.query_timeout_hint == 55
    assert result.sql_to_execute.startswith("SELECT /*+ SET_VAR(query_timeout=55) */")


def test_existing_hint_is_not_overwritten():
    sql = "SELECT /*+ SET_VAR(query_timeout=7) */ * FROM paimon.csc.t LIMIT 1"
    result = audit_sql(sql, READ_PERM, SETTINGS)
    assert result.query_timeout_hint is None
    assert result.sql_to_execute.count("SET_VAR") == 1


def test_hint_is_skipped_for_cte_which_cannot_carry_it():
    sql = "WITH x AS (SELECT a FROM paimon.csc.t) SELECT a FROM x LIMIT 1"
    result = audit_sql(sql, READWRITE_PERM_UNRESTRICTED, SETTINGS)
    assert result.query_timeout_hint is None
    assert "SET_VAR" not in result.sql_to_execute


def test_hint_can_be_disabled():
    no_hint = SecuritySettings(inject_query_timeout_hint=False)
    result = audit_sql("SELECT * FROM paimon.csc.t LIMIT 1", READ_PERM, no_hint)
    assert result.query_timeout_hint is None
    assert "SET_VAR" not in result.sql_to_execute
