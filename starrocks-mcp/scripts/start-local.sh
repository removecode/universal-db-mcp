#!/usr/bin/env bash
# 本机一键启动 starrocks-mcp（mock 模式）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! python3 -c "import starrocks_mcp" 2>/dev/null; then
  echo "==> 安装依赖（可编辑模式）..."
  python3 -m pip install -e ".[dev]"
fi

mkdir -p logs
echo "==> 启动 starrocks-mcp: http://127.0.0.1:8080/mcp"
echo "    健康检查: curl http://127.0.0.1:8080/healthz"
echo "    测试 key (alice/只读): alice-local-dev-key"
echo "    测试 key (bob/读写):   bob-local-dev-key"
exec python3 -m starrocks_mcp.main --config config/settings.local.yaml --log-level INFO
