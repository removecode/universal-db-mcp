#!/usr/bin/env bash
# 启动已离线安装的 starrocks-mcp。
# 用法:
#   ./run.sh
#   ./run.sh --config app/config/settings.yaml --log-level INFO
set -euo pipefail

PKG_ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$PKG_ROOT/app"
VENV_DIR="${STARROCKS_MCP_VENV:-$PKG_ROOT/.venv}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "未找到虚拟环境: $VENV_DIR" >&2
  echo "请先执行: ./install.sh" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
cd "$APP_DIR"
mkdir -p logs

CONFIG="config/settings.yaml"
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG="$2"
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "$CONFIG" ]]; then
  echo "配置文件不存在: $CONFIG" >&2
  echo "请复制示例配置并修改:" >&2
  echo "  cp config/settings.example.yaml config/settings.yaml" >&2
  echo "  cp config/apikeys.example.yaml config/apikeys.yaml" >&2
  exit 1
fi

HOST="$(awk '
  /^server:/{in_server=1; next}
  in_server && /^[^ ]/{in_server=0}
  in_server && /^[[:space:]]+host:/{gsub(/["'\'']/, "", $2); print $2; exit}
' "$CONFIG" 2>/dev/null || true)"
PORT="$(awk '
  /^server:/{in_server=1; next}
  in_server && /^[^ ]/{in_server=0}
  in_server && /^[[:space:]]+port:/{print $2; exit}
' "$CONFIG" 2>/dev/null || true)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

echo "==> 启动 starrocks-mcp"
echo "    配置: $CONFIG"
echo "    MCP:  http://${HOST}:${PORT}/mcp"
echo "    健康: http://${HOST}:${PORT}/healthz"
echo "    指标: http://${HOST}:${PORT}/metrics"
exec python -m starrocks_mcp.main --config "$CONFIG" "${ARGS[@]+"${ARGS[@]}"}"
