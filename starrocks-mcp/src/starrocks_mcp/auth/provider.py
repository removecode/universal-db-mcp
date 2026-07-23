"""AuthProvider：把明文 apikey 解析为 ApiKeyPermission。

设计为可插拔接口，v1 提供基于本地 YAML 文件的实现。后续如果要接入真实的
用户/权限系统，只需要实现同样的 Protocol，不需要改动上层鉴权中间件和工具代码。

配置里同时维护「用户 <-> apikey」映射：请求带上 apikey 后，能反查出 username，
供审计日志直接记录用户名。
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


def _parse_scope(scope_raw: object) -> list[ScopeRule] | None:
    if scope_raw is None:
        return None
    if not isinstance(scope_raw, list):
        raise ValueError("scope 必须是列表或 null")
    return [
        ScopeRule(catalog=item["catalog"], database=item.get("database"))
        for item in scope_raw
    ]


def _build_permission(
    *,
    hashed_key: str,
    username: str,
    entry: dict,
) -> ApiKeyPermission:
    return ApiKeyPermission(
        username=username,
        key_id=entry.get("id") or username or hashed_key,
        role=entry.get("role", "read"),
        scope=_parse_scope(entry.get("scope")),
        rate_limit_per_min=entry.get("rate_limit_per_min"),
    )


class YamlAuthProvider:
    """从本地 YAML 文件加载「用户 <-> apikey」映射与权限。

    支持两种等价写法（可同时存在，后者覆盖同 hash 的前者）：

    1) 推荐：以用户为中心（users）
    ```yaml
    users:
      alice:
        api_key: "sha256:..."
        role: read
        scope: ...
    ```

    2) 兼容：以 apikey 哈希为中心（api_keys）
    ```yaml
    api_keys:
      "sha256:...":
        username: alice
        id: bi-team-readonly
        role: read
    ```

    文件中只存 apikey 的 sha256 摘要，不落地明文密钥。
    支持按文件 mtime 热重载。
    """

    def __init__(self, apikeys_file: str | Path):
        self._path = Path(apikeys_file)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._permissions: dict[str, ApiKeyPermission] = {}
        # username -> hashed api key，方便运维侧按用户反查
        self._username_to_hash: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("apikeys 配置文件不存在: %s，当前没有任何可用的 apikey", self._path)
            self._permissions = {}
            self._username_to_hash = {}
            self._mtime = None
            return

        with self._path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        permissions: dict[str, ApiKeyPermission] = {}
        username_to_hash: dict[str, str] = {}

        # 1) users: 用户 -> apikey
        users = raw.get("users", {}) or {}
        for username, entry in users.items():
            if not entry:
                continue
            hashed_key = entry.get("api_key") or entry.get("api_key_hash")
            if not hashed_key:
                logger.warning("用户 %s 缺少 api_key / api_key_hash，已跳过", username)
                continue
            if not str(hashed_key).startswith("sha256:"):
                logger.warning(
                    "用户 %s 的 api_key 不是 sha256:<hex> 形式（疑似明文），已跳过", username
                )
                continue
            perm = _build_permission(
                hashed_key=hashed_key,
                username=str(username),
                entry=entry,
            )
            permissions[hashed_key] = perm
            username_to_hash[str(username)] = hashed_key

        # 2) api_keys: apikey hash -> 权限（可带 username）
        entries = raw.get("api_keys", {}) or {}
        for hashed_key, entry in entries.items():
            if not entry:
                continue
            username = entry.get("username") or entry.get("user") or entry.get("id") or hashed_key
            perm = _build_permission(
                hashed_key=hashed_key,
                username=str(username),
                entry=entry,
            )
            permissions[hashed_key] = perm
            username_to_hash[str(username)] = hashed_key

        self._permissions = permissions
        self._username_to_hash = username_to_hash
        self._mtime = self._path.stat().st_mtime
        logger.info(
            "已加载 %d 个用户/apikey 映射 (%s)",
            len(permissions),
            self._path,
        )

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

    def get_api_key_hash_by_username(self, username: str) -> str | None:
        """按用户名反查对应的 apikey 哈希（不含明文）。"""
        self._maybe_reload()
        return self._username_to_hash.get(username)
