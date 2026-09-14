"""CAS 统一身份认证登录。密码加密与前端一致：AES-CBC(随机64位前缀 + 密码 + PKCS7)。

关键：POST 必须发到**带 service 参数**的地址，否则 CAS 不下发 ticket。
"""
from __future__ import annotations

import base64
import re
import secrets
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .. import config as cfg
from ..errors import SecretDecryptError
from ..log import log_event
from ..permissions import harden_dir, harden_file
from . import ocr

CAS_BASE = "https://ca.csu.edu.cn/authserver"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"
_AES_LENGTHS = {16: AES, 24: AES, 32: AES}

# 限制验证码重试，避免触发 IP 风控
MAX_CAPTCHA_ATTEMPTS = 2

# 支持的验证码图片类型
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"RIFF", b"II*\x00", b"MM\x00*")

# 验证码接口；时间戳用于防缓存
CAPTCHA_URL = f"{CAS_BASE}/getCaptcha.htl"


class CasIpFrozenError(Exception):
    frozen = True


class _CaptchaRejected(Exception):
    """CAS 明确说验证码不对 —— 换一张图重试，而不是当成凭据错误。"""


def _random_from(chars: str, length: int) -> str:
    return "".join(secrets.choice(chars) for _ in range(length))


def encrypt_password(password: str, salt: str) -> str:
    key = salt.encode()
    if len(key) not in (16, 24, 32):
        raise ValueError(f"pwdEncryptSalt 长度异常：{len(key)}")
    plain = (_random_from(_AES_CHARS, 64) + password).encode()
    cipher = AES.new(key, AES.MODE_CBC, _random_from(_AES_CHARS, 16).encode())
    return base64.b64encode(cipher.encrypt(pad(plain, AES.block_size))).decode()


def input_value(html: str, field_id: str) -> str | None:
    """取某个 <input id="..."> 的 value（不依赖属性顺序与属性存在性）。"""
    tag = BeautifulSoup(html, "html.parser").find("input", id=field_id)
    value = tag.get("value") if tag else None
    return str(value) if value is not None else None


def error_tip(html: str) -> str | None:
    """CAS 的错误提示区（#showErrorTip）—— 登录页正文里也有"验证码"等字样，只能认这个区域。"""
    node = BeautifulSoup(html, "html.parser").select_one("#showErrorTip")
    if not node:
        return None
    return " ".join(node.get_text(" ", strip=True).split()) or None


def detect_ip_frozen(html: str) -> CasIpFrozenError | None:
    text = str(html or "")
    if "IP冻结" in text or "已被冻结" in text:
        detail = re.search(r"您的IP[（(]([^）)]+)[）)]", text)
        suffix = f"（{detail.group(1)}）" if detail else ""
        return CasIpFrozenError(f"学校统一身份认证已冻结本机 IP{suffix}：多次无效登录会触发风控，请稍后再试")
    return None


_CAS_ERRORS = {
    "badCredentials": ("密码错误", "用户名或密码错"),
    "inactive": ("未激活",),
    "locked": ("锁定",),
    "captcha": ("验证码",),
    "sessionExpired": ("会话已失效", "会话失效"),
}


def classify_error(text: str = "") -> str | None:
    for kind, patterns in _CAS_ERRORS.items():
        if any(pattern in text for pattern in patterns):
            return kind
    return None


def _is_cas_host(url: str) -> bool:
    return url.startswith((CAS_BASE, "https://ca.csu.edu.cn/"))


def cas_login(session: requests.Session, username: str, password: str | None,
              service: str, timeout: int = 20) -> str:
    """完成 CAS 登录并跟随跳转，返回落地页 HTML（业务侧要从里面取 uid/lzc）。

    password 传 None 表示本地密码解不开：会话还有效就照样登录（CASTGC 能免密码换 ticket），
    真需要交密码时才报错，而不是一上来就把账号判死。
    """
    login_url = f"{CAS_BASE}/login?service={requests.utils.quote(service, safe='')}"

    first = session.get(login_url, headers={"user-agent": UA}, timeout=timeout)
    frozen = detect_ip_frozen(first.text)
    if frozen:
        raise frozen

    # 有效 CAS 会话可直接换取 ticket
    if not _is_cas_host(first.url):
        return first.text

    if not input_value(first.text, "execution") or not input_value(first.text, "pwdEncryptSalt"):
        raise RuntimeError("未找到 CAS 登录表单（页面结构可能变了）")

    if password is None:
        # 凭据错误会触发重新提交密码状态
        raise SecretDecryptError("CAS 会话已失效，而本地保存的密码又无法解密，请在「编辑账号」里重新提交一次密码")

    if _needs_captcha(session, username, timeout):
        return _login_with_captcha(session, login_url, username, password, timeout)

    return _submit_login(session, login_url, first.text, username, password, "", timeout)


def _needs_captcha(session: requests.Session, username: str, timeout: int) -> bool:
    """CAS 会在连续失败几次后要求验证码；查不到就当不需要（照旧走密码登录）。"""
    try:
        need = session.get(
            f"{CAS_BASE}/checkNeedCaptcha.htl",
            params={"username": username, "_": int(time.time() * 1000)},
            headers={"user-agent": UA},
            timeout=timeout,
        )
        return bool(need.ok and need.json().get("isNeed"))
    except (requests.RequestException, ValueError):
        return False


def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(magic) for magic in _IMAGE_MAGIC)


def _captcha_image(session: requests.Session, page_html: str, login_url: str,
                   timeout: int) -> tuple[bytes | None, str, str]:
    """取验证码图片，返回 (图片, 来源, 地址)。

    图片地址优先从登录页里认（学校自己在页面上写的地址最权威），
    页面上没有就用学校固定的取图接口 getCaptcha.htl。
    拿到的不是图片就当没拿到 —— 绝不把错误页当验证码提交。
    """
    src = None
    for tag in BeautifulSoup(page_html, "html.parser").find_all("img"):
        candidate = str(tag.get("src") or "")
        if "captcha" in candidate.lower():
            src = urljoin(login_url, candidate)
            break
    source = "page" if src else "fallback"
    url = src or f"{CAPTCHA_URL}?{int(time.time() * 1000)}"

    try:
        response = session.get(url, headers={"user-agent": UA, "referer": login_url}, timeout=timeout)
    except requests.RequestException as error:
        log_event("checkin.captcha_image_failed", source=source, url=url, error=str(error))
        return None, source, url

    if not response.ok or not _looks_like_image(response.content):
        log_event("checkin.captcha_image_failed", source=source, url=url,
                  status=response.status_code, bytes=len(response.content or b""))
        return None, source, url
    return response.content, source, url


def _keep_evidence(page_html: str, image: bytes | None) -> None:
    """把现场证据落到 data/captcha-debug/ 下。

    这条链路没法在测试里对着真学校验（会喂风控），真碰上了就得能回头查：
    页面里到底有没有验证码图片、取到的是不是图片、识别成了什么。
    """
    try:
        directory = cfg.DATA_DIR / "captcha-debug"
        directory.mkdir(parents=True, exist_ok=True)
        harden_dir(directory)
        page_file = directory / "last-login-page.html"
        page_file.write_text(page_html, encoding="utf-8")
        harden_file(page_file)
        if image:
            image_file = directory / "last-captcha.png"
            image_file.write_bytes(image)
            harden_file(image_file)
    except OSError as error:
        print(f"[cas] 现场证据保存失败（不影响登录）：{error}", flush=True)


def _login_with_captcha(session: requests.Session, login_url: str, username: str,
                        password: str, timeout: int) -> str:
    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        # 每轮刷新验证码和 execution
        page = session.get(login_url, headers={"user-agent": UA}, timeout=timeout)
        frozen = detect_ip_frozen(page.text)
        if frozen:
            raise frozen

        image, source, url = _captcha_image(session, page.text, login_url, timeout)
        _keep_evidence(page.text, image)
        code = ocr.solve(image) if image else None
        log_event("checkin.captcha_attempt", attempt=attempt, source=source, url=url,
                  bytes=len(image or b""), recognized=bool(code), recognized_length=len(code or ""))
        if not code:
            # 不提交无效识别结果
            raise RuntimeError(
                "CAS 要求输入验证码，但自动识别不可用（未安装 ddddocr 或识别失败），"
                "请先在浏览器登录一次后再试"
            )

        try:
            return _submit_login(session, login_url, page.text, username, password, code, timeout)
        except _CaptchaRejected:
            continue

    raise RuntimeError(
        f"CAS 验证码连续 {MAX_CAPTCHA_ATTEMPTS} 次未通过，请先在浏览器登录一次后再试"
    )


def _submit_login(session: requests.Session, login_url: str, page_html: str, username: str,
                  password: str, captcha: str, timeout: int) -> str:
    execution = input_value(page_html, "execution")
    salt = input_value(page_html, "pwdEncryptSalt")
    if not execution or not salt:
        raise RuntimeError("未找到 CAS 登录表单（页面结构可能变了）")

    posted = session.post(
        login_url,
        data={
            "username": username,
            "password": encrypt_password(password, salt),
            "captcha": captcha,
            "execution": execution,
            "_eventId": "submit",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "lt": "",
            # 延长 CASTGC 有效期
            "rememberMe": "true",
        },
        headers={"user-agent": UA, "content-type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )

    frozen_after = detect_ip_frozen(posted.text)
    if frozen_after:
        raise frozen_after

    tip = error_tip(posted.text)
    if tip:
        kind = classify_error(tip)
        if kind == "captcha":
            raise _CaptchaRejected(tip)
        if kind == "badCredentials":
            raise RuntimeError("学号或密码错误")
        if kind == "inactive":
            raise RuntimeError("账号未激活，请先在统一身份认证平台激活")
        if kind == "locked":
            raise RuntimeError("账号已被锁定，请联系学校")
        raise RuntimeError(f"CAS 登录失败：{tip}")

    if _is_cas_host(posted.url):
        text = re.sub(r"<script[\s\S]*?</script>", " ", posted.text, flags=re.IGNORECASE)
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", text))
        raise RuntimeError(f"CAS 登录未完成（仍停留在登录页）：{snippet.strip()[:160]}")

    return posted.text
