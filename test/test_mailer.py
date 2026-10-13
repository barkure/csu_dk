"""邮件发送。"""
from __future__ import annotations

import io
import json

import pytest
import requests
import structlog

from app import config as cfg
from app import log, mailer
from app.errors import UpstreamError


@pytest.fixture()
def log_output(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(log, "_logger", structlog.wrap_logger(structlog.PrintLogger(stream)))
    return stream


class _Response:
    def __init__(self, status_code: int, payload: object, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _enable(monkeypatch, **updates):
    config = cfg.config.model_copy(update={
        "plunk_secret_key": "sk_test",
        "plunk_api_url": "https://next-api.useplunk.com",
        "mail_from": "Barkure <no-reply@example.com>",
        **updates,
    })
    monkeypatch.setattr(cfg, "config", config)


def _capture(monkeypatch, status_code=200, payload=None, text=""):
    captured = []
    if payload is None:
        payload = {"success": True, "data": {"emails": [{"email": "email-id"}]}}

    def post(url, json, headers, timeout, allow_redirects):
        captured.append({"url": url, "json": json, "headers": headers,
                         "timeout": timeout, "allow_redirects": allow_redirects})
        return _Response(status_code, payload, text)

    monkeypatch.setattr(mailer.requests, "post", post)
    return captured


def test_login_code_sends_project_template_without_contact_data(monkeypatch):
    _enable(monkeypatch)
    captured = _capture(monkeypatch)

    assert mailer.send_login_code("user@example.com", "4<24242") == {"sent": True, "id": "email-id"}

    request = captured[0]
    assert request["url"] == "https://next-api.useplunk.com/v1/send"
    assert request["headers"] == {"Authorization": "Bearer sk_test"}
    assert request["allow_redirects"] is False
    assert request["timeout"] == 15
    body = request["json"]
    assert body["to"] == "user@example.com"
    assert body["from"] == {"name": "Barkure", "email": "no-reply@example.com"}
    assert body["subject"] == "【妙妙道具】登录验证码"
    assert "4&lt;24242" in body["body"]
    assert "4<24242" not in body["body"]
    assert "10 分钟内有效" in body["body"]
    assert "template" not in body
    assert "data" not in body
    assert "sk_test" not in json.dumps(body)


def test_credential_notice_sends_only_username_tail(monkeypatch):
    _enable(monkeypatch, mail_from="sender@example.com")
    captured = _capture(monkeypatch)

    result = mailer.send_credential_invalid_notice("user@example.com", "7803250916")
    mailer.send_credential_invalid_notice("user@example.com", "")

    assert result == {"sent": True, "id": "email-id"}
    notice, empty = captured
    assert notice["json"]["from"] == "sender@example.com"
    assert notice["json"]["subject"] == "【妙妙道具】账号登录异常"
    assert "学号尾号 0916" in notice["json"]["body"]
    assert "7803250916" not in json.dumps(notice["json"])
    assert "学号尾号" not in empty["json"]["body"]
    assert "data" not in notice["json"]


@pytest.mark.parametrize("send_notice", [False, True], ids=["login-code", "credential-notice"])
def test_mail_errors_hide_upstream_details(monkeypatch, send_notice):
    _enable(monkeypatch)
    secret = "token=eyJsecret.payload.signature"

    def fail(*_args, **_kwargs):
        raise requests.ConnectionError(secret)

    monkeypatch.setattr(mailer.requests, "post", fail)
    send = (lambda: mailer.send_credential_invalid_notice("user@example.com", "12345678")) \
        if send_notice else (lambda: mailer.send_login_code("user@example.com", "424242"))

    with pytest.raises(UpstreamError) as exc:
        send()

    assert exc.value.message == "邮件发送失败，请稍后重试"
    assert secret not in str(exc.value)
    assert isinstance(exc.value.__cause__, requests.ConnectionError)


def test_http_error_body_is_not_attached(monkeypatch):
    _enable(monkeypatch)
    _capture(monkeypatch, status_code=401, text="sk_test leaked in body")

    with pytest.raises(UpstreamError) as exc:
        mailer.send_login_code("user@example.com", "424242")

    assert exc.value.__cause__ is None
    assert "sk_test" not in str(exc.value)
    assert "leaked" not in str(exc.value)


@pytest.mark.parametrize("send_notice", [False, True], ids=["login-code", "credential-notice"])
def test_success_logs_hide_mail_recipient_and_content(monkeypatch, log_output, capsys, send_notice):
    _enable(monkeypatch)
    _capture(monkeypatch, payload={"success": True, "data": {"emails": [{"email": "message-id"}]}})

    if send_notice:
        mailer.send_credential_invalid_notice("user@example.com", "8301210402")
    else:
        mailer.send_login_code("user@example.com", "424242")

    text = log_output.getvalue() + capsys.readouterr().out
    for secret in ("user@example.com", "8301210402", "424242", "sk_test"):
        assert secret not in text
    row = json.loads(log_output.getvalue())
    assert row["event"] == ("mail.credential_invalid_sent" if send_notice else "mail.verification_code_sent")
    assert row["email"] == "***"
    assert row["message_id"] == "message-id"


@pytest.mark.parametrize("payload", [
    {"success": False, "error": {"message": "user@example.com"}},
    {"success": True, "data": None},
    {"success": True, "data": []},
    {"success": True, "data": "invalid"},
])
def test_rejected_payload_is_a_generic_failure(monkeypatch, payload):
    _enable(monkeypatch)
    _capture(monkeypatch, payload=payload)

    with pytest.raises(UpstreamError, match="邮件发送失败"):
        mailer.send_login_code("user@example.com", "424242")


def test_debug_code_is_explicit_and_hides_recipient(log_output, capsys):
    result = mailer.send_login_code("user@example.com", "424242")

    assert result == {"sent": False, "dev_code": "424242"}
    text = capsys.readouterr().out
    assert "本地调试验证码" in text
    assert "424242" in text
    assert "user@example.com" not in text + log_output.getvalue()
    assert "424242" not in log_output.getvalue()


def test_unconfigured_notice_logs_without_recipient(log_output, capsys):
    assert mailer.send_credential_invalid_notice("user@example.com", "8301210402") == {"sent": False}
    row = json.loads(log_output.getvalue())
    assert row["event"] == "mail.credential_invalid_skipped"
    assert row["email"] == "***"
    assert capsys.readouterr().out == ""


def test_parse_mail_from_keeps_display_name():
    assert cfg.parse_mail_from("Barkure <no-reply@example.com>") == {
        "name": "Barkure", "email": "no-reply@example.com",
    }
    assert cfg.parse_mail_from("sender@example.com") == "sender@example.com"
