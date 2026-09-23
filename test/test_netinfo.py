"""请求来源判定：本机判定、限流 IP 提取，以及 Uvicorn ProxyHeadersMiddleware 与应用的组合行为。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import auth, netinfo
from app import config as cfg
from app.config import Settings
from app.main import app as fastapi_app

DEFAULT_CLIENT = ("127.0.0.1", 5000)


def make_request(*, host: str | None = "localhost", client=DEFAULT_CLIENT,
                 headers: dict[str, str] | None = None, no_client: bool = False) -> Request:
    raw = [] if host is None else [(b"host", host.encode())]
    for name, value in (headers or {}).items():
        raw.append((name.lower().encode(), value.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": raw, "client": None if no_client else client,
    })


# ---------- 本机判定 ----------

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1:8443", "[::1]", "[::1]:8443"])
def test_is_local_request_accepts_loopback_hosts(host):
    assert netinfo.is_local_request(make_request(host=host)) is True


def test_is_local_request_rejects_missing_host():
    assert netinfo.is_local_request(make_request(host=None)) is False, "缺失 Host 必须拒绝（fail closed）"
    assert netinfo.is_local_request(make_request(host="")) is False


def test_is_local_request_rejects_remote_host_or_client():
    assert netinfo.is_local_request(make_request(host="example.com")) is False
    assert netinfo.is_local_request(make_request(host="localhost", client=("203.0.113.7", 1))) is False


@pytest.mark.parametrize("headers", [
    {"x-forwarded-for": "203.0.113.9"},
    {"x-real-ip": "203.0.113.9"},
])
def test_is_local_request_rejects_proxy_headers(headers):
    assert netinfo.is_local_request(make_request(headers=headers)) is False


# ---------- 限流 IP 提取 ----------

@pytest.mark.parametrize(("client", "expected"), [
    (("203.0.113.9", 1), "203.0.113.9"),
    (("2001:db8::1", 1), "2001:db8::1"),
    (("not-an-ip", 1), "unknown"),
    (("", 1), "unknown"),
    (("   ", 1), "unknown"),
])
def test_client_ip_validates_peer_address(client, expected):
    assert netinfo.client_ip(make_request(client=client)) == expected


def test_client_ip_without_client_is_unknown():
    assert netinfo.client_ip(make_request(no_client=True)) == "unknown"


def test_client_ip_never_reads_forwarded_headers():
    request = make_request(client=("203.0.113.9", 1),
                           headers={"x-forwarded-for": "6.6.6.6", "x-real-ip": "6.6.6.6"})
    assert netinfo.client_ip(request) == "203.0.113.9"


# ---------- 配置解析 ----------

def test_trusted_proxies_split_and_dedupe(monkeypatch):
    monkeypatch.setenv("CSU_DK_TRUSTED_PROXIES", "127.0.0.1, 10.0.0.0/8,127.0.0.1")
    assert Settings().trusted_proxies == ("127.0.0.1", "10.0.0.0/8")


@pytest.mark.parametrize("value", ["*", "not-an-ip", "10.0.0.0/99", "ftp://127.0.0.1"])
def test_trusted_proxies_reject_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("CSU_DK_TRUSTED_PROXIES", value)
    with pytest.raises(ValueError, match="CSU_DK_TRUSTED_PROXIES"):
        Settings()


def test_proxy_startup_options_require_explicit_addresses(monkeypatch):
    monkeypatch.setattr(cfg.config, "trust_proxy", False)
    monkeypatch.setattr(cfg.config, "trusted_proxies", ())
    assert netinfo.proxy_startup_options() == {"proxy_headers": False}

    monkeypatch.setattr(cfg.config, "trust_proxy", True)
    assert netinfo.proxy_startup_options() == {"proxy_headers": False}, \
        "没有明确代理地址就不信任（fail closed）"

    monkeypatch.setattr(cfg.config, "trusted_proxies", ("127.0.0.1", "10.0.0.0/8"))
    assert netinfo.proxy_startup_options() == {
        "proxy_headers": True, "forwarded_allow_ips": "127.0.0.1,10.0.0.0/8"}

    monkeypatch.setattr(cfg.config, "trust_proxy", False)
    assert netinfo.proxy_startup_options() == {"proxy_headers": False}, "显式关闭时不信任"


# ---------- ProxyHeadersMiddleware × 应用 组合 ----------

@pytest.fixture()
def seen_ips(monkeypatch):
    seen: list[str] = []

    def fake_request_login_code(_email, ip="unknown"):
        seen.append(ip)
        return {"sent": True}

    monkeypatch.setattr(auth, "request_login_code", fake_request_login_code)
    return seen


def capture_ip(seen_ips, client: TestClient, headers=None) -> str:
    response = client.post("/api/auth/request-code", json={"email": "compose@example.com"},
                           headers=headers)
    assert response.status_code == 200, response.text
    assert seen_ips, "未捕获到 client_ip"
    return seen_ips[-1]


def test_composition_trusted_proxy_ignores_forged_prefix(seen_ips):
    stack = ProxyHeadersMiddleware(fastapi_app, trusted_hosts="127.0.0.1")
    client = TestClient(stack, client=("127.0.0.1", 40000))
    got = capture_ip(seen_ips, client, headers={"x-forwarded-for": "6.6.6.6, 203.0.113.9"})
    assert got == "203.0.113.9", "单层代理只采信追加后的来源，忽略伪造的左侧前缀"


def test_composition_untrusted_peer_ignores_forwarded_header(seen_ips):
    stack = ProxyHeadersMiddleware(fastapi_app, trusted_hosts="127.0.0.1")
    client = TestClient(stack, client=("198.51.100.7", 40000))
    got = capture_ip(seen_ips, client, headers={"x-forwarded-for": "6.6.6.6"})
    assert got == "198.51.100.7", "不可信对端的 XFF 一律不采信"


def test_composition_multi_proxy_resolves_each_real_client(seen_ips):
    stack = ProxyHeadersMiddleware(fastapi_app, trusted_hosts=["127.0.0.1", "10.0.0.0/8"])
    client = TestClient(stack, client=("10.0.0.2", 40000))

    for forwarded, expected in [("203.0.113.5, 10.0.0.3", "203.0.113.5"),
                                ("198.51.100.9, 10.0.0.3", "198.51.100.9")]:
        got = capture_ip(seen_ips, client, headers={"x-forwarded-for": forwarded})
        assert got == expected, "多层代理取第一个不可信来源，不能都归并到末端代理"


def test_composition_trust_disabled_ignores_forwarded_header(seen_ips):
    client = TestClient(fastapi_app, client=("203.0.113.50", 40000))
    got = capture_ip(seen_ips, client, headers={"x-forwarded-for": "6.6.6.6"})
    assert got == "203.0.113.50", "关闭信任（无中间件）时伪造 XFF 不改变限流 IP"


def test_composition_blank_invalid_and_duplicate_headers(seen_ips):
    stack = ProxyHeadersMiddleware(fastapi_app, trusted_hosts="127.0.0.1")
    client = TestClient(stack, client=("127.0.0.1", 40000))

    blank = capture_ip(seen_ips, client, headers={"x-forwarded-for": "   "})
    assert blank == "127.0.0.1", "空白 XFF 不改写来源"
    garbage = capture_ip(seen_ips, client, headers={"x-forwarded-for": "not-an-ip"})
    assert garbage == "unknown", "异常格式归并到 unknown，不成为可切换限流键"
    duplicate = capture_ip(seen_ips, client,
                           headers=[("x-forwarded-for", "6.6.6.6"), ("x-forwarded-for", "203.0.113.9")])
    assert duplicate == "203.0.113.9", "重复头与合并后的单头等价处理"
