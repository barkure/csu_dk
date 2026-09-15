"""验证码识别链路：绝不真打学校，更绝不拿空验证码去提交（每次失败登录都在喂学校风控）。"""
from __future__ import annotations

import pytest

from app.checkin import _CREDENTIAL_ERRORS as checkin_credential_errors
from app.checkin import NEEDS_RECREDENTIALS
from app.csu import cas, ocr
from app.errors import SecretDecryptError

LOGIN_URL = f"{cas.CAS_BASE}/login?service=x"
LANDING = "<html>业务系统首页</html>"

PAGE = """
<html><body><form>
<input id="execution" value="e1s1">
<input id="pwdEncryptSalt" value="abcdefghijklmnop">
<img id="captchaImg" src="/authserver/captcha?ts=1">
</form></body></html>
"""
PAGE_WITHOUT_IMAGE = """
<html><body><form>
<input id="execution" value="e1s1">
<input id="pwdEncryptSalt" value="abcdefghijklmnop">
</form></body></html>
"""

CAPTCHA_TIP = '<html><div id="showErrorTip">验证码错误</div></html>'


class FakeResponse:
    def __init__(self, text: str = "", url: str = LOGIN_URL, content: bytes = b"",
                 payload: dict | None = None, ok: bool = True, status_code: int = 200):
        self.text = text
        self.url = url
        self.content = content
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class FakeSession:

    def __init__(self, need_captcha: bool, login_pages: list[str], posts: list[FakeResponse],
                 first_url: str = LOGIN_URL):
        self.first_url = first_url
        self.need_captcha = need_captcha
        self.login_pages = list(login_pages)
        self.posts = list(posts)
        self.posted: list[dict] = []
        self.image_hits = 0

    def get(self, url: str, **_kwargs) -> FakeResponse:
        if "checkNeedCaptcha" in url:
            return FakeResponse(payload={"isNeed": self.need_captcha})
        if "captcha" in url.lower():
            self.image_hits += 1
            return FakeResponse(content=b"\x89PNG-fake")
        page = self.login_pages.pop(0) if len(self.login_pages) > 1 else self.login_pages[0]
        return FakeResponse(text=page, url=self.first_url)

    def post(self, _url: str, data: dict, **_kwargs) -> FakeResponse:
        self.posted.append(data)
        return self.posts.pop(0)


def test_without_captcha_nothing_changes(monkeypatch):
    called = []
    monkeypatch.setattr(cas.ocr, "solve", lambda *_a, **_k: called.append(1) or "zzzzzz")

    session = FakeSession(need_captcha=False, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    assert cas.cas_login(session, "255000001", "pw", "svc") == LANDING
    assert session.posted[0]["captcha"] == ""
    assert called == []


def test_valid_cookie_does_not_consume_password_login_limit():
    checked = []
    session = FakeSession(need_captcha=False, login_pages=[LANDING], posts=[],
                          first_url="https://zhxg.csu.edu.cn/callback")

    assert cas.cas_login(session, "255000001", "pw", "svc",
                         before_password_login=lambda: checked.append(True)) == LANDING
    assert checked == []


def test_password_login_checks_limit_before_submitting():
    session = FakeSession(need_captcha=False, login_pages=[PAGE], posts=[])

    with pytest.raises(RuntimeError, match="limited"):
        cas.cas_login(session, "255000001", "pw", "svc",
                      before_password_login=lambda: (_ for _ in ()).throw(RuntimeError("limited")))

    assert session.posted == []


def test_school_bad_credentials_tip_gives_actionable_guidance(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda *_a, **_k: "")
    rejected = '<html><div id="showErrorTip">您提供的用户名\n或者密码有误</div></html>'
    session = FakeSession(need_captcha=False, login_pages=[PAGE],
                          posts=[FakeResponse(text=rejected)])

    with pytest.raises(RuntimeError) as caught:
        cas.cas_login(session, "255000001", "pw", "svc")

    message = str(caught.value)
    assert "学号或密码有误" in message
    assert "学校信息门户" in message
    assert "确认能够正常登录" in message
    assert "再回到此页面添加账号" in message


def test_solved_captcha_is_submitted(monkeypatch):
    seen = []

    def fake_solve(image: bytes) -> str:
        seen.append(image)
        return "8f3k2p"

    monkeypatch.setattr(cas.ocr, "solve", fake_solve)

    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    assert cas.cas_login(session, "255000001", "pw", "svc") == LANDING
    assert session.posted[0]["captcha"] == "8f3k2p"
    assert seen == [b"\x89PNG-fake"], "应该把登录页里那张图交给了识别器"


def test_wrong_captcha_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: "aaaaaa")

    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=CAPTCHA_TIP), FakeResponse(text=CAPTCHA_TIP)])
    with pytest.raises(RuntimeError) as error:
        cas.cas_login(session, "255000001", "pw", "svc")

    assert len(session.posted) == cas.MAX_CAPTCHA_ATTEMPTS == 2
    assert session.image_hits == 2, "每轮都该重新取一张图"
    assert NEEDS_RECREDENTIALS.search(str(error.value)), "要能触发「需要人工重登」的判定"


def test_second_attempt_succeeds(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: "bbbbbb")

    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=CAPTCHA_TIP),
                                 FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    assert cas.cas_login(session, "255000001", "pw", "svc") == LANDING
    assert len(session.posted) == 2


def test_no_ocr_never_submits_a_blank_captcha(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: None)

    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    with pytest.raises(RuntimeError) as error:
        cas.cas_login(session, "255000001", "pw", "svc")

    assert session.posted == [], "不该发那次注定失败的登录"
    assert "ddddocr" in str(error.value)
    assert NEEDS_RECREDENTIALS.search(str(error.value))


def test_captcha_image_url_is_discovered_from_page(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: "cccccc")
    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    urls: list[str] = []
    original_get = session.get

    def spy(url: str, **kwargs):
        urls.append(url)
        return original_get(url, **kwargs)

    session.get = spy
    cas.cas_login(session, "255000001", "pw", "svc")
    assert any(url.endswith("/authserver/captcha?ts=1") for url in urls), urls


def test_fallback_uses_the_real_endpoint(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: "ffffff")
    urls: list[str] = []
    session = FakeSession(need_captcha=True, login_pages=[PAGE_WITHOUT_IMAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    original_get = session.get

    def spy(url, **kwargs):
        urls.append(url)
        return original_get(url, **kwargs)

    session.get = spy
    cas.cas_login(session, "255000001", "pw", "svc")
    assert any(url.startswith(f"{cas.CAPTCHA_URL}?") for url in urls), urls


def test_ocr_module_is_optional_and_never_raises():
    assert ocr.solve(b"") is None
    if ocr.available():
        assert ocr.solve(b"not an image at all") is None
    else:
        pytest.skip("未安装 ddddocr（可选依赖）")


def test_non_image_response_is_not_fed_to_ocr(monkeypatch):
    called = []
    monkeypatch.setattr(cas.ocr, "solve", lambda image: called.append(image) or "dddddd")

    class HtmlImageSession(FakeSession):
        def get(self, url, **kwargs):
            if "captcha" in url.lower() and "checkneedcaptcha" not in url.lower():
                self.image_hits += 1
                return FakeResponse(content=b"<html>404 not found</html>", ok=False)
            return super().get(url, **kwargs)

    session = HtmlImageSession(need_captcha=True, login_pages=[PAGE],
                               posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    with pytest.raises(RuntimeError) as error:
        cas.cas_login(session, "255000001", "pw", "svc")

    assert session.posted == [], "不是图片就别提交"
    assert called == [], "别把错误页喂给识别器"
    assert NEEDS_RECREDENTIALS.search(str(error.value))


def test_evidence_is_kept_for_later_inspection(monkeypatch):
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: "eeeee1")
    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=CAPTCHA_TIP), FakeResponse(text=CAPTCHA_TIP)])
    with pytest.raises(RuntimeError):
        cas.cas_login(session, "255000001", "pw", "svc")

    from app import config as cfg

    debug_dir = cfg.DATA_DIR / "captcha-debug"
    assert (debug_dir / "last-login-page.html").exists()
    assert (debug_dir / "last-captcha.png").read_bytes() == b"\x89PNG-fake"
    assert oct((debug_dir / "last-captcha.png").stat().st_mode)[-3:] == "600", "现场证据也要收紧权限"


@pytest.mark.parametrize(("raw", "expected"), [
    ("a", None),            # 只认出一个字符：明显没认对
    ("12x", None),          # 三位也不行
    ("ab-cd", "abcd"),      # 标点去掉后刚好四位，可以接受
    ("abcdef", "abcdef"),
    ("abcdefgh", None),     # 超出长度说明认花了
    ("", None),
])
def test_ocr_only_accepts_plausible_lengths(monkeypatch, raw, expected):
    class FakeEngine:
        def classification(self, _image):
            return raw

    monkeypatch.setattr(ocr, "_engine", FakeEngine())
    monkeypatch.setattr(ocr, "_loaded", True)
    assert ocr.solve(b"fake-image") == expected


def test_captcha_log_does_not_leak_the_recognized_text(monkeypatch):
    events = []
    monkeypatch.setattr(cas, "log_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: "s3cr3t")

    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=CAPTCHA_TIP), FakeResponse(text=CAPTCHA_TIP)])
    with pytest.raises(RuntimeError):
        cas.cas_login(session, "255000001", "pw", "svc")

    attempts = [fields for event, fields in events if event == "checkin.captcha_attempt"]
    assert attempts and all("s3cr3t" not in str(fields.values()) for fields in attempts)
    assert attempts[0]["recognized"] is True
    assert attempts[0]["recognized_length"] == 6


CALLBACK = "<html><script>var uid = 'abc'; var lzc = 'def';</script></html>"


def test_valid_cas_session_skips_the_password(monkeypatch):
    called = []
    monkeypatch.setattr(cas, "encrypt_password", lambda pw, salt: called.append(pw) or "encrypted")

    session = FakeSession(need_captcha=False, login_pages=[CALLBACK], posts=[],
                          first_url="https://zhxg.csu.edu.cn/home")
    assert cas.cas_login(session, "255000001", "pw", "svc") == CALLBACK
    assert session.posted == [], "会话有效时一次登录 POST 都不该发"
    assert called == [], "更不该去加密密码"


def test_missing_password_only_fails_if_cas_really_needs_it(monkeypatch):
    session = FakeSession(need_captcha=False, login_pages=[CALLBACK], posts=[],
                          first_url="https://zhxg.csu.edu.cn/home")
    assert cas.cas_login(session, "255000001", None, "svc") == CALLBACK

    needs_password = FakeSession(need_captcha=False, login_pages=[PAGE],
                                 posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    with pytest.raises(SecretDecryptError) as error:
        cas.cas_login(needs_password, "255000001", None, "svc")
    assert "重新提交一次密码" in str(error.value)
    assert isinstance(error.value, checkin_credential_errors), "必须是凭据类错误，否则不会标记需重填"
    assert needs_password.posted == [], "没有密码就别发那次注定失败的登录"


def test_password_is_encrypted_when_cas_asks_for_it(monkeypatch):
    called = []
    monkeypatch.setattr(cas, "encrypt_password", lambda pw, salt: called.append((pw, salt)) or "ENCRYPTED")

    session = FakeSession(need_captcha=False, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    assert cas.cas_login(session, "255000001", "pw", "svc") == LANDING
    assert len(called) == 1, "要密码时必须加密提交一次"
    assert session.posted[0]["password"] == "ENCRYPTED"
