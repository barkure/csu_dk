"""统一安全响应头和请求体大小限制。"""
from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 64 * 1024
BODY_TOO_LARGE_MESSAGE = "请求体过大（上限 64KB）"

# 含账号状态、登录验证码的页面不允许落缓存；其余可条件缓存
_NO_STORE_PREFIXES = ("/api/", "/ui/")
_NO_STORE_PATHS = ("/dashboard",)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    # 模板无内联脚本/样式事件，htmx 4 无 eval；style 放行内联是给动态样式留余地
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


def cache_control_for(path: str) -> str:
    """按路径定缓存策略：敏感页面 no-store，其余 no-cache。"""
    if path.startswith(_NO_STORE_PREFIXES) or path in _NO_STORE_PATHS:
        return "no-store"
    return "no-cache"


class SecurityHeadersMiddleware:
    """注入缓存和安全响应头；兜底异常也带头返回后继续抛出。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(raw=message["headers"])
                for key, value in _SECURITY_HEADERS.items():
                    headers[key] = value
                headers["cache-control"] = cache_control_for(scope.get("path", ""))
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if response_started:
                raise
            await JSONResponse({"error": "服务端内部错误"}, status_code=500)(
                scope, receive, send_with_headers)
            raise


def _declared_length(scope: Scope) -> int | None:
    """读 Content-Length 声明；缺失或非法返回 None，退回实测计数。"""
    raw = (Headers(scope=scope).get("content-length") or "").strip()
    return int(raw) if raw.isdigit() else None


async def _send_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    """413 与普通响应走同一个 send 出口，缓存/安全响应头由外层统一注入。"""
    headers = {"HX-Reswap": "none"} if Headers(scope=scope).get("hx-request") else None
    response = JSONResponse({"error": BODY_TOO_LARGE_MESSAGE, "code": "payload_too_large"},
                            status_code=413, headers=headers)
    await response(scope, receive, send)


class BodyLimitMiddleware:
    """按实际读取字节数限流，在路由解析前统一返回 413。"""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _send_too_large(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                # 真实断连：正文残缺就不再执行业务，也不伪造响应
                return
            chunk = message.get("body") or b""
            total += len(chunk)
            if total > self.max_bytes:
                await _send_too_large(scope, receive, send)
                return
            if chunk:
                chunks.append(chunk)
            more = message.get("more_body", False)

        body = b"".join(chunks)
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
