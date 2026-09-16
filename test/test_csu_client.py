"""智学工客户端的 cookie 序列化：登录态要能原样落库与恢复。"""
from __future__ import annotations

from app.csu.zhxg import ZhxgClient, load_cookies


def test_cookie_roundtrip():
    client = ZhxgClient()
    client.session.cookies.set("CASTGC", "TGT-1234", domain="ca.csu.edu.cn", path="/authserver")
    client.session.cookies.set("JSESSIONID", "abc", domain="zhxg.csu.edu.cn", path="/")

    raw = client.cookies_json()
    restored = ZhxgClient(cookies=raw)
    assert restored.session.cookies.get("CASTGC", domain="ca.csu.edu.cn") == "TGT-1234"
    assert restored.session.cookies.get("JSESSIONID", domain="zhxg.csu.edu.cn") == "abc"


def test_cookie_domains_do_not_leak():
    client = ZhxgClient()
    client.session.cookies.set("CASTGC", "TGT-1234", domain="ca.csu.edu.cn", path="/")
    restored = ZhxgClient(cookies=client.cookies_json())
    prepared = restored.session.prepare_request(__import__("requests").Request("GET", "https://zhxg.csu.edu.cn/"))
    assert "CASTGC" not in restored.session.cookies.get_dict(domain="zhxg.csu.edu.cn")
    assert prepared.headers.get("cookie", "").find("CASTGC") == -1


def test_invalid_cookie_data_is_ignored():
    session = __import__("requests").Session()
    load_cookies(session, '{"CASTGC": "x"}')
    assert session.cookies.get("CASTGC") is None
    load_cookies(session, "not json")
    assert session.cookies.get("CASTGC") is None


def test_switch_exit_preserves_cookies_and_rebuilds_proxies(monkeypatch):
    client = ZhxgClient()
    original = client.session
    client.session.cookies.set("CASTGC", "TGT-1234", domain="ca.csu.edu.cn", path="/authserver")
    monkeypatch.setattr("app.exits.current", lambda: "http://fallback:1091")

    client.switch_exit("http://fallback:1091")

    assert client.session is not original
    assert client.exit == "http://fallback:1091"
    assert client.session.proxies == {
        "http": "http://fallback:1091", "https": "http://fallback:1091",
    }
    assert client.session.cookies.get("CASTGC", domain="ca.csu.edu.cn") == "TGT-1234"
