"""登录验证码邮件（腾讯云邮件推送 SES）。

配置不全时不发信，把验证码打到日志 —— 本地调试与测试环境都走这条路。
正文在腾讯云控制台的模板里（变量 {{code}} / {{minutes}}），代码只负责填变量。
"""
from __future__ import annotations

import json

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ses.v20201002 import models, ses_client

from . import config as cfg
from .errors import UpstreamError

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
        print("[mailer] 未配置腾讯云邮件推送 —— 本地调试模式", flush=True)
        print(f"[mailer] 发给 {email} 的验证码：{code}\n", flush=True)
        return {"sent": False, "dev_code": code}

    request = models.SendEmailRequest()
    request.FromEmailAddress = cfg.config.mail_from          # 别名 + 一个空格 + <地址>
    request.Destination = [email]
    request.Subject = VERIFICATION_CODE_SUBJECT
    request.TriggerType = 1                                  # 1 = 触发类（验证码），走即时投递通道
    request.Template = models.Template()
    request.Template.TemplateID = cfg.config.tencent_ses_verification_code_template_id
    # 变量值必须是字符串：传数字会被腾讯云判为参数错误
    request.Template.TemplateData = json.dumps(
        {"code": code, "minutes": str(cfg.config.code_minutes)}, ensure_ascii=False
    )

    try:
        response = _client().SendEmail(request)
    except TencentCloudSDKException as error:
        raise UpstreamError(f"邮件发送失败：{error}") from error

    print(f"[mailer] 已发送验证码邮件 -> {email}（id={response.MessageId}）", flush=True)
    return {"sent": True, "id": response.MessageId}


def send_credential_invalid_notice(email: str, username: str) -> dict:
    """通知用户重新提交学校账号密码。"""
    if not credential_invalid_notice_enabled():
        print(f"[mailer] 未配置账号异常模板，未发送通知 -> {email}", flush=True)
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
        raise UpstreamError(f"邮件发送失败：{error}") from error

    print(f"[mailer] 已发送账号异常通知 -> {email}（id={response.MessageId}）", flush=True)
    return {"sent": True, "id": response.MessageId}
