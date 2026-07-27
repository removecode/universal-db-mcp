# starrocks-mcp 离线部署（Linux x86_64）

本文说明如何在**可联网机器**上打包依赖，并在**离线 Linux x86_64** 目标机上安装、配置与启动。

## 1. 前置条件

| 角色 | 要求 |
|---|---|
| 打包机 | Linux x86_64，可访问 PyPI，Python >= 3.10（建议 3.10/3.11/3.12） |
| 目标机 | Linux x86_64，**无需外网**，Python 主次版本尽量与打包机一致（最低 >= 3.10）。建议安装 `python3-venv`（Debian/Ubuntu: `apt install python3-venv`）；若无 ensurepip，`install.sh` 会用包内 pip wheel 兜底引导 |
| StarRocks | 目标机能访问 FE MySQL 协议端口（使用 `pymysql` 驱动时） |

> 离线包按打包时的 Python 小版本下载 wheel。目标机 Python 为 3.12 时，请用 3.12 打包；3.10/3.11 同理。

## 2. 在联网机器上打包

```bash
cd starrocks-mcp
chmod +x scripts/build-offline-package.sh \
         scripts/install-offline.sh \
         scripts/run-offline.sh

# 默认按当前 python3 版本、manylinux2014_x86_64 下载依赖
./scripts/build-offline-package.sh
```

成功后得到：

```text
dist/starrocks-mcp-offline-linux-x86_64-<version>.tar.gz
```

可选环境变量：

| 变量 | 含义 | 默认 |
|---|---|---|
| `PYTHON` | 使用的 Python 解释器 | `python3` |
| `PLATFORM` | pip 平台标签 | `manylinux2014_x86_64` |
| `INCLUDE_DEV` | `1` 时额外打包 pytest 等开发依赖 | `0` |
| `OUT_DIR` | 产物目录 | `./dist` |

将生成的 `.tar.gz` 拷贝到离线目标机（U 盘、内网文件服务器等）。

## 3. 在离线目标机上安装

```bash
tar -xzf starrocks-mcp-offline-linux-x86_64-<version>.tar.gz
cd starrocks-mcp-offline-linux-x86_64-<version>

# 在包目录创建 .venv 并离线安装
./install.sh

# 或指定虚拟环境路径
# ./install.sh /opt/starrocks-mcp/.venv
```

`install.sh` 会：

1. 用目标机 `python3 -m venv` 创建虚拟环境  
2. 从 `wheels/` **完全离线**安装 `pip/setuptools/wheel` 与全部运行依赖  
3. 安装 `starrocks-mcp`  
4. 若尚无配置，从示例生成 `app/config/settings.yaml` 与 `app/config/apikeys.yaml`

## 4. 配置

### 4.1 服务与数据库

编辑 `app/config/settings.yaml`：

```yaml
server:
  host: 0.0.0.0
  port: 8080
  transport: streamable-http
  # 多实例 + 负载均衡时建议 true
  stateless_http: false

database:
  driver: pymysql          # 生产改为 pymysql；联调可用 mock
  host: 10.58.16.10
  port: 5744
  charset: utf8
  read_account:
    user: reader
    password: ${STARROCKS_READ_PASSWORD}
  write_account:
    user: writer
    password: ${STARROCKS_WRITE_PASSWORD}
```

密码建议用环境变量注入，不要写进文件：

```bash
export STARROCKS_READ_PASSWORD='******'
export STARROCKS_WRITE_PASSWORD='******'
```

### 4.2 ApiKey

编辑 `app/config/apikeys.yaml`，写入用户与 key 的 SHA-256：

```bash
python3 -c "import hashlib; print('sha256:' + hashlib.sha256(b'your-raw-key').hexdigest())"
```

示例：

```yaml
users:
  alice:
    api_key: "sha256:<hash>"
    id: bi-team-readonly
    role: read
    scope:
      - catalog: default_catalog
        database: analytics
```

## 5. 启动与验证

```bash
./run.sh
# 或
./run.sh --config app/config/settings.yaml --log-level INFO
```

验证：

```bash
curl -sS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/metrics | head
```

MCP 地址（客户端配置）：

```text
http://<目标机IP>:8080/mcp
```

请求头携带明文 apikey，例如：`X-API-Key: your-raw-key`（也支持 `Authorization: Bearer <key>`）。

### systemd 示例（可选）

```ini
[Unit]
Description=starrocks-mcp
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/starrocks-mcp-offline/app
Environment=STARROCKS_READ_PASSWORD=******
Environment=STARROCKS_WRITE_PASSWORD=******
ExecStart=/opt/starrocks-mcp-offline/.venv/bin/python -m starrocks_mcp.main --config config/settings.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

将离线包解压到 `/opt/starrocks-mcp-offline` 并完成 `./install.sh` 后，把上述 unit 放到 `/etc/systemd/system/starrocks-mcp.service`，再执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now starrocks-mcp
```

## 6. 目录结构（离线包）

```text
starrocks-mcp-offline-linux-x86_64-<version>/
  README-OFFLINE.md      # 包内快速说明
  BUILDINFO.txt          # 打包 Python / 平台 / 版本信息
  WHEELS.manifest        # wheel 清单
  install.sh             # 离线安装
  run.sh                 # 启动
  wheels/                # 全部 Python 依赖（含项目自身 wheel）
  app/                   # 应用源码与配置示例
    config/
    docs/offline-deploy.md
    src/starrocks_mcp/
```

## 7. 常见问题

**安装报错 `No matching distribution`**  
目标机 Python 小版本与打包机不一致。用目标机同版本 Python 重新执行 `build-offline-package.sh`。

**`healthz` 返回 503**  
真实驱动连不上 StarRocks。检查 `database.host/port`、账号密码、网络与防火墙。

**只有 mock 能通、pymysql 失败**  
确认 StarRocks FE 已开启 MySQL 协议端口，且只读/读写账号权限正确（见项目根 `README.md`）。

**多实例部署会话异常**  
在负载均衡后跑多进程时，将 `server.stateless_http` 设为 `true`。
