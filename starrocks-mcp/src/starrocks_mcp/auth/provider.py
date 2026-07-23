"""AuthProvider：把明文 apikey 解析为 ApiKeyPermission。

设计为可插拔接口，v1 提供基于本地 YAML 文件的实现。后续如果要接入真实的
用户/权限系统，只需要实现同样的 Protocol，不需要改动上层鉴权中间件和工具代码。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Protocol

import yaml

from .models import ApiKeyPermission, ScopeRule

logger = logging.getLogger(__name__)


def hash_api_key(raw_api_key: str) -> str:
    """把明文 apikey 转成配置文件里使用的 "sha256:<hex>" 形式。"""
    digest = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class AuthProvider(Protocol):
    """apikey -> 权限 的解析接口。"""

    def get_permission(self, raw_api_key: str) -> ApiKeyPermission | None:
        """返回 apikey 对应的权限；apikey 不存在或已失效时返回 None。"""
        ...


class YamlAuthProvider:
    """从本地 YAML 文件加载 apikey -> 权限 映射。

    文件内容示例见 config/apikeys.example.yaml。为了避免明文 apikey 落盘，
    文件中的 key 一律是 apikey 的 sha256 摘要（"sha256:<hex>"），本类在收到
    客户端传入的明文 apikey 时会先做同样的哈希，再去查表。

    支持简单的“热重载”：每次调用 get_permission 时检查文件 mtime，变化了就
    重新加载，避免每次请求都读盘（未变化时直接用内存缓存）。
    """

    def __init__(self, apikeys_file: str | Path):
        self._path = Path(apikeys_file)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._permissions: dict[str, ApiKeyPermission] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("apikeys 配置文件不存在: %s，当前没有任何可用的 apikey", self._path)
            self._permissions = {}
            self._mtime = None
            return

        with self._path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        entries = raw.get("api_keys", {}) or {}
        permissions: dict[str, ApiKeyPermission] = {}
        for hashed_key, entry in entries.items():
            if not entry:
                continue
            scope_raw = entry.get("scope")
            scope: list[ScopeRule] | None
            if scope_raw is None:
                scope = None
            else:
                scope = [
                    ScopeRule(catalog=item["catalog"], database=item.get("database"))
                    for item in scope_raw
                ]

            permissions[hashed_key] = ApiKeyPermission(
                key_id=entry.get("id", hashed_key),
                role=entry.get("role", "read"),
                scope=scope,
                rate_limit_per_min=entry.get("rate_limit_per_min"),
            )

        self._permissions = permissions
        self._mtime = self._path.stat().st_mtime
        logger.info("已加载 %d 个 apikey 权限配置 (%s)", len(permissions), self._path)

    def _maybe_reload(self) -> None:
        if not self._path.exists():
            return
        current_mtime = self._path.stat().st_mtime
        if current_mtime != self._mtime:
            with self._lock:
                if self._path.stat().st_mtime != self._mtime:
                    self._load()

    def get_permission(self, raw_api_key: str) -> ApiKeyPermission | None:
        if not raw_api_key:
            return None
        self._maybe_reload()
        hashed = hash_api_key(raw_api_key)
        return self._permissions.get(hashed)
