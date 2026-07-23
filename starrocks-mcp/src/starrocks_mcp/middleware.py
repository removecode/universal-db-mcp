"""ApiKey 鉴权中间件（纯 ASGI 中间件，避免 BaseHTTPMiddleware 缓冲 streamable-http 的流式响应）。

职责：
1. 从 HTTP Header（默认 `X-API-Key`，同时兼容 `Authorization: Bearer <key>`）取出明文 apikey。
2. 用 AuthProvider 解析出 ApiKeyPermission；解析失败直接返回 401，不进入 MCP 协议层。
3. 把解析出的 ApiKeyPermission 挂到 ASGI scope["state"] 上，供后续 MCP 工具通过
   `get_permission_from_context()` 读取，不需要每个工具重复解析 Header。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from .auth.models import ApiKeyPermission
from .auth.provider import AuthProvider

logger = logging.getLogger(__name__)

STATE_KEY = "api_key_permission"


class ApiKeyAuthMiddleware:
    """校验 apikey 并把权限对象注入请求状态的 ASGI 中间件。"""

    def __init__(
        self,
        app: ASGIApp,
        auth_provider: AuthProvider,
        header_name: str = "X-API-Key",
        exempt_path_prefixes: Iterable[str] = ("/metrics", "/healthz"),
    ) -> None:
        self.app = app
        self.auth_provider = auth_provider
        self.header_name = header_name.lower().encode("latin-1")
        self.exempt_path_prefixes = tuple(exempt_path_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if any(path.startswith(prefix) for prefix in self.exempt_path_prefixes):
            await self.app(scope, receive, send)
            return

        raw_api_key = self._extract_api_key(scope)
        permission: ApiKeyPermission | None = None
        if raw_api_key:
            permission = self.auth_provider.get_permission(raw_api_key)

        if permission is None:
            await self._reject_unauthorized(send)
            return

        state = scope.setdefault("state", {})
        state[STATE_KEY] = permission

        await self.app(scope, receive, send)

    def _extract_api_key(self, scope: Scope) -> str | None:
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        header_map = {k: v for k, v in headers}

        raw = header_map.get(self.header_name)
        if raw:
            return raw.decode("latin-1").strip()

        auth_header = header_map.get(b"authorization")
        if auth_header:
            value = auth_header.decode("latin-1").strip()
            if value.lower().startswith("bearer "):
                return value[7:].strip()
        return None

    @staticmethod
    async def _reject_unauthorized(send: Send) -> None:
        body = json.dumps(
            {"error": "unauthorized", "message": "缺少有效的 apikey，请在请求头中提供 X-API-Key"}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def get_permission_from_request_scope(scope: Scope) -> ApiKeyPermission | None:
    """从 ASGI scope 中取出鉴权中间件注入的权限对象。"""
    state = scope.get("state") or {}
    return state.get(STATE_KEY)


def get_permission_from_context(ctx: Any) -> ApiKeyPermission:
    """从 MCP 工具的 Context 对象中取出当前调用者的权限。

    `ctx.request_context.request` 在 streamable-http 传输下是原始的 Starlette
    Request 对象（同一个 ASGI scope），因此可以直接读取鉴权中间件设置的 state。
    找不到权限信息说明中间件未生效或请求未经过 HTTP 层（不应该发生），按未鉴权处理。
    """
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        raise PermissionError("无法获取请求上下文，拒绝执行（可能未通过 HTTP 层调用）")

    scope = getattr(request, "scope", None)
    permission = get_permission_from_request_scope(scope) if scope is not None else None
    if permission is None:
        raise PermissionError("未找到有效的 apikey 权限信息，拒绝执行")
    return permission
