"""加载并解析 settings.yaml 配置文件。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: object) -> object:
    """递归替换字符串中的 ${ENV_VAR} 占位符。"""
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            return os.environ.get(name, "")

        return _ENV_VAR_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
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


def load_settings(config_path: str | Path) -> Settings:
    """从 YAML 文件加载配置，缺失字段使用默认值，字符串值支持 ${ENV_VAR} 占位符。"""
    path = Path(config_path)
    raw: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    raw = _interpolate_env(raw)

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
    )

    security = SecuritySettings(
        default_row_limit=int(security_raw.get("default_row_limit", 1000)),
        max_row_limit=int(security_raw.get("max_row_limit", 10000)),
        query_timeout_seconds=float(security_raw.get("query_timeout_seconds", 30.0)),
        blocked_keywords=list(security_raw.get("blocked_keywords", SecuritySettings().blocked_keywords)),
        fail_closed_on_unresolvable_scope=bool(security_raw.get("fail_closed_on_unresolvable_scope", True)),
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

    base_dir = path.parent.resolve() if path.exists() else Path.cwd()
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
