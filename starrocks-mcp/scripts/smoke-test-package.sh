#!/usr/bin/env bash
set -euo pipefail

PKG="$(ls /pkg/starrocks-mcp-0.1.0-intranet-linux-x86_64-py312-*.tar.gz | head -1)"
echo "==> 解压: $PKG"
cd /tmp
tar -xzf "$PKG"
cd "$(basename "${PKG%.tar.gz}")"

echo "==> 离线安装（容器已断网）"
bash scripts/install-offline.sh

echo ""
echo "==> 校验：配置文件缺失时应当直接报错，而不是静默降级成 mock"
if .venv/bin/python -m starrocks_mcp.main --config config/nope.yaml 2>/dev/null; then
  echo "FAIL: 配置文件不存在却启动成功了"
  exit 1
fi
echo "  OK  拒绝启动"

echo ""
echo "==> 校验：\${ENV_VAR} 未设置时应当直接报错，而不是把密码变成空串"
cp config/settings.example.yaml config/settings.yaml
cp config/apikeys.example.yaml config/apikeys.yaml
if .venv/bin/python -m starrocks_mcp.main --config config/settings.yaml 2>/dev/null; then
  echo "FAIL: 环境变量缺失却启动成功了"
  exit 1
fi
echo "  OK  拒绝启动"

echo ""
echo "==> 启动服务（mock 驱动）"
export STARROCKS_READ_PASSWORD=dummy
export STARROCKS_WRITE_PASSWORD=dummy
.venv/bin/python -m starrocks_mcp.main --config config/settings.yaml \
  --host 127.0.0.1 --port 8080 > /tmp/server.log 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 30); do
  if .venv/bin/python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo ""
echo "==> /healthz"
.venv/bin/python - <<'PY'
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:8080/healthz"))
print(json.dumps(d, ensure_ascii=False, indent=2))
assert d["status"] == "ok", d
assert "degraded" in d, "缺少新增的 degraded 字段"
for p in d["pools"]:
    assert "in_use" in p and "reason" in p, p
PY

echo ""
echo "==> /metrics 里的连接池指标"
.venv/bin/python -c "
import urllib.request
t = urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode()
lines = [l for l in t.splitlines() if l.startswith('mcp_pool')]
print('\n'.join(lines))
assert any('mcp_pool_waiting' in l for l in lines), '缺少新增的 mcp_pool_waiting'
"

echo ""
echo "==> 停止服务（验证不会卡死）"
kill -TERM $SERVER_PID
for _ in $(seq 1 15); do
  kill -0 $SERVER_PID 2>/dev/null || break
  sleep 1
done
if kill -0 $SERVER_PID 2>/dev/null; then
  echo "FAIL: SIGTERM 后 15 秒仍未退出"
  kill -9 $SERVER_PID
  exit 1
fi
echo "  OK  已正常退出"

echo ""
echo "全部通过"
