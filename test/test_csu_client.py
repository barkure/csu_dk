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

    # requests 自己按域匹配，别把 CAS 票据发给业务域
    prepared = restored.session.prepare_request(__import__("requests").Request("GET", "https://zhxg.csu.edu.cn/"))
    assert "CASTGC" not in restored.session.cookies.get_dict(domain="zhxg.csu.edu.cn")
    assert prepared.headers.get("cookie", "").find("CASTGC") == -1


def test_foreign_cookie_format_is_ignored():
    """早期只存 {name: value}，这种没有域信息，宁可不恢复（下次自动重登）。"""
    session = __import__("requests").Session()
    load_cookies(session, '{"CASTGC": "x"}')
    assert session.cookies.get("CASTGC") is None
    load_cookies(session, "not json")
    assert session.cookies.get("CASTGC") is None
