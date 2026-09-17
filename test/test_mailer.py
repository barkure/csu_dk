"""邮件发送。"""
from __future__ import annotations

from types import SimpleNamespace

from app import config as cfg
from app import mailer


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
