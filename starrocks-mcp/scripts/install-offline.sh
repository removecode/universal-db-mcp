#!/usr/bin/env bash
# 在离线 Linux x86_64 目标机上安装 starrocks-mcp。
# 用法（在解压后的离线包根目录）:
#   ./install.sh
#   ./install.sh /opt/starrocks-mcp/.venv
set -euo pipefail

PKG_ROOT="$(cd "$(dirname "$0")" && pwd)"
WHEELS_DIR="$PKG_ROOT/wheels"
APP_DIR="$PKG_ROOT/app"
VENV_DIR="${1:-$PKG_ROOT/.venv}"
PYTHON="${PYTHON:-python3}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令: $1" >&2
    exit 1
  }
}

need_cmd "$PYTHON"

if [[ ! -d "$WHEELS_DIR" ]]; then
  echo "未找到 wheels 目录: $WHEELS_DIR" >&2
  exit 1
fi

PY_MAJOR_MINOR="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
MIN_OK="$("$PYTHON" -c 'import sys; print(int(sys.version_info[:2] >= (3, 10)))')"
if [[ "$MIN_OK" != "1" ]]; then
  echo "需要 Python >= 3.10，当前: $PY_MAJOR_MINOR" >&2
  exit 1
fi

if [[ -f "$PKG_ROOT/BUILDINFO.txt" ]]; then
  BUILT_PY="$(awk -F= '/^python=/{print $2}' "$PKG_ROOT/BUILDINFO.txt" || true)"
  if [[ -n "${BUILT_PY:-}" && "$BUILT_PY" != "$PY_MAJOR_MINOR" ]]; then
    echo "警告: 离线包按 Python ${BUILT_PY} 打包，当前为 ${PY_MAJOR_MINOR}。" >&2
    echo "若安装失败，请在目标机安装 Python ${BUILT_PY} 后重试。" >&2
  fi
fi

find_wheel() {
  local pattern="$1"
  ls -1 "$WHEELS_DIR"/$pattern 2>/dev/null | head -n1 || true
}

bootstrap_pip() {
  # 不依赖 ensurepip / 外网：解压包内 pip wheel 后离线安装
  local pip_whl tmp
  pip_whl="$(find_wheel 'pip-*.whl')"
  if [[ -z "$pip_whl" ]]; then
    echo "wheels/ 中缺少 pip-*.whl，无法离线引导 pip" >&2
    exit 1
  fi
  echo "==> 使用包内 wheel 引导 pip"
  tmp="$(mktemp -d)"
  python -m zipfile -e "$pip_whl" "$tmp"
  PYTHONPATH="$tmp" python -m pip install --no-index --find-links="$WHEELS_DIR" --upgrade pip setuptools wheel
  rm -rf "$tmp"
}

echo "==> 创建虚拟环境: $VENV_DIR"
rm -rf "$VENV_DIR"
if ! "$PYTHON" -m venv "$VENV_DIR" 2>/tmp/starrocks-mcp-venv.err; then
  echo "==> python -m venv 失败，改用 --without-pip 创建后离线引导 pip"
  cat /tmp/starrocks-mcp-venv.err >&2 || true
  "$PYTHON" -m venv --without-pip "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! python -m pip --version >/dev/null 2>&1; then
  bootstrap_pip
else
  echo "==> 离线升级 pip / setuptools / wheel"
  python -m pip install --no-index --find-links="$WHEELS_DIR" --upgrade pip setuptools wheel
fi

echo "==> 离线安装 starrocks-mcp 及依赖"
PROJECT_WHEEL="$(find_wheel 'starrocks_mcp-*.whl')"
if [[ -n "$PROJECT_WHEEL" ]]; then
  python -m pip install --no-index --find-links="$WHEELS_DIR" "$PROJECT_WHEEL"
else
  python -m pip install --no-index --find-links="$WHEELS_DIR" -e "$APP_DIR"
fi

mkdir -p "$APP_DIR/logs" "$APP_DIR/config"

if [[ ! -f "$APP_DIR/config/settings.yaml" && -f "$APP_DIR/config/settings.example.yaml" ]]; then
  cp "$APP_DIR/config/settings.example.yaml" "$APP_DIR/config/settings.yaml"
  echo "==> 已生成 $APP_DIR/config/settings.yaml（请按环境修改）"
fi
if [[ ! -f "$APP_DIR/config/apikeys.yaml" && -f "$APP_DIR/config/apikeys.example.yaml" ]]; then
  cp "$APP_DIR/config/apikeys.example.yaml" "$APP_DIR/config/apikeys.yaml"
  echo "==> 已生成 $APP_DIR/config/apikeys.yaml（请填入 apikey 哈希）"
fi

cat >"$PKG_ROOT/activate.sh" <<EOF
#!/usr/bin/env bash
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
export STARROCKS_MCP_HOME="$APP_DIR"
cd "\$STARROCKS_MCP_HOME"
EOF
chmod +x "$PKG_ROOT/activate.sh"

echo
echo "==> 安装完成"
echo "激活环境: source $PKG_ROOT/activate.sh"
echo "启动服务: $PKG_ROOT/run.sh"
python -c "import starrocks_mcp; print('starrocks_mcp OK:', getattr(starrocks_mcp, '__version__', 'installed'))"
