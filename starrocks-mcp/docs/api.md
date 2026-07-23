# starrocks-mcp 接口文档

本文档描述当前版本对外提供的全部接口，分为：

1. **MCP 工具接口**（给 LLM / MCP 客户端调用，需要 apikey）
2. **HTTP 运维端点**（给人 / 探针 / 监控系统调用）

基础地址默认：`http://<host>:<port>`（配置项 `server.host` / `server.port`，默认 `0.0.0.0:8080`）。

MCP 协议端点：`POST/GET/DELETE http://<host>:<port>/mcp`

---

## 鉴权

除运维端点外，所有 MCP 请求都必须携带 apikey。

| Header | 说明 |
|--------|------|
| `X-API-Key: <明文 apikey>` | 推荐 |
| `Authorization: Bearer <明文 apikey>` | 兼容写法 |

未携带或 apikey 无效时，服务在进入 MCP 协议层之前直接返回：

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"error":"unauthorized","message":"缺少有效的 apikey，请在请求头中提供 X-API-Key"}
```

apikey 与权限的映射在服务端 `config/apikeys.yaml` 中配置（文件里存的是 sha256 哈希，不是明文）。这同时也是 **用户 ↔ apikey** 映射：请求带上 apikey 后，服务端反查出 `username`，审计日志直接记录用户名。

推荐配置（以用户为中心）：

```yaml
users:
  alice:
    api_key: "sha256:..."
    id: bi-team-readonly   # 可选
    role: read
    scope:
      - catalog: paimon
        database: csc
```

每个用户条目包含：

| 字段 | 含义 |
|------|------|
| `username`（users 的 key） | 业务用户名，审计日志记录这个 |
| `api_key` | apikey 的 sha256 哈希 |
| `id` | apikey 标识（可选，缺省等于用户名） |
| `role` | `read`（只能查）或 `readwrite`（可查可写） |
| `scope` | 可选。限制能访问的 `catalog` / `database`；`null` 表示不限制 |

---

## 一、MCP 工具接口

客户端先通过 MCP 协议完成 `initialize`，再调用 `tools/list` / `tools/call`。下面按工具说明。

当前注册的工具：

| 工具名 | 用途 |
|--------|------|
| `get_connection_status` | 检查 MCP 是否已连上数据库 |
| `get_database_info_tool` | 结构理解（catalog / database / table / columns） |
| `execute_sql` | 执行 SQL（带安全审计） |

### 1. `get_connection_status`

检查当前 MCP 是否已成功连接到 StarRocks。会分别探测只读连接池和读写连接池。

**参数**：无

**返回示例（mock 模式，服务可用）：**

```json
{
  "connected": true,
  "driver": "mock",
  "mode": "mock",
  "host": null,
  "port": null,
  "pools": [
    {"pool": "read", "connected": true, "latency_ms": 0.12, "error": null},
    {"pool": "write", "connected": true, "latency_ms": 0.08, "error": null}
  ],
  "message": "当前运行在 mock 模式，未连接真实 StarRocks；服务可用，返回的是构造数据。"
}
```

**返回示例（真实驱动连不上库）：**

```json
{
  "connected": false,
  "driver": "pymysql",
  "mode": "live",
  "host": "10.58.16.10",
  "port": 5744,
  "pools": [
    {
      "pool": "read",
      "connected": false,
      "latency_ms": 12.5,
      "error": "Can't connect to MySQL server on '10.58.16.10'"
    },
    {
      "pool": "write",
      "connected": false,
      "latency_ms": 11.8,
      "error": "Can't connect to MySQL server on '10.58.16.10'"
    }
  ],
  "message": "数据库未连接或当前不可用（10.58.16.10:5744）。失败的连接池: read: ...; write: ...。请检查数据库地址、端口、账号密码以及网络连通性。"
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `connected` | bool | 整体是否可用。mock 模式恒为 `true`；真实驱动要求读/写两个池都通 |
| `driver` | string | `mock` 或 `pymysql` |
| `mode` | string | `mock`（构造数据）或 `live`（真实库） |
| `host` / `port` | string/int/null | 真实库地址；mock 模式为 `null` |
| `pools` | array | 各连接池探测结果 |
| `message` | string | 面向调用方的可读提示 |

建议：执行查询前、或怀疑数据库不可用时，先调用本工具。

---

### 2. `get_database_info_tool`

获取 StarRocks 结构信息，帮助 LLM 理解可查询的数据。参数均可选，按层级传入。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `catalog` | string | 否 | catalog 名。只传 `database`/`table` 时默认 `default_catalog` |
| `database` | string | 否 | 库名 |
| `table` | string | 否 | 表名。传 `table` 时必须同时传 `database` |

**行为：**

| 传参 | 返回 `level` | 内容 |
|------|--------------|------|
| 不传 | `catalogs` | 当前 apikey 可见的 catalog 列表 |
| 只传 `catalog` | `databases` | 该 catalog 下的 database 列表（会按 apikey scope 过滤） |
| `catalog` + `database` | `tables` | 该库下的 table 列表 |
| `catalog` + `database` + `table` | `columns` | 列结构；内部表额外带 `create_table`，外部 catalog 带 `note` |

**返回示例（下钻到表）：**

```json
{
  "level": "columns",
  "catalog": "paimon",
  "database": "csc",
  "table": "ods_csc_log_audit",
  "columns": [
    {"Field": "app_id", "Type": "varchar(128)", "Null": "YES", "Key": "", "Default": null, "Extra": ""},
    {"Field": "year", "Type": "int", "Null": "YES", "Key": "", "Default": null, "Extra": ""}
  ],
  "note": "该表来自外部 catalog，通常不具备 StarRocks 原生表的主键/分区/分桶等概念，..."
}
```

**错误提示：**

- apikey 无权访问目标 catalog/database → 权限错误
- 数据库连不上 → 明确提示「数据库未连接或当前不可用...」，而不是原始驱动堆栈

---

### 3. `execute_sql`

执行一条 SQL。执行前会做安全审计，通过后按语句类型路由到只读或读写连接池。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sql` | string | 是 | 要执行的 SQL（一次只能一条） |
| `max_rows` | number | 否 | 客户端侧再截断返回行数；只能收紧，不能突破服务端上限 |
| `timeout_seconds` | number | 否 | 本次超时；只能收紧，不能突破服务端 `query_timeout_seconds` |

**安全审计规则（摘要）：**

| 规则 | 说明 |
|------|------|
| 单语句 | 拒绝多语句堆叠（如 `SELECT 1; DROP TABLE x`） |
| 角色白名单 | `read`：`SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`；`readwrite`：额外允许 `INSERT`/`UPDATE`/`DELETE` |
| 高危禁止 | 任何角色都不能执行 `DROP`/`TRUNCATE`/`ALTER`/`CREATE`/`GRANT`/`REVOKE`/`LOAD`/`RENAME`/`KILL`/`SET` |
| 访问范围 | 若 apikey 配置了 `scope`，SQL 引用的表必须在范围内；无法可靠解析时默认 fail-closed 拒绝 |
| LIMIT 保护 | `SELECT` 没有 `LIMIT` 时自动注入默认上限；超过硬上限会被收紧 |

**连接池选择：**

- `SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN` → 只读池
- `INSERT` / `UPDATE` / `DELETE` → 读写池（且角色必须是 `readwrite`）

**成功返回示例：**

```json
{
  "columns": ["app_id", "year", "month", "day", "log_time", "log_content"],
  "rows": [
    {
      "app_id": "hids-identify-result-storage-svc",
      "year": 2026,
      "month": 202606,
      "day": 20260610,
      "log_time": "2026-06-10T12:00:00+00:00",
      "log_content": "mock log content #1"
    }
  ],
  "row_count": 2,
  "affected_rows": null,
  "statement_type": "SELECT",
  "pool_used": "read",
  "limit_applied": 2,
  "limit_source": "user",
  "truncated": false,
  "execution_time_ms": 4.3
}
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `columns` / `rows` / `row_count` | 查询结果 |
| `affected_rows` | 写操作受影响行数；查询时为 `null` |
| `statement_type` | 识别出的语句类型 |
| `pool_used` | `read` 或 `write` |
| `limit_applied` | 生效的 LIMIT 值（仅 SELECT） |
| `limit_source` | `user` / `default_injected` / `capped` |
| `truncated` | LIMIT 是否被服务端收紧 |
| `execution_time_ms` | 服务端统计的执行耗时 |

**常见失败提示：**

| 场景 | 提示特征 |
|------|----------|
| 无权限写 / 越权访问 / 多语句 / DDL | `SQL 未通过安全审计: ...` |
| 数据库连不上 | `数据库未连接或当前不可用。请检查 StarRocks 地址/端口/账号密码...` |
| 执行超时 | `查询执行超过 Xs 超时限制` |

---

## 二、HTTP 运维端点

这些端点**不需要 apikey**。

### 1. `GET /healthz`

健康检查：同时探测进程存活和数据库连通性。

| 场景 | HTTP 状态码 | `status` |
|------|-------------|----------|
| 进程正常且数据库可用 | 200 | `ok` |
| mock 模式（服务可用，未连真实库） | 200 | `ok`，且 `mode=mock` |
| 真实驱动连不上库 | 503 | `unavailable` |

**响应体字段**与 `get_connection_status` 相同，并额外包含顶层 `status`（`ok` / `unavailable`）。

**示例（mock）：**

```json
{
  "status": "ok",
  "connected": true,
  "driver": "mock",
  "mode": "mock",
  "host": null,
  "port": null,
  "pools": [
    {"pool": "read", "connected": true, "latency_ms": 0.1, "error": null},
    {"pool": "write", "connected": true, "latency_ms": 0.1, "error": null}
  ],
  "message": "当前运行在 mock 模式，未连接真实 StarRocks；服务可用，返回的是构造数据。"
}
```

### 2. `GET /metrics`

Prometheus 指标（`Content-Type: text/plain`）。主要指标：

| 指标 | 类型 | 标签 |
|------|------|------|
| `mcp_sql_requests_total` | Counter | `api_key_id`, `statement_type`, `status` |
| `mcp_sql_duration_seconds` | Histogram | `api_key_id`, `statement_type` |
| `mcp_sql_rows_returned` | Histogram | `api_key_id` |
| `mcp_pool_in_use` | Gauge | `pool`（`read` / `write`） |

其中 `status` 常见取值：`success` / `rejected` / `timeout` / `error` / `disconnected`。

### 3. 审计日志（SQLite）

每次 `execute_sql` 调用会写入本地 SQLite（默认 `logs/audit.db`），**直接记录用户名**（来自用户↔apikey 映射）：

```sql
SELECT ts, username, api_key_id, statement_type, sql_text, duration_ms, status, error
FROM request_log
WHERE username = 'alice'
ORDER BY id DESC LIMIT 20;
```

| 字段 | 说明 |
|------|------|
| `username` | 业务用户名（审计主字段） |
| `api_key_id` | apikey 标识（便于关联配置） |
| `statement_type` | SELECT / INSERT / DROP… |
| `sql_text` | 请求 SQL（可按配置截断） |
| `pool` | read / write |
| `duration_ms` | 耗时 |
| `row_count` | 返回行数 |
| `status` | success / rejected / timeout / disconnected / error |
| `error` | 失败原因 |

---

## 三、客户端调用示例

### MCP Python SDK（推荐）

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    headers = {"X-API-Key": "your-api-key"}
    async with streamablehttp_client("http://127.0.0.1:8080/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. 先检查是否连上数据库
            print(await session.call_tool("get_connection_status", {}))

            # 2. 结构理解
            print(await session.call_tool("get_database_info_tool", {}))
            print(await session.call_tool("get_database_info_tool", {
                "catalog": "paimon", "database": "csc", "table": "ods_csc_log_audit"
            }))

            # 3. 执行查询
            sql = (
                "select * from paimon.csc.ods_csc_log_audit "
                "where app_id='hids-identify-result-storage-svc' "
                "and year=2026 and month=202606 and day=20260610 limit 2"
            )
            print(await session.call_tool("execute_sql", {"sql": sql}))

asyncio.run(main())
```

### curl 运维探活

```bash
curl -s http://127.0.0.1:8080/healthz | jq
curl -s http://127.0.0.1:8080/metrics | head
```

---

## 四、版本与变更说明

- 当前版本：`0.1.0`（见 `pyproject.toml`）
- 相较初版新增：
  - MCP 工具 `get_connection_status`
  - `/healthz` 会真实探测数据库连通性（连不上返回 503）
  - `get_database_info_tool` / `execute_sql` 在数据库不可用时返回明确的「数据库未连接」提示
  - 启动时主动探活一次，失败会打错误日志（不阻止进程启动，便于排查）
