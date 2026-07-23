# 本机 Cursor 连接 starrocks-mcp

本仓库里的云端 Agent **不能**在你的笔记本电脑上直接启动进程。请在本机终端按下面步骤操作。

## 1. 本机启动 MCP

在本机打开终端，进入仓库里的 `starrocks-mcp` 目录：

```bash
cd starrocks-mcp
chmod +x scripts/start-local.sh
./scripts/start-local.sh
```

或手动：

```bash
cd starrocks-mcp
pip install -e ".[dev]"
python3 -m starrocks_mcp.main --config config/settings.local.yaml
```

看到类似日志即成功：

```text
Uvicorn running on http://127.0.0.1:8080
```

另开一个终端验证：

```bash
curl -s http://127.0.0.1:8080/healthz
```

应返回 `"status":"ok"` 且 `"mode":"mock"`。

## 2. 在 Cursor 里配置 MCP

打开 Cursor Settings → MCP，或编辑 MCP 配置文件（常见路径）：

- macOS: `~/.cursor/mcp.json`
- 或项目级: `.cursor/mcp.json`

加入（Streamable HTTP）：

```json
{
  "mcpServers": {
    "starrocks": {
      "url": "http://127.0.0.1:8080/mcp",
      "headers": {
        "X-API-Key": "alice-local-dev-key"
      }
    }
  }
}
```

读写账号可把 key 换成 `bob-local-dev-key`。

保存后在 Cursor MCP 面板里启用/刷新 `starrocks`，状态应变为已连接。

## 3. 在对话里试一下

连上后可以让 Agent：

1. 调用 `get_connection_status`
2. 调用 `get_database_info_tool`
3. 执行：

```sql
select * from paimon.csc.ods_csc_log_audit
where app_id='hids-identify-result-storage-svc'
  and year=2026 and month=202606 and day=20260610
limit 2
```

## 本地测试账号

| 用户 | 明文 apikey | 权限 |
|------|-------------|------|
| alice | `alice-local-dev-key` | 只读，仅 `paimon.csc` |
| bob | `bob-local-dev-key` | 读写，不限范围 |

对应哈希已写在 `config/apikeys.local.yaml`。

## 常见问题

- **Cursor 显示未连接**：确认本机 `8080` 已启动；URL 必须带 `/mcp`；Header 名是 `X-API-Key`。
- **401**：apikey 写错，或没用 `apikeys.local.yaml`。
- **Cursor 配置项不是 url**：不同 Cursor 版本字段可能是 `url` / `serverUrl`；以当前 Cursor MCP 设置页为准，核心是「HTTP URL + Header」。
- **想连真实 StarRocks**：把 `config/settings.local.yaml` 里 `database.driver` 改成 `pymysql`，并填 host/账号（密码用环境变量），再重启。
