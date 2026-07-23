"""AuthProvider（YAML 实现）测试：命中/未命中、scope 解析、热重载。"""

from __future__ import annotations

import os
import time

import yaml

from starrocks_mcp.auth.provider import YamlAuthProvider

from .conftest import READONLY_KEY, READWRITE_KEY, hash_key


def test_readonly_key_resolves_with_scope(auth_provider):
    perm = auth_provider.get_permission(READONLY_KEY)
    assert perm is not None
    assert perm.role == "read"
    assert perm.key_id == "bi-team-readonly"
    assert not perm.is_unrestricted
    assert perm.allows_reference("paimon", "csc")
    assert not perm.allows_reference("paimon", "other_db")
    assert not perm.allows_reference("other_catalog", "csc")


def test_readwrite_key_is_unrestricted(auth_provider):
    perm = auth_provider.get_permission(READWRITE_KEY)
    assert perm is not None
    assert perm.role == "readwrite"
    assert perm.is_unrestricted
    assert perm.allows_reference("anything", "anything")


def test_unknown_key_returns_none(auth_provider):
    assert auth_provider.get_permission("this-key-does-not-exist") is None


def test_empty_key_returns_none(auth_provider):
    assert auth_provider.get_permission("") is None


def test_missing_file_returns_none_for_everything(tmp_path):
    provider = YamlAuthProvider(tmp_path / "does-not-exist.yaml")
    assert provider.get_permission(READONLY_KEY) is None


def test_hot_reload_picks_up_changes(apikeys_file, auth_provider):
    assert auth_provider.get_permission(READONLY_KEY) is not None

    # 确保 mtime 会发生变化（部分文件系统 mtime 精度是秒级）
    time.sleep(1.01)
    apikeys_file.write_text(yaml.safe_dump({"api_keys": {}}), encoding="utf-8")
    os.utime(apikeys_file, None)

    assert auth_provider.get_permission(READONLY_KEY) is None


def test_hash_key_is_stable():
    assert hash_key("abc") == hash_key("abc")
    assert hash_key("abc") != hash_key("abd")
    assert hash_key("abc").startswith("sha256:")
