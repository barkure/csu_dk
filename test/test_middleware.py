"""安全响应头、缓存策略和请求体上限。"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import MutableHeaders

from app import auth, db
from app import config as cfg
from app import middleware as mw
from app.clock import local_now, to_local_iso
from app.main import app

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}
LIMIT = mw.MAX_BODY_BYTES


@pytest.fixture()
def client():
    for limiter in auth.limiters.values():
        limiter.reset()
    return TestClient(app)


def inject_code(email: str, code: str) -> None:
    now = local_now(cfg.config.tz)
    db.insert_login_code(email, auth.hash_login_code(email, code),
                         to_local_iso(now + timedelta(minutes=10)), to_local_iso(now))


def login(client: TestClient, email: str) -> None:
    inject_code(email, "424242")
    response = client.post("/api/auth/verify", json={"email": email, "code": "424242"})
    assert response.status_code == 200, response.text


def assert_security_headers(headers) -> None:
    for key, value in SECURITY_HEADERS.items():
        assert headers.get(key) == value, f"缺少或错误的响应头：{key}"


# 响应头

def test_cache_control_policy_by_path(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    assert client.post("/ui/code", data={"email": "mw-ui@example.com"}).headers["cache-control"] == "no-store"
    login(client, "mw-dash@example.com")
    assert client.get("/dashboard").headers["cache-control"] == "no-store"


def test_security_headers_cover_success_redirect_and_client_errors(client):
    responses = [
        client.get("/api/health"),                                   # 200
        client.get("/dashboard", follow_redirects=False),            # 303
        client.get("/api/accounts"),                                 # 401
        client.post("/api/auth/verify", json={"email": "nope", "code": "1"}),  # 400
    ]
    for response in responses:
        assert_security_headers(response.headers)


def test_unexpected_error_500_carries_headers(client, monkeypatch):
    """路由异常仍带安全响应头。"""
    inject_code("mw-500@example.com", "424242")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟建会话失败")

    monkeypatch.setattr(auth, "_insert_session", boom)
    no_raise = TestClient(app, raise_server_exceptions=False)
    response = no_raise.post("/api/auth/verify",
                             json={"email": "mw-500@example.com", "code": "424242"})
    assert response.status_code == 500
    assert response.json() == {"error": "服务端内部错误"}
    assert response.headers["cache-control"] == "no-store"
    assert_security_headers(response.headers)


def test_error_handler_crash_fallback_500_carries_headers(monkeypatch):
    """异常处理器出错时由外层中间件兜底。"""
    inject_code("mw-500x@example.com", "424242")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟错误处理器崩溃")

    monkeypatch.setattr(auth, "_insert_session", boom)
    monkeypatch.setattr("app.main._json_error", boom)
    no_raise = TestClient(app, raise_server_exceptions=False)
    response = no_raise.post("/api/auth/verify",
                             json={"email": "mw-500x@example.com", "code": "424242"})
    assert response.status_code == 500
    assert response.json() == {"error": "服务端内部错误"}
    assert response.headers["cache-control"] == "no-store"
    assert_security_headers(response.headers)


def test_error_handler_crash_still_raises_for_server_logging(monkeypatch):
    """兜底后继续抛错，保留服务器日志。"""
    inject_code("mw-500y@example.com", "424242")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟错误处理器崩溃")

    monkeypatch.setattr(auth, "_insert_session", boom)
    monkeypatch.setattr("app.main._json_error", boom)
    with pytest.raises(RuntimeError, match="模拟错误处理器崩溃"):
        TestClient(app).post("/api/auth/verify",
                             json={"email": "mw-500y@example.com", "code": "424242"})


# 请求体上限

def chunked(large: bytes):
    def gen():
        for index in range(0, len(large), 1024):
            yield large[index:index + 1024]
    return gen()


def test_declared_oversize_json_rejected_before_read(client):
    response = client.post("/api/auth/verify", json={"email": "a@b.c", "code": "x" * 200_000})
    assert response.status_code == 413
    assert response.json() == {"error": "请求体过大（上限 64KB）", "code": "payload_too_large"}
    assert response.headers["cache-control"] == "no-store"
    assert_security_headers(response.headers)


def test_declared_oversize_htmx_keeps_reswap(client):
    response = client.post("/ui/code", data={"email": "mw-hx@example.com", "extra": "x" * 200_000},
                           headers={"hx-request": "true"})
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.headers["hx-reswap"] == "none"
    assert_security_headers(response.headers)


def test_chunked_oversize_json_rejected_by_actual_bytes(client):
    response = client.post("/api/auth/verify", content=chunked(b"x" * (LIMIT + 4096)),
                           headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"error": "请求体过大（上限 64KB）", "code": "payload_too_large"}
    assert response.headers["cache-control"] == "no-store"
    assert_security_headers(response.headers)


def test_chunked_oversize_form_rejected_by_actual_bytes(client):
    response = client.post("/ui/code", content=chunked(b"x" * (LIMIT + 4096)),
                           headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert_security_headers(response.headers)


def test_chunked_undersize_json_parses_normally(client):
    inject_code("mw-chunk@example.com", "424242")
    payload = b'{"email": "mw-chunk@example.com", "code": "424242"}'
    response = client.post("/api/auth/verify", content=chunked(payload),
                           headers={"content-type": "application/json"})
    assert response.status_code == 200, response.text


def test_chunked_undersize_form_parses_normally(client):
    response = client.post("/ui/code", content=chunked(b"email=mw-form@example.com"),
                           headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 200, response.text
    assert "验证码" in response.text


# ASGI 边界

class FakeDownstream:
    """合成 ASGI 下游：读完整 body 后回 200，并记录是否被调用。"""

    def __init__(self):
        self.calls = 0
        self.bodies: list[bytes] = []

    async def __call__(self, scope, receive, send):
        self.calls += 1
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return
            body += message.get("body") or b""
            more = message.get("more_body", False)
        self.bodies.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class ReceiveSpy:
    """合成 ASGI 下游：按固定次数原样收集 receive() 消息。"""

    def __init__(self, reads: int):
        self.reads = reads
        self.seen: list[dict] = []

    async def __call__(self, scope, receive, send):
        for _ in range(self.reads):
            self.seen.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def make_scope(path: str = "/api/test", headers: dict | None = None) -> dict:
    return {
        "type": "http", "method": "POST", "path": path, "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
    }


def queue_receive(messages: list[dict], log: list | None = None) -> object:
    queue = list(messages)

    async def receive():
        if log is not None:
            log.append(True)
        assert queue, "receive() 被多调了"
        return queue.pop(0)

    return receive


def collect_send(sent: list) -> object:
    async def send(message):
        sent.append(message)

    return send


def run(app, scope, receive, sent, timeout: float = 2.0) -> None:
    asyncio.run(asyncio.wait_for(app(scope, receive, collect_send(sent)), timeout))


def test_exact_limit_allowed_and_replayed_once():
    downstream = FakeDownstream()
    sent: list = []
    messages = [
        {"type": "http.request", "body": b"a" * (LIMIT - 1), "more_body": True},
        {"type": "http.request", "body": b"b", "more_body": False},
        {"type": "http.disconnect"},
    ]
    run(mw.BodyLimitMiddleware(downstream), make_scope(), queue_receive(messages), sent)
    assert downstream.calls == 1
    assert downstream.bodies == [b"a" * (LIMIT - 1) + b"b"]
    assert sent and sent[0]["status"] == 200


def test_one_byte_over_limit_rejected():
    downstream = FakeDownstream()
    sent: list = []
    messages = [{"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}]
    run(mw.BodyLimitMiddleware(downstream), make_scope(), queue_receive(messages), sent)
    assert downstream.calls == 0
    assert sent[0]["status"] == 413


def test_body_without_transfer_headers_still_counted():
    """无 content-length/transfer-encoding 头的请求同样受实际字节限制。"""
    downstream = FakeDownstream()
    sent: list = []
    chunk = b"x" * (LIMIT // 2 + 1)
    messages = [
        {"type": "http.request", "body": chunk, "more_body": True},
        {"type": "http.request", "body": chunk, "more_body": False},
    ]
    scope = make_scope()
    assert not any(key in (b"content-length", b"transfer-encoding")
                   for key, _ in scope["headers"])
    run(mw.BodyLimitMiddleware(downstream), scope, queue_receive(messages), sent)
    assert downstream.calls == 0
    assert sent[0]["status"] == 413


def test_declared_small_but_actual_over_rejected():
    downstream = FakeDownstream()
    sent: list = []
    messages = [
        {"type": "http.request", "body": b"x" * 40_000, "more_body": True},
        {"type": "http.request", "body": b"x" * 40_000, "more_body": False},
    ]
    scope = make_scope(headers={"content-length": "10"})
    run(mw.BodyLimitMiddleware(downstream), scope, queue_receive(messages), sent)
    assert downstream.calls == 0
    assert sent[0]["status"] == 413


def test_multi_and_empty_chunks_reassemble():
    downstream = FakeDownstream()
    sent: list = []
    messages = [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ]
    run(mw.BodyLimitMiddleware(downstream), make_scope(), queue_receive(messages), sent)
    assert downstream.bodies == [b"abcd"]


def test_replayed_once_then_original_receive_delegated():
    spy = ReceiveSpy(reads=2)
    sent: list = []
    messages = [
        {"type": "http.request", "body": b"whole", "more_body": False},
        {"type": "http.disconnect"},
    ]
    run(mw.BodyLimitMiddleware(spy), make_scope(), queue_receive(messages), sent)
    assert spy.seen[0] == {"type": "http.request", "body": b"whole", "more_body": False}
    assert spy.seen[1] == {"type": "http.disconnect"}, "回放一次后应委托原始 receive，不重复 body"


def test_declared_over_never_calls_receive():
    sent: list = []
    read_log: list = []
    scope = make_scope(headers={"content-length": str(LIMIT + 1)})
    run(mw.BodyLimitMiddleware(FakeDownstream()), scope,
        queue_receive([], log=read_log), sent)
    assert sent[0]["status"] == 413
    assert read_log == [], "声明超限应在调用 receive() 前快速拒绝"


def test_mid_read_disconnect_skips_business():
    downstream = FakeDownstream()
    sent: list = []
    messages = [
        {"type": "http.request", "body": b"partial", "more_body": True},
        {"type": "http.disconnect"},
    ]
    run(mw.BodyLimitMiddleware(downstream), make_scope(), queue_receive(messages), sent)
    assert downstream.calls == 0, "正文残缺不得执行业务"
    assert sent == [], "客户端已断开，不强行产生响应"


def test_body_limit_413_passes_header_wrapper():
    """读取超限的 413 与声明超限同出口，T4 响应头一并带上（验证包裹顺序）。"""
    wrapped = mw.SecurityHeadersMiddleware(mw.BodyLimitMiddleware(FakeDownstream()))
    sent: list = []
    messages = [{"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}]
    scope = make_scope(path="/api/test", headers={"hx-request": "true"})
    run(wrapped, scope, queue_receive(messages), sent)
    assert sent[0]["status"] == 413
    headers = MutableHeaders(raw=sent[0]["headers"])
    assert headers["cache-control"] == "no-store"
    assert headers["hx-reswap"] == "none"
    for key, value in SECURITY_HEADERS.items():
        assert headers.get(key.lower()) == value, key


def test_non_http_scope_passes_through_untouched():
    seen: list = []

    async def inner(scope, receive, send):
        seen.append(scope)

    async def fail_receive():  # 生命周期/WS 不该被读取
        raise AssertionError("非 HTTP scope 不应调用 receive")

    for middleware_cls in (mw.SecurityHeadersMiddleware, mw.BodyLimitMiddleware):
        scope = {"type": "lifespan"}
        asyncio.run(middleware_cls(inner)(scope, fail_receive, collect_send([])))
        assert seen == [scope]
        seen.clear()
