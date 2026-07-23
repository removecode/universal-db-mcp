"""ApiKey 权限相关的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["read", "readwrite"]


@dataclass(frozen=True)
class ScopeRule:
    """限制某个 apikey 可以访问的 catalog（可选进一步限制到某个 database）。"""

    catalog: str
    database: str | None = None

    def allows(self, catalog: str, database: str | None) -> bool:
        if self.catalog.lower() != catalog.lower():
            return False
        if self.database is None:
            return True
        if database is None:
            # scope 要求限定到具体 database，但待校验的引用没有给出 database，
            # 视为无法判断，交给调用方按 fail-closed 策略处理
            return False
        return self.database.lower() == database.lower()


@dataclass(frozen=True)
class ApiKeyPermission:
    """一个 apikey 解析后的权限信息（同时承载「用户 <-> apikey」映射）。"""

    username: str
    """业务用户名。审计日志优先记录这个字段。"""

    key_id: str
    """apikey 自身的标识（可与 username 相同，也可单独命名）。"""

    role: Role
    scope: list[ScopeRule] | None = None
    rate_limit_per_min: int | None = None

    @property
    def is_unrestricted(self) -> bool:
        return self.scope is None

    def allows_reference(self, catalog: str, database: str | None) -> bool:
        """检查某个 catalog(.database) 引用是否在该 apikey 的可访问范围内。"""
        if self.scope is None:
            return True
        return any(rule.allows(catalog, database) for rule in self.scope)

    def allows_catalog(self, catalog: str) -> bool:
        """检查该 apikey 是否有任意权限触及这个 catalog（不关心具体 database）。"""
        if self.scope is None:
            return True
        return any(rule.catalog.lower() == catalog.lower() for rule in self.scope)

    def visible_catalogs(self) -> set[str] | None:
        """返回该 apikey scope 中出现过的所有 catalog；None 表示不限制。"""
        if self.scope is None:
            return None
        return {rule.catalog for rule in self.scope}
