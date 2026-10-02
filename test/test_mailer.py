"""邮件发送。"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
import structlog
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException

from app import config as cfg
from app import log, mailer
from app.errors import UpstreamError


@pytest.fixture()
def log_output(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(log, "_logger", structlog.wrap_logger(structlog.PrintLogger(stream)))
    return stream


def test_credential_invalid_notice_uses_template_and_only_sends_username_tail(monkeypatch):
    config = cfg.config.model_copy(update={
        "tencent_ses_secret_id": "id",
        "tencent_ses_secret_key": "key",
        "tencent_ses_credential_invalid_template_id": 218941,
        "mail_from": "Barkure <no-reply@example.com>",
    })
    requests = []
    client = SimpleNamespace(SendEmail=lambda request: (
        requests.append(request) or SimpleNamespace(MessageId="message-id")))
    monkeypatch.setattr(cfg, "config", config)
    monkeypatch.setattr(mailer, "_client", lambda: client)

    result = mailer.send_credential_invalid_notice("user@example.com", "7803250916")

    assert result == {"sent": True, "id": "message-id"}
    request = requests[0]
    assert request.Subject == "【妙妙道具】账号登录异常"
    assert request.Destination == ["user@example.com"]
    assert request.Template.TemplateID == 218941
    assert request.Template.TemplateData == '{"username_tail": "0916"}'
    assert "7803250916" not in request.Template.TemplateData


@pytest.mark.parametrize("send_notice", [False, True], ids=["login-code", "credential-notice"])
def test_mail_errors_hide_sdk_details(monkeypatch, send_notice):
    config = cfg.config.model_copy(update={
        "tencent_ses_secret_id": "id",
        "tencent_ses_secret_key": "key",
        "tencent_ses_verification_code_template_id": 100,
        "tencent_ses_credential_invalid_template_id": 200,
        "mail_from": "Barkure <no-reply@example.com>",
    })
    monkeypatch.setattr(cfg, "config", config)

    def fail(_request):
        raise TencentCloudSDKException("InternalError", "token=eyJsecret.payload.signature")

    monkeypatch.setattr(mailer, "_client", lambda: SimpleNamespace(SendEmail=fail))
    send = (lambda: mailer.send_credential_invalid_notice("user@example.com", "12345678")) \
        if send_notice else (lambda: mailer.send_login_code("user@example.com", "424242"))

    with pytest.raises(UpstreamError) as exc:
        send()

    assert exc.value.message == "邮件发送失败，请稍后重试"
    assert "eyJsecret" not in str(exc.value)
    assert isinstance(exc.value.__cause__, TencentCloudSDKException)


@pytest.mark.parametrize("send_notice", [False, True], ids=["login-code", "credential-notice"])
def test_success_logs_hide_mail_recipient_and_content(monkeypatch, log_output, capsys, send_notice):
    monkeypatch.setattr(cfg, "config", cfg.config.model_copy(update={
        "tencent_ses_secret_id": "id", "tencent_ses_secret_key": "key",
        "tencent_ses_verification_code_template_id": 100,
        "tencent_ses_credential_invalid_template_id": 200,
        "mail_from": "sender@example.com",
    }))
    monkeypatch.setattr(mailer, "_client", lambda: SimpleNamespace(
        SendEmail=lambda _request: SimpleNamespace(MessageId="message-id")))

    if send_notice:
        mailer.send_credential_invalid_notice("user@example.com", "8301210402")
    else:
        mailer.send_login_code("user@example.com", "424242")

    text = log_output.getvalue() + capsys.readouterr().out
    for secret in ("user@example.com", "8301210402", "424242"):
        assert secret not in text
    row = json.loads(log_output.getvalue())
    assert row["event"] == ("mail.credential_invalid_sent" if send_notice else "mail.verification_code_sent")
    assert row["email"] == "***"
    assert row["message_id"] == "message-id"


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
