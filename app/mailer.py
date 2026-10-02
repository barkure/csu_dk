"""腾讯云模板邮件；未配置时仅提供本地调试验证码。"""
from __future__ import annotations

import json

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ses.v20201002 import models, ses_client

from . import config as cfg
from .errors import UpstreamError
from .log import log_event

VERIFICATION_CODE_SUBJECT = "【妙妙道具】登录验证码"
CREDENTIAL_INVALID_SUBJECT = "【妙妙道具】账号登录异常"
ENDPOINT = "ses.tencentcloudapi.com"


def mailer_enabled() -> bool:
    return bool(
        cfg.config.tencent_ses_secret_id
        and cfg.config.tencent_ses_secret_key
        and cfg.config.tencent_ses_verification_code_template_id
        and cfg.config.mail_from
    )


def credential_invalid_notice_enabled() -> bool:
    return bool(
        cfg.config.tencent_ses_secret_id
        and cfg.config.tencent_ses_secret_key
        and cfg.config.tencent_ses_credential_invalid_template_id
        and cfg.config.mail_from
    )


def _client() -> ses_client.SesClient:
    profile = ClientProfile(httpProfile=HttpProfile(endpoint=ENDPOINT, reqTimeout=15))
    return ses_client.SesClient(
        credential.Credential(cfg.config.tencent_ses_secret_id, cfg.config.tencent_ses_secret_key),
        cfg.config.tencent_ses_region,
        profile,
    )


def send_login_code(email: str, code: str) -> dict:
    if not mailer_enabled():
        log_event("mail.verification_code_debug", email=email)
        print(f"[mailer] 本地调试验证码（邮箱已隐藏）：{code}", flush=True)
        return {"sent": False, "dev_code": code}

    request = models.SendEmailRequest()
    request.FromEmailAddress = cfg.config.mail_from
    request.Destination = [email]
    request.Subject = VERIFICATION_CODE_SUBJECT
    request.TriggerType = 1  # 触发类邮件，即时投递。
    request.Template = models.Template()
    request.Template.TemplateID = cfg.config.tencent_ses_verification_code_template_id
    # 模板变量必须是字符串。
    request.Template.TemplateData = json.dumps(
        {"code": code, "minutes": str(cfg.config.code_minutes)}, ensure_ascii=False
    )

    try:
        response = _client().SendEmail(request)
    except TencentCloudSDKException as error:
        raise UpstreamError("邮件发送失败，请稍后重试") from error

    log_event("mail.verification_code_sent", email=email, message_id=response.MessageId)
    return {"sent": True, "id": response.MessageId}


def send_credential_invalid_notice(email: str, username: str) -> dict:
    """通知用户重新提交学校账号密码。"""
    if not credential_invalid_notice_enabled():
        log_event("mail.credential_invalid_skipped", email=email, reason="未配置账号异常邮件")
        return {"sent": False}

    request = models.SendEmailRequest()
    request.FromEmailAddress = cfg.config.mail_from
    request.Destination = [email]
    request.Subject = CREDENTIAL_INVALID_SUBJECT
    request.TriggerType = 1
    request.Template = models.Template()
    request.Template.TemplateID = cfg.config.tencent_ses_credential_invalid_template_id
    request.Template.TemplateData = json.dumps(
        {"username_tail": username[-4:] if username else ""}, ensure_ascii=False)

    try:
        response = _client().SendEmail(request)
    except TencentCloudSDKException as error:
        raise UpstreamError("邮件发送失败，请稍后重试") from error

    log_event("mail.credential_invalid_sent", email=email, message_id=response.MessageId)
    return {"sent": True, "id": response.MessageId}
