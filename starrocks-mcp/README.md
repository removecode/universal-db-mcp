# starrocks-mcp

面向 StarRocks 的 MCP（Model Context Protocol）服务：作为 LLM 查询 StarRocks 的统一入口。

- 常驻多租户服务，通过 **Streamable HTTP** 暴露给多个 LLM 客户端，每个客户端用 HTTP Header 里的 **apikey** 鉴权。
- 数据库侧使用**只读 / 读写两个账号**，按 apikey 角色路由到不同连接池，apikey 本身不知道、也拿不到真实的数据库账号密码。
- 对外暴露两个 MCP 工具：
  - `get_database_info_tool`：结构理解，帮助 LLM 知道有哪些 catalog / database / table / column。
  - `execute_sql`：真正执行 SQL，内置安全审计（角色白名单、访问范围校验、LIMIT 保护）。
- 内置 Prometheus 监控指标（`/metrics`）和按 apikey 记录的 SQLite 审计日志。
- 当前环境没有真实 StarRocks 时，可以用内置的 Mock 数据源跑通完整链路（含开发、调试、自动化测试）。

## 快速开始（Mock 模式，无需真实 StarRocks）

```bash
cd starrocks-mcp
pip install -e ".[dev]"

cp config/settings.example.yaml config/settings.yaml
cp config/apikeys.example.yaml config/apikeys.yaml
```

`config/settings.yaml` 里保持 `database.driver: mock` 即可跑通全流程（不会连接任何真实网络）。

生成一个测试用的 apikey 哈希，填进 `config/apikeys.yaml`：

```bash
python3 -c "import hashlib; print('sha256:' + hashlib.sha256(b'my-test-key').hexdigest())"
```

启动服务：

```bash
python3 -m starrocks_mcp.main --config config/settings.yaml
```

用官方 MCP Python SDK 的客户端验证（示例基于用户提供的真实查询）：

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    headers = {"X-API-Key": "my-test-key"}
    async with streamablehttp_client("http://127.0.0.1:8080/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 结构理解：一路下钻 catalog -> database -> table -> columns
            print(await session.call_tool("get_database_info_tool", {}))
            print(await session.call_tool("get_database_info_tool", {"catalog": "paimon"}))
            print(await session.call_tool("get_database_info_tool", {"catalog": "paimon", "database": "csc"}))

            # 执行查询
            sql = (
                "select * from paimon.csc.ods_csc_log_audit "
                "where app_id='hids-identify-result-storage-svc' and year=2026 "
                "and month=202606 and day=20260610 limit 2"
            )
            print(await session.call_tool("execute_sql", {"sql": sql}))

asyncio.run(main())
```

## 接入真实 StarRocks

把 `config/settings.yaml` 里的 `database.driver` 改成 `pymysql`，并填好连接信息：

```yaml
database:
  driver: pymysql
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

密码建议通过环境变量注入（`${VAR_NAME}` 占位符会在加载配置时被替换），不要把明文密码提交到版本库。

StarRocks 侧建议准备两个账号：

```sql
-- 只读账号：只能 SELECT
CREATE USER 'reader'@'%' IDENTIFIED BY 'xxx';
GRANT SELECT ON *.* TO 'reader'@'%';

-- 读写账号：额外允许 INSERT/UPDATE/DELETE（DDL 不建议授权给这个账号，
-- 因为 starrocks-mcp 的安全审计本身也永远不会执行 DDL）
CREATE USER 'writer'@'%' IDENTIFIED BY 'yyy';
GRANT SELECT, INSERT, UPDATE, DELETE ON *.* TO 'writer'@'%';
```

## apikey 权限配置

`config/apikeys.yaml` 是 apikey → 权限 的映射表，key 用 apikey 的 **sha256 哈希**（不落地明文）：

```yaml
api_keys:
  "sha256:<hash>":
    id: bi-team-readonly     # 展示名，会出现在监控指标和审计日志里
    role: read                # read | readwrite
    scope:                    # 限制可访问的 catalog/database；null 表示不限制
      - catalog: paimon
        database: csc
    rate_limit_per_min: 120   # 预留字段，当前版本未实现限流
```

`AuthProvider` 是可插拔接口（`src/starrocks_mcp/auth/provider.py`），v1 只提供 YAML 实现。如果要接入已有的用户/权限系统，实现同样的 `get_permission(raw_api_key) -> ApiKeyPermission | None` 接口即可，不需要改动鉴权中间件和 MCP 工具代码。

## 两个 MCP 工具

### `get_database_info_tool`（结构理解）

参数均可选，按层级传入：

| 传参 | 返回 |
|---|---|
| 不传 | 当前 apikey 可见的所有 catalog |
| `catalog` | 该 catalog 下的所有 database |
| `catalog` + `database`（或只传 `database`，默认 catalog 为 `default_catalog`） | 该 database 下的所有 table |
| `catalog` + `database` + `table` | 该表的列结构；StarRocks 内部表还会带建表语句（含分区/分桶信息），外部 catalog 的表只有列信息 |

StarRocks 支持多 catalog（内部 `default_catalog` + 外部 catalog，比如接入 Paimon/Hive），命名方式是三段式 `catalog.database.table`（类似 Trino）。所以这个工具不是"单库模型"，而是按层级导航；不同 catalog 的能力不同——外部 catalog 通常没有 StarRocks 原生的主键/分区/分桶概念。

### `execute_sql`（执行）

参数：`sql`（必填）、`max_rows`（可选，只能收紧不能突破服务端上限）、`timeout_seconds`（同上）。

执行前的安全审计流程：

1. 只允许单条语句，拒绝任何形式的堆叠。
2. 按语句类型分类（`SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `INSERT` / `UPDATE` / `DELETE` / 其他）。
3. 按 apikey 角色校验：`read` 只能执行前四种；`readwrite` 额外可以 `INSERT`/`UPDATE`/`DELETE`。
4. 任何角色都禁止 DDL/DCL（`DROP`/`TRUNCATE`/`ALTER`/`CREATE`/`GRANT`/`REVOKE`/`LOAD`/`RENAME`/`KILL`/`SET`），v1 没有覆盖开关。
5. 校验 SQL 引用的表是否在 apikey 的访问范围（`scope`）内；这是 best-effort 的正则提取，**无法可靠判断时按 fail-closed 策略拒绝**（可通过 `security.fail_closed_on_unresolvable_scope` 关闭，不建议）。
6. `SELECT` 语句强制 LIMIT 保护：没有 `LIMIT` 自动注入默认值（`security.default_row_limit`），超过硬上限（`security.max_row_limit`）会被收紧。

审计通过后，`SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN` 一律走只读连接池（即使调用方是 `readwrite` key，也不占用读写池的连接数），`INSERT`/`UPDATE`/`DELETE` 走读写连接池。

## 监控与审计

- Prometheus 指标（`GET /metrics`，不需要 apikey）：
  - `mcp_sql_requests_total{api_key_id,statement_type,status}`
  - `mcp_sql_duration_seconds{api_key_id,statement_type}`
  - `mcp_sql_rows_returned{api_key_id}`
  - `mcp_pool_in_use{pool}`
- 按 apikey 的请求记录落地在本地 SQLite（`monitoring.audit_db_path`，默认 `logs/audit.db`），表 `request_log`，可以直接用 SQL 查某个 apikey 最近的调用：

  ```sql
  SELECT ts, statement_type, sql_text, duration_ms, row_count, status, error
  FROM request_log
  WHERE api_key_id = 'bi-team-readonly'
  ORDER BY id DESC LIMIT 20;
  ```

- 健康检查：`GET /healthz`（不需要 apikey）。

## 并发模型 / 性能

默认实现：同步 `pymysql` + `DBUtils.PooledDB`（只读、读写各一个连接池）+ 独立线程池（`database.executor_max_workers`），通过 `asyncio.loop.run_in_executor` 桥接进 FastMCP 的异步处理流程，用 `asyncio.wait_for` 施加超时。这个组合实现简单、稳定，能覆盖中等并发（几十到上百并发查询）。

已知限制：`asyncio.wait_for` 超时后只是让调用方提前拿到超时错误，工作线程里的 SQL 可能仍在数据库侧继续运行，直到底层驱动/数据库自身超时。生产环境建议同时在数据库侧也配置查询超时作为兜底。

**后续性能优化方向**：如果实测 QPS 更高、线程调度成为瓶颈，可以把 `src/starrocks_mcp/db/pymysql_pool.py` 换成基于 `asyncmy`（原生异步 MySQL 协议驱动，兼容 StarRocks）的实现——`ConnectionPool` 接口（`src/starrocks_mcp/db/base.py`）保持不变，不需要改动安全审计、MCP 工具、监控等上层代码。

## 项目结构

```
starrocks-mcp/
  src/starrocks_mcp/
    config.py              # 加载 settings.yaml
    auth/                  # apikey -> 权限 的解析（可插拔 Provider）
    db/                    # 连接池抽象、mock 实现、pymysql 真实实现、线程池桥接
    security/audit.py      # SQL 安全审计
    schema/catalog.py      # get_database_info 的分层查询逻辑
    monitoring/            # Prometheus 指标 + SQLite 审计日志
    handlers.py            # 两个工具的业务逻辑（不依赖 FastMCP Context，方便单测）
    middleware.py           # ApiKey 鉴权的 ASGI 中间件
    server.py               # 组装 FastMCP + 中间件 + 监控端点，得到最终 ASGI 应用
    main.py                 # CLI 启动入口
  tests/                    # pytest 单测（全部基于 mock 数据源，无需真实网络）
  config/                   # 配置示例（*.example.yaml）
```

## 运行测试

```bash
pip install -e ".[dev]"
pytest -v
```

测试全部基于 `MockConnectionPool`，不依赖真实 StarRocks 或网络，包含：

- `test_auth.py`：YAML 权限解析、热重载
- `test_audit.py`：语句分类、角色规则、LIMIT 保护、范围校验、多语句拒绝
- `test_schema_tool.py`：`get_database_info` 四种参数组合
- `test_execute_tool.py`：`execute_sql` 端到端行为、监控与审计日志
- `test_metrics_and_log.py`：Prometheus 指标、SQLite 审计日志模块单测
- `test_app_integration.py`：完整 ASGI 应用（鉴权中间件 + FastMCP streamable-http）进程内集成测试
