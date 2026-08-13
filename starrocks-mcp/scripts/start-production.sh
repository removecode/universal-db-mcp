#!/usr/bin/env bash
# 启动 starrocks-mcp 生产服务（在部署包根目录执行）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CONFIG="$ROOT/config/settings.yaml"
VENV="$ROOT/.venv"
PYTHON="$VENV/bin/python"

if [[ ! -f "$CONFIG" ]]; then
  echo "缺少 config/settings.yaml，请从 config/settings.production.example.yaml 复制并修改" >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "未找到 $PYTHON，请先运行: ./scripts/install-offline.sh" >&2
  exit 1
fi

mkdir -p logs

PORT="$(grep -E '^[[:space:]]*port:' "$CONFIG" | head -1 | awk '{print $2}')"
PORT="${PORT:-8080}"

echo "==> 启动 starrocks-mcp"
echo "    配置: $CONFIG"
echo "    Python: $PYTHON"
echo "    健康检查: http://127.0.0.1:${PORT}/healthz"
echo "    MCP 端点: http://127.0.0.1:${PORT}/mcp"

exec "$PYTHON" -m starrocks_mcp.main --config "$CONFIG" --log-level INFO
