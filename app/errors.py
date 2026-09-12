"""错误类型：用稳定的 code 判断该不该让用户动手，不要匹配错误文案。"""


class AppError(Exception):
    status = 500
    code = "internal"
    expose = False

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, expose: bool | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        if expose is not None:
            self.expose = expose


class BadRequestError(AppError):
    status = 400
    expose = True

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message, code=code)


class ForbiddenError(AppError):
    status = 403
    expose = True

    def __init__(self, message: str, code: str = "forbidden"):
        super().__init__(message, code=code)


class RateLimitError(AppError):
    status = 429
    expose = True
    code = "rate_limited"

    def __init__(self, message: str, retry_after_sec: int = 60):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class InvalidTimeError(BadRequestError):
    def __init__(self, message: str):
        super().__init__(message, "invalid_time")


class MasterKeyMissingError(AppError):
    code = "master_key_missing"

    def __init__(self, path):
        super().__init__(f"加密密钥不存在：{path}")


class MasterKeyInvalidError(AppError):
    code = "master_key_invalid"

    def __init__(self, path, detail: str | None):
        super().__init__(f"加密密钥内容非法（{detail}）：{path}")


class SecretDecryptError(AppError):
    code = "secret_decrypt_failed"
    expose = True

    def __init__(self, message: str = "本地保存的密码无法解密（加密密钥不匹配或数据损坏）"):
        super().__init__(message)


class ConfigError(AppError):
    code = "config_invalid"

    def __init__(self, message: str):
        super().__init__(message)


class UpstreamError(AppError):
    """上游（邮件服务、学校接口）出错：原因可以说给用户听。"""

    status = 502
    expose = True
    code = "upstream_failed"


_FRIENDLY = {
    "master_key_missing": "服务端加密密钥缺失，请按启动日志里的说明恢复 master.key 后重启",
    "master_key_invalid": "服务端加密密钥内容非法，请查看启动日志",
    "secret_decrypt_failed": "本地保存的密码无法解密，请在「编辑账号」里重新提交一次密码",
    "config_invalid": "服务端配置有误，请查看启动日志",
}


def friendly_message(error: BaseException) -> str | None:
    if isinstance(error, AppError):
        return error.message if error.expose else _FRIENDLY.get(error.code, "服务端内部错误")
    return None
