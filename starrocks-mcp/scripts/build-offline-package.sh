#!/usr/bin/env bash
# 在可联网的 Linux x86_64 机器上打包 starrocks-mcp 离线部署包。
# 产物：dist/starrocks-mcp-offline-linux-x86_64-<version>.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"
INCLUDE_DEV="${INCLUDE_DEV:-0}"
OUT_DIR="${OUT_DIR:-$ROOT/dist}"
STAGE_DIR="${STAGE_DIR:-/tmp/starrocks-mcp-offline-stage}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令: $1" >&2
    exit 1
  }
}

need_cmd "$PYTHON"
need_cmd tar
need_cmd gzip

ARCH="$("$PYTHON" -c 'import platform; print(platform.machine())')"
if [[ "$ARCH" != "x86_64" && "$ARCH" != "AMD64" ]]; then
  echo "警告: 当前架构为 $ARCH，目标平台为 Linux x86_64。" >&2
  echo "请在 x86_64 Linux 上打包，或显式设置 PLATFORM / 使用兼容 wheel。" >&2
fi

PY_VER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
VERSION="$("$PYTHON" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
PKG_NAME="starrocks-mcp-offline-linux-x86_64-${VERSION}"
PKG_ROOT="$STAGE_DIR/$PKG_NAME"

echo "==> 清理并创建暂存目录: $PKG_ROOT"
rm -rf "$STAGE_DIR"
mkdir -p "$PKG_ROOT/wheels" "$PKG_ROOT/app" "$OUT_DIR"

export PATH="${HOME}/.local/bin:${PATH}"

# 使用独立 build venv，避免系统 Python PEP 668（externally-managed-environment）拦截
BUILD_VENV="${BUILD_VENV:-$STAGE_DIR/.build-venv}"
echo "==> 准备构建虚拟环境: $BUILD_VENV"
"$PYTHON" -m venv "$BUILD_VENV"
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"
python -m pip install -U "pip>=24" "wheel" "setuptools>=68" "build" >/dev/null
BUILD_PYTHON="$(command -v python)"

echo "==> 构建 starrocks-mcp 源码包 / wheel"
rm -rf build
# 保留历史 offline tar.gz，只清理项目 wheel/sdist
find dist -maxdepth 1 -type f \( -name 'starrocks_mcp-*.whl' -o -name 'starrocks_mcp-*.tar.gz' \) -delete 2>/dev/null || true
mkdir -p dist
"$BUILD_PYTHON" -m build --outdir dist

# 复制应用源码（不含本地密钥与虚拟环境）
echo "==> 复制应用文件"
tar -C "$ROOT" \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.egg-info' \
  --exclude='.pytest_cache' \
  --exclude='build' \
  --exclude='dist' \
  --exclude='offline' \
  --exclude='config/settings.yaml' \
  --exclude='config/apikeys.yaml' \
  --exclude='config/*.local.yaml' \
  --exclude='logs/*.db' \
  -cf - . | tar -C "$PKG_ROOT/app" -xf -

# 把刚构建的项目 wheel/sdist 放进包内，便于离线 pip install
mkdir -p "$PKG_ROOT/app/dist"
shopt -s nullglob
PROJECT_ARTIFACTS=(dist/starrocks_mcp-*.whl dist/starrocks_mcp-*.tar.gz)
if [[ ${#PROJECT_ARTIFACTS[@]} -eq 0 ]]; then
  echo "未找到 dist/starrocks_mcp-* 构建产物" >&2
  exit 1
fi
cp -a "${PROJECT_ARTIFACTS[@]}" "$PKG_ROOT/wheels/"
cp -a "${PROJECT_ARTIFACTS[@]}" "$PKG_ROOT/app/dist/"
shopt -u nullglob

echo "==> 下载运行依赖 wheel（platform=$PLATFORM, python=$PY_VER）"
REQ_FILE="$PKG_ROOT/requirements-runtime.txt"
"$BUILD_PYTHON" - <<'PY' >"$REQ_FILE"
from pathlib import Path
import tomllib
data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
for dep in data["project"]["dependencies"]:
    print(dep)
PY

# 安装工具链本身也打进离线包，目标机无需联网升级 pip
"$BUILD_PYTHON" -m pip download \
  -d "$PKG_ROOT/wheels" \
  --python-version "$PY_VER" \
  --only-binary=:all: \
  --platform "$PLATFORM" \
  --platform manylinux_2_17_x86_64 \
  --platform manylinux2014_x86_64 \
  --platform linux_x86_64 \
  --platform any \
  pip setuptools wheel packaging

# 项目依赖：优先二进制；个别纯 Python 包可能只有 sdist，再兜底下载
set +e
"$BUILD_PYTHON" -m pip download \
  -d "$PKG_ROOT/wheels" \
  --python-version "$PY_VER" \
  --only-binary=:all: \
  --platform "$PLATFORM" \
  --platform manylinux_2_17_x86_64 \
  --platform manylinux2014_x86_64 \
  --platform linux_x86_64 \
  --platform any \
  -r "$REQ_FILE"
DL_RC=$?
set -e
if [[ $DL_RC -ne 0 ]]; then
  echo "==> 部分包无对应平台 wheel，改用当前环境解析依赖（仍优先 wheel）"
  "$BUILD_PYTHON" -m pip download \
    -d "$PKG_ROOT/wheels" \
    -r "$REQ_FILE"
fi

if [[ "$INCLUDE_DEV" == "1" ]]; then
  echo "==> 额外下载开发依赖"
  DEV_REQ="$PKG_ROOT/requirements-dev.txt"
  "$BUILD_PYTHON" - <<'PY' >"$DEV_REQ"
from pathlib import Path
import tomllib
data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
for dep in data["project"].get("optional-dependencies", {}).get("dev", []):
    print(dep)
PY
  "$BUILD_PYTHON" -m pip download -d "$PKG_ROOT/wheels" -r "$DEV_REQ"
fi

# 安装 / 启动脚本
cp "$ROOT/scripts/install-offline.sh" "$PKG_ROOT/install.sh"
cp "$ROOT/scripts/run-offline.sh" "$PKG_ROOT/run.sh"
chmod +x "$PKG_ROOT/install.sh" "$PKG_ROOT/run.sh"

# 包内说明
cat >"$PKG_ROOT/README-OFFLINE.md" <<EOF
# starrocks-mcp 离线部署包（Linux x86_64）

- 版本: ${VERSION}
- 目标架构: Linux x86_64
- 打包 Python: ${PY_VER}
- 平台标签: ${PLATFORM} (+ manylinux / any)

## 目标机要求

- Linux x86_64
- Python ${PY_VER}.x（推荐与打包机主次版本一致；最低 >= 3.10）
- 能访问目标 StarRocks（若使用 pymysql 驱动）

## 安装

\`\`\`bash
tar -xzf ${PKG_NAME}.tar.gz
cd ${PKG_NAME}
./install.sh
\`\`\`

默认会在当前目录创建 \`.venv\` 并从 \`wheels/\` 离线安装全部依赖。

## 配置

\`\`\`bash
cp app/config/settings.example.yaml app/config/settings.yaml
cp app/config/apikeys.example.yaml app/config/apikeys.yaml
# 编辑 settings.yaml / apikeys.yaml，或用环境变量注入密码
\`\`\`

## 启动

\`\`\`bash
./run.sh
# 或指定配置:
./run.sh --config app/config/settings.yaml
\`\`\`

健康检查: \`curl http://127.0.0.1:8080/healthz\`
MCP 入口: \`http://<host>:8080/mcp\`

完整说明见 \`app/docs/offline-deploy.md\`。
EOF

# 清单
(
  cd "$PKG_ROOT"
  find wheels -type f | sort > WHEELS.manifest
  echo "python=${PY_VER}" > BUILDINFO.txt
  echo "arch=x86_64" >> BUILDINFO.txt
  echo "platform=${PLATFORM}" >> BUILDINFO.txt
  echo "version=${VERSION}" >> BUILDINFO.txt
  echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> BUILDINFO.txt
  echo "wheel_count=$(find wheels -type f | wc -l)" >> BUILDINFO.txt
)

ARCHIVE="$OUT_DIR/${PKG_NAME}.tar.gz"
echo "==> 打包 $ARCHIVE"
tar -C "$STAGE_DIR" -czf "$ARCHIVE" "$PKG_NAME"

echo "==> 完成"
ls -lh "$ARCHIVE"
echo "wheel 数量: $(find "$PKG_ROOT/wheels" -type f | wc -l)"
cat "$PKG_ROOT/BUILDINFO.txt"
