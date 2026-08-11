# starrocks-mcp 内网部署指南

## 包内容

```
starrocks-mcp-0.1.0-intranet-YYYYMMDD/
  wheels/              # 全部 Python 依赖（离线安装）
  config/              # 配置模板（不含密钥）
  scripts/             # 安装与启动脚本
  docs/                # 接口与 Cursor 接入说明
  logs/                # 审计日志目录（运行时写入）
  README.md
  VERSION.txt
```

## 环境要求

- Python **3.10+**（当前离线包按 **Python 3.11 + Windows x64** 下载依赖）
- 内网机器能访问 StarRocks（MySQL 协议端口，如 5744）
- 无需外网（依赖已打入 `wheels/`）

> **Linux 内网部署**：请在 Linux 机器上执行 `scripts/package-intranet.sh`（或同等 `pip download`）重新打包含 `manylinux` 的 wheels；Windows 打的包不能直接在 Linux 上 `pip install` 二进制依赖。

## 1. 解压部署包

将 `starrocks-mcp-*-intranet-*.zip` 解压到目标目录，例如：

- Linux: `/opt/starrocks-mcp`
- Windows: `D:\apps\starrocks-mcp`

## 2. 离线安装

**Windows:**

```powershell
cd D:\apps\starrocks-mcp
powershell -ExecutionPolicy Bypass -File scripts\install-offline.ps1
```

**Linux:**

```bash
cd /opt/starrocks-mcp
sed -i 's/\r$//' scripts/*.sh   # 若脚本在 Windows 上打包，先去掉 CRLF
chmod +x scripts/*.sh
./scripts/install-offline.sh
```

安装会在部署目录下创建 `.venv` 虚拟环境（Debian/Ubuntu 不允许 `pip install` 到系统 Python，见 PEP 668）。

若提示缺少 `venv` 模块，需先安装（有网机器上）：`apt install python3-venv` 或 `python3.11-venv`。

## 3. 配置

```bash
cp config/settings.production.example.yaml config/settings.yaml
cp config/apikeys.example.yaml config/apikeys.yaml
```

编辑 `config/settings.yaml`：填写 StarRocks `host` / `port` / 账号。

编辑 `config/apikeys.yaml`：配置用户与 apikey 哈希（不要存明文 key）。

生成 apikey 哈希：

```bash
python3 -c "import hashlib,sys; print('sha256:'+hashlib.sha256(sys.argv[1].encode()).hexdigest())" "your-api-key"
```

设置数据库密码环境变量（推荐）：

```bash
# Linux
export STARROCKS_READ_PASSWORD='your-password'
export STARROCKS_WRITE_PASSWORD='your-password'

# Windows PowerShell
$env:STARROCKS_READ_PASSWORD = 'your-password'
$env:STARROCKS_WRITE_PASSWORD = 'your-password'
```

## 4. 启动服务

**Windows:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-production.ps1
```

**Linux（前台）:**

```bash
./scripts/start-production.sh
```

**Linux（systemd 示例）:**

```ini
[Unit]
Description=starrocks-mcp
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/starrocks-mcp
Environment=STARROCKS_READ_PASSWORD=xxx
Environment=STARROCKS_WRITE_PASSWORD=xxx
ExecStart=/opt/starrocks-mcp/.venv/bin/python -m starrocks_mcp.main --config config/settings.yaml --log-level INFO
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 5. 验证

```bash
curl -s http://127.0.0.1:8080/healthz
```

应返回 `"status":"ok"` 且 `"mode":"live"`（`driver` 为 `pymysql` 时）。

## 6. Cursor / 客户端接入

在能访问该服务的机器上配置 MCP：

**Cursor（Streamable HTTP）**

```json
{
  "mcpServers": {
    "starrocks": {
      "url": "http://<内网服务IP>:8080/mcp",
      "headers": {
        "X-API-Key": "客户端持有的明文-apikey"
      }
    }
  }
}
```

**OpenClaw（SSE，默认传输）**

```json
{
  "mcp": {
    "servers": {
      "starrocks": {
        "url": "http://<内网服务IP>:8080/sse",
        "headers": {
          "X-API-Key": "客户端持有的明文-apikey"
        }
      }
    }
  }
}
```

详见 `docs/local-cursor.md`。

## 7. 运维端点

| 路径 | 鉴权 | 说明 |
|------|------|------|
| `/healthz` | 否 | 健康检查 |
| `/metrics` | 否 | Prometheus 指标 |
| `/mcp` | 是 | MCP Streamable HTTP |
| `/sse` | 是 | MCP SSE（OpenClaw 等） |
| `/messages` | 是 | SSE 客户端消息 POST（与 `/sse` 配套） |

生产环境建议用 Nginx 反代并加 HTTPS，`/metrics` 仅内网可达。

## 8. mock 与真实库切换

仅由 `config/settings.yaml` 中 `database.driver` 决定：

| driver | 行为 |
|--------|------|
| `mock` | 返回构造数据，不连库 |
| `pymysql` | 连接真实 StarRocks |

内网生产请使用 `pymysql`。
