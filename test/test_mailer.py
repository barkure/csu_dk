"""邮件发送。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException

from app import config as cfg
from app import mailer
from app.errors import UpstreamError


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
