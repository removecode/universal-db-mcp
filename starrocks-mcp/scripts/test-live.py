"""Live functional test against running starrocks-mcp HTTP server."""

from __future__ import annotations

import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://127.0.0.1:8080"
ALICE = {"X-API-Key": "alice-local-dev-key"}
BOB = {"X-API-Key": "bob-local-dev-key"}


def show(title: str, result) -> None:
    text = result.content[0].text if result.content else str(result)
    print(f"\n=== {title} ===")
    try:
        print(json.dumps(json.loads(text), ensure_ascii=False, indent=2)[:2500])
    except Exception:
        print(text[:2500])
    print("ERROR" if result.isError else "OK")


async def call_tool(headers: dict[str, str], name: str, args: dict | None = None):
    async with streamablehttp_client(f"{BASE}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args or {})


async def main() -> None:
    async with httpx.AsyncClient() as client:
        health = (await client.get(f"{BASE}/healthz")).json()
        print("HEALTH:", health.get("status"), health.get("mode"), "connected=", health.get("connected"))

        unauth = await client.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        print("UNAUTH STATUS:", unauth.status_code)

    async with streamablehttp_client(f"{BASE}/mcp", headers=ALICE) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

    show("get_connection_status", await call_tool(ALICE, "get_connection_status"))
    show("catalogs", await call_tool(ALICE, "get_database_info_tool"))
    show("databases in paimon", await call_tool(ALICE, "get_database_info_tool", {"catalog": "paimon"}))
    show(
        "tables in paimon.csc",
        await call_tool(ALICE, "get_database_info_tool", {"catalog": "paimon", "database": "csc"}),
    )

    sql = (
        "select * from paimon.csc.ods_csc_log_audit "
        "where app_id='hids-identify-result-storage-svc' and year=2026 "
        "and month=202606 and day=20260610 limit 2"
    )
    show("execute_sql (alice read)", await call_tool(ALICE, "execute_sql", {"sql": sql}))
    show(
        "alice INSERT blocked",
        await call_tool(ALICE, "execute_sql", {"sql": "INSERT INTO paimon.csc.ods_csc_log_audit VALUES (1)"}),
    )
    show(
        "bob UPDATE allowed",
        await call_tool(BOB, "execute_sql", {"sql": "UPDATE dw.orders SET status='paid' WHERE order_id=1001"}),
    )


if __name__ == "__main__":
    asyncio.run(main())
