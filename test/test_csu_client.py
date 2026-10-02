"""智慧学工客户端的 Cookie 和出口切换。"""
from __future__ import annotations

import pytest
import requests

from app import config as cfg
from app.csu.zhxg import ZhxgClient, load_cookies
from app.errors import ExitUnreachableError


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


@pytest.mark.parametrize("stage", ["cas", "token", "business"])
def test_connection_failure_on_proxy_exit_cools_it_down(monkeypatch, stage):
    from app import exits

    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(
        update={"outbound_proxies": ("http://127.0.0.1:1092",)}))
    exits.reset()
    client = ZhxgClient()
    error = requests.exceptions.ConnectionError("connection reset: /cas?ticket=private-ticket&token=private-token")
    events = []
    monkeypatch.setattr(exits, "log_event", lambda event, **fields: events.append((event, fields)))

    def disconnect(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(client.session, "request", disconnect)
    operations = {
        "cas": lambda: client.login("test-student", "test-password"),
        "token": lambda: client._exchange_callback("var uid = 'test-student';"),
        "business": client.dk_status,
    }
    with pytest.raises(ExitUnreachableError) as caught:
        operations[stage]()

    assert caught.value.__cause__ is error
    assert exits.summary() == {"healthy": 1, "total": 2}
    assert exits.current() == ""
    assert [event for event, _fields in events] == ["exit.unreachable"]
    for secret in ("private-ticket", "private-token"):
        assert secret not in str(caught.value)
        assert secret not in str(events)


@pytest.mark.parametrize("stage", ["cas", "token", "business"])
def test_connection_failure_on_direct_exit_is_not_an_exit_fault(monkeypatch, stage):
    from app import exits

    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(update={"outbound_proxies": ()}))
    exits.reset()
    client = ZhxgClient()
    error = requests.exceptions.ConnectionError("Connection reset by peer")

    def disconnect(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(client.session, "request", disconnect)
    operations = {
        "cas": lambda: client.login("test-student", "test-password"),
        "token": lambda: client._exchange_callback("var uid = 'test-student';"),
        "business": client.dk_status,
    }
    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        operations[stage]()

    assert caught.value is error
    assert exits.summary() == {"healthy": 1, "total": 1}
