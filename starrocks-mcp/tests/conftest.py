"""共享的测试 fixtures：假 apikeys.yaml 文件、mock 连接池、轻量的 AppState 替身。"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from starrocks_mcp.auth.provider import YamlAuthProvider
from starrocks_mcp.config import Settings
from starrocks_mcp.db.executor import PooledExecutor
from starrocks_mcp.db.mock_pool import MockConnectionPool
from starrocks_mcp.monitoring.metrics import Metrics
from starrocks_mcp.monitoring.request_log import RequestLogger

READONLY_KEY = "test-readonly-key"
READWRITE_KEY = "test-readwrite-key"


def hash_key(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.fixture
def apikeys_file(tmp_path):
    path = tmp_path / "apikeys.yaml"
    data = {
        "users": {
            "alice": {
                "api_key": hash_key(READONLY_KEY),
                "id": "bi-team-readonly",
                "role": "read",
                "scope": [{"catalog": "paimon", "database": "csc"}],
            },
            "bob": {
                "api_key": hash_key(READWRITE_KEY),
                "id": "etl-job",
                "role": "readwrite",
                "scope": None,
            },
        }
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def auth_provider(apikeys_file):
    return YamlAuthProvider(apikeys_file)


@pytest.fixture
def settings(tmp_path, apikeys_file):
    s = Settings()
    s.base_dir = tmp_path
    s.auth.apikeys_file = str(apikeys_file)
    s.monitoring.audit_db_path = str(tmp_path / "audit.db")
    s.database.driver = "mock"
    return s


class FakeAppState:
    """替代真正的 `starrocks_mcp.server.AppState`，避免测试依赖真实连接池/进程管理。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.read_pool = MockConnectionPool(role="read")
        self.write_pool = MockConnectionPool(role="write")
        self.executor = PooledExecutor(max_workers=4)
        self.metrics = Metrics()
        self.request_logger = RequestLogger(settings.resolve_path(settings.monitoring.audit_db_path))

    def pool_for(self, pool_name: str):
        return self.read_pool if pool_name == "read" else self.write_pool

    def close(self) -> None:
        self.executor.shutdown()


@pytest.fixture
def app_state(settings):
    state = FakeAppState(settings)
    yield state
    state.close()
