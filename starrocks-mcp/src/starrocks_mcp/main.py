"""启动入口：加载配置，构建 ASGI 应用，用 uvicorn 跑 streamable-http 服务。"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import load_settings
from .server import build_asgi_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="starrocks-mcp", description="StarRocks MCP Server")
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="settings.yaml 配置文件路径（默认: config/settings.yaml）",
    )
    parser.add_argument("--host", default=None, help="覆盖配置文件中的监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置文件中的监听端口")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = load_settings(args.config)
    if args.host:
        settings.server.host = args.host
    if args.port:
        settings.server.port = args.port

    app, state = build_asgi_app(settings)

    logging.getLogger(__name__).info(
        "启动 starrocks-mcp: driver=%s host=%s port=%s",
        settings.database.driver,
        settings.server.host,
        settings.server.port,
    )

    try:
        uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_level=args.log_level.lower())
    finally:
        state.close()


if __name__ == "__main__":
    cli(sys.argv[1:])
