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

# 页面里有登录表单、但没写验证码图片地址（真学校很可能就是这样，图要靠接口取）
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
    """按 URL 分流的假会话，记录每一次请求。"""

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
    """学校不要验证码时，走的就是老路径：一次 POST、验证码字段为空、不动 OCR。"""
    called = []
    monkeypatch.setattr(cas.ocr, "solve", lambda *_a, **_k: called.append(1) or "zzzzzz")

    session = FakeSession(need_captcha=False, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    assert cas.cas_login(session, "255000001", "pw", "svc") == LANDING
    assert session.posted[0]["captcha"] == ""
    assert called == []


def test_solved_captcha_is_submitted(monkeypatch):
    """要验证码时：取图 → 识别 → 把结果提交上去。"""
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
    """识别错了会换一张图重试，但次数有上限 —— 每次失败登录都在喂学校风控。"""
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
    """识别不出来时一次 POST 都不发：提交空验证码必然是失败登录。"""
    monkeypatch.setattr(cas.ocr, "solve", lambda _image: None)

    session = FakeSession(need_captcha=True, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    with pytest.raises(RuntimeError) as error:
        cas.cas_login(session, "255000001", "pw", "svc")

    assert session.posted == [], "不该发那次注定失败的登录"
    assert "ddddocr" in str(error.value)
    assert NEEDS_RECREDENTIALS.search(str(error.value))


def test_captcha_image_url_is_discovered_from_page(monkeypatch):
    """图片地址要从登录页里认出来，而不是写死。"""
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
    """页面上认不出图片时，兜底要用学校真实的取图接口 getCaptcha.htl（不是猜的 /captcha）。"""
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
    """没装 ddddocr 也只是返回 None；装了就不能在垃圾数据上炸。"""
    assert ocr.solve(b"") is None
    if ocr.available():
        assert ocr.solve(b"not an image at all") is None
    else:
        pytest.skip("未安装 ddddocr（可选依赖）")


def test_non_image_response_is_not_fed_to_ocr(monkeypatch):
    """取回来的是 HTML 错误页（地址猜错了）时，不许拿去识别、更不许提交登录。"""
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
    """真碰上验证码时把页面和图片留一份：这条链路没法在测试里真验，只能靠现场证据。"""
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
    """识别结果的长度必须像个验证码，否则这一次就是没认出来，别拿去换一次失败登录。"""
    class FakeEngine:
        def classification(self, _image):
            return raw

    monkeypatch.setattr(ocr, "_engine", FakeEngine())
    monkeypatch.setattr(ocr, "_loaded", True)
    assert ocr.solve(b"fake-image") == expected


def test_captcha_log_does_not_leak_the_recognized_text(monkeypatch):
    """日志里只留"认出来没有 + 几位"，不写明文。"""
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
    """CAS 会话（CASTGC）还有效时，不该再交一次密码。

    这是"把 CAS cookie 存下来"的全部意义：学校那边从每天一次密码登录变成每十几天一次，
    失败计数与验证码的暴露都更小。
    """
    called = []
    monkeypatch.setattr(cas, "encrypt_password", lambda pw, salt: called.append(pw) or "encrypted")

    session = FakeSession(need_captcha=False, login_pages=[CALLBACK], posts=[],
                          first_url="https://zhxg.csu.edu.cn/home")
    assert cas.cas_login(session, "255000001", "pw", "svc") == CALLBACK
    assert session.posted == [], "会话有效时一次登录 POST 都不该发"
    assert called == [], "更不该去加密密码"


def test_missing_password_only_fails_if_cas_really_needs_it(monkeypatch):
    """密码解不开时：会话有效就继续；真需要交密码才报错（提示人工重填）。"""
    session = FakeSession(need_captcha=False, login_pages=[CALLBACK], posts=[],
                          first_url="https://zhxg.csu.edu.cn/home")
    assert cas.cas_login(session, "255000001", None, "svc") == CALLBACK

    needs_password = FakeSession(need_captcha=False, login_pages=[PAGE],
                                 posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    # 必须是凭据类错误：调用方靠类型（和 NEEDS_RECREDENTIALS）决定"标记需重填并暂停打卡"
    with pytest.raises(SecretDecryptError) as error:
        cas.cas_login(needs_password, "255000001", None, "svc")
    assert "重新提交一次密码" in str(error.value)
    assert isinstance(error.value, checkin_credential_errors), "必须是凭据类错误，否则不会标记需重填"
    assert needs_password.posted == [], "没有密码就别发那次注定失败的登录"


def test_password_is_encrypted_when_cas_asks_for_it(monkeypatch):
    """反证：CAS 真要密码时，必须走一次加密提交。

    这条是给"探针可信度"兜底的 —— 上面两条断言"0 次提交"，得先证明计数本来就会动。
    """
    called = []
    monkeypatch.setattr(cas, "encrypt_password", lambda pw, salt: called.append((pw, salt)) or "ENCRYPTED")

    session = FakeSession(need_captcha=False, login_pages=[PAGE],
                          posts=[FakeResponse(text=LANDING, url="https://zhxg.csu.edu.cn/home")])
    assert cas.cas_login(session, "255000001", "pw", "svc") == LANDING
    assert len(called) == 1, "要密码时必须加密提交一次"
    assert session.posted[0]["password"] == "ENCRYPTED"
