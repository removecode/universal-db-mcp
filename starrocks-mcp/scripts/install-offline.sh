#!/usr/bin/env bash
# 内网离线安装 starrocks-mcp（在解压后的部署包根目录执行）
# 使用项目内 .venv，避免 Debian/Ubuntu PEP 668 限制系统 pip。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELS="$ROOT/wheels"
VENV="$ROOT/.venv"

if [[ ! -d "$WHEELS" ]]; then
  echo "未找到 wheels 目录: $WHEELS" >&2
  exit 1
fi

if ! python3 -c "import venv" 2>/dev/null; then
  echo "缺少 python3-venv，请先安装（有网环境）：apt install python3-venv 或 python3.11-venv" >&2
  exit 1
fi

echo "==> 创建虚拟环境: $VENV"
python3 -m venv "$VENV"

echo "==> 从本地 wheels 离线安装（当前 Python: $(python3 --version)）"
# pip 自升级只在 wheels 里带了 pip 时才做：离线环境下访问不到 pip 源，
# 加上 set -e 会让整个安装在这一步直接失败
"$VENV/bin/pip" install --no-index --find-links "$WHEELS" --upgrade pip 2>/dev/null \
  || echo "    （跳过 pip 自升级：离线且 wheels 内无 pip，不影响后续安装）"
"$VENV/bin/pip" install --no-index --find-links "$WHEELS" starrocks-mcp

echo ""
echo "安装完成。下一步:"
echo "  1. cp config/settings.production.example.yaml config/settings.yaml"
echo "  2. cp config/apikeys.example.yaml config/apikeys.yaml 并填写用户/apikey 哈希"
echo "  3. export STARROCKS_READ_PASSWORD / STARROCKS_WRITE_PASSWORD"
echo "  4. ./scripts/start-production.sh"
