"""配置加载：不允许静默降级成"看起来正常但其实是假的"运行状态。"""

from __future__ import annotations

import pytest

from starrocks_mcp.config import load_settings


def _write(tmp_path, text: str):
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_config_file_raises_instead_of_defaulting_to_mock(tmp_path):
    # 默认值里 driver=mock，静默降级会让服务返回构造数据却看起来一切正常
    with pytest.raises(FileNotFoundError, match="配置文件不存在"):
        load_settings(tmp_path / "not-there.yaml")


def test_missing_env_var_raises_instead_of_becoming_empty_password(tmp_path, monkeypatch):
    monkeypatch.delenv("SR_TEST_PASSWORD", raising=False)
    path = _write(
        tmp_path,
        "database:\n"
        "  driver: pymysql\n"
        "  read_account:\n"
        "    user: reader\n"
        "    password: ${SR_TEST_PASSWORD}\n",
    )
    with pytest.raises(ValueError, match="SR_TEST_PASSWORD"):
        load_settings(path)


def test_env_var_is_interpolated_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("SR_TEST_PASSWORD", "s3cret")
    path = _write(
        tmp_path,
        "database:\n"
        "  driver: pymysql\n"
        "  read_account:\n"
        "    user: reader\n"
        "    password: ${SR_TEST_PASSWORD}\n",
    )
    settings = load_settings(path)
    assert settings.database.read_account.password == "s3cret"


def test_socket_timeout_defaults_above_query_timeout(tmp_path):
    """socket 超时必须大于查询预算，否则正常的慢查询会被 socket 层误杀。"""
    path = _write(tmp_path, "security:\n  query_timeout_seconds: 60\n")
    settings = load_settings(path)
    assert settings.effective_socket_timeout() > settings.security.query_timeout_seconds


def test_explicit_socket_timeout_wins(tmp_path):
    path = _write(
        tmp_path,
        "database:\n  socket_timeout: 12.5\nsecurity:\n  query_timeout_seconds: 60\n",
    )
    assert load_settings(path).effective_socket_timeout() == 12.5
