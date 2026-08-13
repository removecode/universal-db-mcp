#!/usr/bin/env bash
# 打包 starrocks-mcp 内网离线部署包。
#
# 在与目标机同架构的 Linux 上执行最稳妥（依赖里 pydantic-core、rpds-py 等是编译
# 产物，跨平台下载需要额外指定 --platform/--abi，容易配错）。
#
# 用法：
#   ./scripts/package-intranet.sh
#       按 requirements-lock-linux-py312.txt 下载依赖（需要能访问 pip 源）
#
#   REUSE_WHEELS=/path/to/现有部署包/wheels ./scripts/package-intranet.sh
#       复用既有依赖 wheel，只重新构建项目自身的 wheel。
#       依赖没有变化时首选这种：产出的依赖版本和线上正在跑的完全一致，零升级风险。
#
# 可用环境变量：PYTHON / LOCK_FILE / PLATFORM_LABEL / REUSE_WHEELS
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="0.1.0"
STAMP="$(date +%Y%m%d)"
PYTHON="${PYTHON:-python3}"
PLATFORM_LABEL="${PLATFORM_LABEL:-linux-x86_64}"
LOCK_FILE="${LOCK_FILE:-requirements-lock-linux-py312.txt}"
REUSE_WHEELS="${REUSE_WHEELS:-}"

OUT_NAME="starrocks-mcp-${VERSION}-intranet-${PLATFORM_LABEL}-${STAMP}"
STAGE="$ROOT/dist/$OUT_NAME"
WHEELS="$STAGE/wheels"

echo "==> 清理并创建目录: $STAGE"
rm -rf "$STAGE"
mkdir -p "$WHEELS" "$STAGE/config" "$STAGE/scripts" "$STAGE/docs" "$STAGE/logs"

echo "==> 构建项目 wheel"
"$PYTHON" -m pip install -q build wheel
"$PYTHON" -m build --wheel --outdir "$WHEELS"

if [[ -n "$REUSE_WHEELS" ]]; then
  if [[ ! -d "$REUSE_WHEELS" ]]; then
    echo "REUSE_WHEELS 指向的目录不存在: $REUSE_WHEELS" >&2
    exit 1
  fi
  echo "==> 复用既有依赖 wheel: $REUSE_WHEELS"
  # 排除 starrocks_mcp 自身，那个用上面刚构建的新版本
  find "$REUSE_WHEELS" -maxdepth 1 \( -name '*.whl' -o -name '*.tar.gz' \) \
    ! -name 'starrocks_mcp-*' -exec cp -n {} "$WHEELS/" \;
else
  if [[ ! -f "$LOCK_FILE" ]]; then
    echo "找不到锁定文件: $LOCK_FILE（可用 uv pip compile 生成，见文件头注释）" >&2
    exit 1
  fi
  echo "==> 按锁定文件下载依赖 wheel: $LOCK_FILE"
  "$PYTHON" -m pip download -r "$LOCK_FILE" -d "$WHEELS"
fi

echo "==> 复制配置模板与文档"
cp config/settings.example.yaml config/settings.production.example.yaml \
   config/apikeys.example.yaml "$STAGE/config/"
cp README.md "$STAGE/"
cp docs/api.md docs/local-cursor.md docs/deploy-intranet.md "$STAGE/docs/"
cp requirements-prod.txt requirements-prod.constraints.txt "$STAGE/"
[[ -f "$LOCK_FILE" ]] && cp "$LOCK_FILE" "$STAGE/"

echo "==> 复制安装/启动脚本"
cp scripts/install-offline.ps1 scripts/install-offline.sh \
   scripts/start-production.ps1 scripts/start-production.sh "$STAGE/scripts/"
chmod +x "$STAGE/scripts/"*.sh
touch "$STAGE/logs/.gitkeep"

{
  echo "starrocks-mcp $VERSION"
  echo "packaged_at: $(date -Iseconds)"
  echo "platform: $PLATFORM_LABEL"
  echo "build_python: $("$PYTHON" --version 2>&1)"
  if [[ -n "$REUSE_WHEELS" ]]; then
    echo "deps: 复用自 $REUSE_WHEELS"
  else
    echo "deps: $LOCK_FILE"
  fi
  echo ""
  echo "wheels:"
  ls -1 "$WHEELS"
} > "$STAGE/VERSION.txt"

TARBALL="$ROOT/dist/${OUT_NAME}.tar.gz"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$ROOT/dist" "$OUT_NAME"

echo ""
echo "完成: $TARBALL"
du -h "$TARBALL"
