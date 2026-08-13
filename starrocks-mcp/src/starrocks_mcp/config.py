"""加载并解析 settings.yaml 配置文件。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: object, missing: set[str]) -> object:
    """递归替换字符串中的 ${ENV_VAR} 占位符，未设置的变量名收集进 `missing`。"""
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                missing.add(name)
                return ""
            return os.environ[name]

        return _ENV_VAR_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v, missing) for v in value]
    return value


@dataclass
class PoolSettings:
    mincached: int = 1
    maxcached: int = 5
    maxconnections: int = 10


@dataclass
class DbAccountSettings:
    user: str = ""
    password: str = ""


@dataclass
class DatabaseSettings:
    driver: str = "mock"
    host: str = "127.0.0.1"
    port: int = 9030
    charset: str = "utf8"
    read_account: DbAccountSettings = field(default_factory=DbAccountSettings)
    write_account: DbAccountSettings = field(default_factory=DbAccountSettings)
    read_pool: PoolSettings = field(default_factory=PoolSettings)
    write_pool: PoolSettings = field(default_factory=lambda: PoolSettings(mincached=1, maxcached=3, maxconnections=5))
    executor_max_workers: int = 32
    health_executor_max_workers: int = 4
    connect_timeout: float = 10.0
    # socket 读写超时；None 表示按 security.query_timeout_seconds + margin 自动推导。
    # 它是连接能被归还的最后一道保障，必须大于查询超时预算，否则正常的慢查询会被误杀。
    socket_timeout: float | None = None
    socket_timeout_margin_seconds: float = 30.0
    # 取不到空闲连接时的最长等待；超过就报“池满”，而不是无限期挂起
    pool_acquire_timeout_seconds: float = 5.0


@dataclass
class SecuritySettings:
    default_row_limit: int = 1000
    max_row_limit: int = 10000
    query_timeout_seconds: float = 30.0
    blocked_keywords: list[str] = field(default_factory=lambda: [
        "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE",
        "LOAD", "RENAME", "KILL", "SET",
    ])
    fail_closed_on_unresolvable_scope: bool = True
    # 给 SELECT 注入 /*+ SET_VAR(query_timeout=N) */，让 StarRocks 侧也会主动终止查询
    inject_query_timeout_hint: bool = True


@dataclass
class MonitoringSettings:
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    audit_db_path: str = "logs/audit.db"
    sql_text_max_length: int = 2000


@dataclass
class AuthSettings:
    provider: str = "yaml"
    apikeys_file: str = "config/apikeys.yaml"
    header_name: str = "X-API-Key"


@dataclass
class ServerSettings:
    host: str = "0.0.0.0"
    port: int = 8080
    transport: str = "streamable-http"
    stateless_http: bool = False


@dataclass
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)

    # 配置文件所在目录，用于把相对路径（apikeys_file、audit_db_path）解析成绝对路径
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve_path(self, path_str: str) -> Path:
        path = Path(path_str)
        if path.is_absolute():
            return path
        return self.base_dir / path

    def effective_socket_timeout(self) -> float:
        """socket 读写超时：显式配置优先，否则取查询超时预算加一段余量。"""
        if self.database.socket_timeout is not None:
            return self.database.socket_timeout
        return self.security.query_timeout_seconds + self.database.socket_timeout_margin_seconds


def load_settings(config_path: str | Path) -> Settings:
    """从 YAML 文件加载配置，缺失字段使用默认值，字符串值支持 ${ENV_VAR} 占位符。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}。不要依赖默认值启动——默认 driver=mock 会返回构造数据，"
            "服务看起来一切正常，实际根本没连数据库。"
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    missing_env: set[str] = set()
    raw = _interpolate_env(raw, missing_env)
    if missing_env:
        raise ValueError(
            f"配置文件 {path} 引用的环境变量未设置: {', '.join(sorted(missing_env))}。"
            "请先导出后再启动——否则占位符会被静默替换成空字符串，表现为莫名其妙的认证失败。"
        )

    server_raw = raw.get("server", {}) or {}
    db_raw = raw.get("database", {}) or {}
    security_raw = raw.get("security", {}) or {}
    monitoring_raw = raw.get("monitoring", {}) or {}
    auth_raw = raw.get("auth", {}) or {}

    pool_raw = db_raw.get("pool", {}) or {}
    read_pool_raw = pool_raw.get("read", {}) or {}
    write_pool_raw = pool_raw.get("write", {}) or {}

    database = DatabaseSettings(
        driver=db_raw.get("driver", "mock"),
        host=db_raw.get("host", "127.0.0.1"),
        port=int(db_raw.get("port", 9030)),
        charset=db_raw.get("charset", "utf8"),
        read_account=DbAccountSettings(**(db_raw.get("read_account", {}) or {})),
        write_account=DbAccountSettings(**(db_raw.get("write_account", {}) or {})),
        read_pool=PoolSettings(**read_pool_raw) if read_pool_raw else PoolSettings(),
        write_pool=PoolSettings(**write_pool_raw) if write_pool_raw else PoolSettings(
            mincached=1, maxcached=3, maxconnections=5
        ),
        executor_max_workers=int(db_raw.get("executor_max_workers", 32)),
        health_executor_max_workers=int(db_raw.get("health_executor_max_workers", 4)),
        connect_timeout=float(db_raw.get("connect_timeout", 10.0)),
        socket_timeout=(
            None if db_raw.get("socket_timeout") is None else float(db_raw["socket_timeout"])
        ),
        socket_timeout_margin_seconds=float(db_raw.get("socket_timeout_margin_seconds", 30.0)),
        pool_acquire_timeout_seconds=float(db_raw.get("pool_acquire_timeout_seconds", 5.0)),
    )

    security = SecuritySettings(
        default_row_limit=int(security_raw.get("default_row_limit", 1000)),
        max_row_limit=int(security_raw.get("max_row_limit", 10000)),
        query_timeout_seconds=float(security_raw.get("query_timeout_seconds", 30.0)),
        blocked_keywords=list(security_raw.get("blocked_keywords", SecuritySettings().blocked_keywords)),
        fail_closed_on_unresolvable_scope=bool(security_raw.get("fail_closed_on_unresolvable_scope", True)),
        inject_query_timeout_hint=bool(security_raw.get("inject_query_timeout_hint", True)),
    )

    monitoring = MonitoringSettings(
        metrics_enabled=bool(monitoring_raw.get("metrics_enabled", True)),
        metrics_path=monitoring_raw.get("metrics_path", "/metrics"),
        audit_db_path=monitoring_raw.get("audit_db_path", "logs/audit.db"),
        sql_text_max_length=int(monitoring_raw.get("sql_text_max_length", 2000)),
    )

    auth = AuthSettings(
        provider=auth_raw.get("provider", "yaml"),
        apikeys_file=auth_raw.get("apikeys_file", "config/apikeys.yaml"),
        header_name=auth_raw.get("header_name", "X-API-Key"),
    )

    server = ServerSettings(
        host=server_raw.get("host", "0.0.0.0"),
        port=int(server_raw.get("port", 8080)),
        transport=server_raw.get("transport", "streamable-http"),
        stateless_http=bool(server_raw.get("stateless_http", False)),
    )

    base_dir = path.parent.resolve()
    # settings 常放在 config/ 目录下，相对路径应该相对项目根目录，而不是 config/ 目录
    if base_dir.name == "config":
        base_dir = base_dir.parent

    return Settings(
        server=server,
        database=database,
        security=security,
        monitoring=monitoring,
        auth=auth,
        base_dir=base_dir,
    )
