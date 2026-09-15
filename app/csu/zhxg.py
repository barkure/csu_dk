"""智慧学工（zhxg.csu.edu.cn）：CAS 登录换业务 JWT，业务请求体走 DES-ECB。"""
from __future__ import annotations

import http.cookiejar
import json
import re
from collections.abc import Callable
from urllib.parse import quote

import requests

from .. import exits
from .cas import UA, cas_login
from .des import des_encrypt, generate_casual

ORIGIN = "https://zhxg.csu.edu.cn"
CAS_CALLBACK = f"{ORIGIN}/fdcwonsun/caslogin_h5.jsp"
API_SYS = f"{ORIGIN}/znzhxgpt/basesys"
API_QXJ = f"{ORIGIN}/znzhxgpt/qxj"
APP_CODE = "znzhxgpt"


class ZhxgError(Exception):
    pass


def dump_cookies(session: requests.Session) -> str:
    return json.dumps([
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "expires": cookie.expires,
        }
        for cookie in session.cookies
    ])


def load_cookies(session: requests.Session, raw: str | None) -> None:
    """忽略无效 Cookie 数据。"""
    if not raw:
        return
    try:
        items = json.loads(raw)
    except ValueError:
        return
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        domain = str(item.get("domain") or "")
        session.cookies.set_cookie(http.cookiejar.Cookie(
            version=0,
            name=str(item["name"]),
            value=str(item.get("value") or ""),
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=str(item.get("path") or "/"),
            path_specified=True,
            secure=bool(item.get("secure")),
            expires=item.get("expires"),
            discard=item.get("expires") is None,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        ))


def new_session() -> requests.Session:
    session = requests.Session()
    proxy = exits.current()
    session.csu_exit = proxy
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


class ZhxgClient:
    def __init__(self, session: requests.Session | None = None, casual: str | None = None,
                 token: str | None = None, cookies: str | None = None):
        self.session = session or new_session()
        self.exit = getattr(self.session, "csu_exit", exits.current())
        if cookies:
            load_cookies(self.session, cookies)
        self.casual = casual or generate_casual(16)
        self.token = token or None

    def cookies_json(self) -> str:
        """把会话 cookie 序列化落库（与密码同等待遇，加密存储）。"""
        return dump_cookies(self.session)

    def has_login_cookie(self) -> bool:
        """是否持有可用于免密登录 CAS 的 Cookie。"""
        return any(cookie.name.upper() == "CASTGC" for cookie in self.session.cookies)

    def login(self, username: str, password: str | None = None,
              before_password_login: Callable[[], None] | None = None) -> str:
        html = cas_login(self.session, username, password, CAS_CALLBACK,
                         before_password_login=before_password_login)
        return self._exchange_callback(html)

    def _exchange_callback(self, html: str) -> str:
        uid = re.search(r"var\s+uid\s*=\s*'([^']*)'", html) or re.search(r'var\s+uid\s*=\s*"([^"]*)"', html)
        lzc = re.search(r"var\s+lzc\s*=\s*'([^']*)'", html) or re.search(r'var\s+lzc\s*=\s*"([^"]*)"', html)
        if not uid:
            raise ZhxgError("CAS 回调页未返回 uid（可能是 service 不匹配）")

        response = self.session.post(
            f"{API_SYS}/rbac-yh/login-other",
            json={
                "tyrzpt": "1",
                "channeld": "1",
                "yhzh": quote(quote(uid.group(1)), safe=""),
                "lzc": quote(quote(lzc.group(1) if lzc else ""), safe=""),
                "caasual": self.casual,
            },
            headers={
                "user-agent": UA,
                "content-type": "application/json; charset=utf-8",
                "deviceType": "4",
            },
            timeout=20,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        token = (data.get("data") or {}).get("token")
        if not token:
            raise ZhxgError(f"换取业务 token 失败：{json.dumps(data, ensure_ascii=False)[:200]}")
        self.token = token
        return token

    def _headers(self) -> dict:
        return {
            "user-agent": UA,
            "content-type": "application/json; charset=utf-8",
            "deviceType": "4",
            "Authorization": self.token or "",
            "token": self.token or "",
            "AppCode": APP_CODE,
        }

    def post(self, path: str, payload: dict) -> dict:
        response = self.session.post(
            f"{API_QXJ}{path}",
            data=des_encrypt(payload, self.casual),
            headers=self._headers(),
            timeout=20,
        )
        try:
            return response.json()
        except ValueError:
            return {"code": f"HTTP {response.status_code}", "message": response.text[:200], "data": None}

    def dk_status(self, dklb: str = "PA") -> dict:
        return self.post("/qxj-padkglxx/queryKqDkbc", {"paramsData": {"dklb": dklb}})

    def check_location(self, jd: float, wd: float, dklb: str = "PA") -> dict:
        return self.post("/qxj-padkglxx/jcqqwzsjsfndk", {"paramsData": {"jd": jd, "wd": wd, "dklb": dklb}})

    def submit_dk(self, jd: float, wd: float, dkbc: str, dkdz: str = "", smsy: str = "", smfj: str = "",
                  sfwcdk: int = 0) -> dict:
        return self.post("/qxj-padkglxx/xspadk", {
            "jd": jd, "wd": wd, "dkbc": dkbc, "dkdz": dkdz, "smsy": smsy, "smfj": smfj, "sfwcdk": sfwcdk,
        })

    def month_stats(self, rq: str, dklb: str = "PA") -> dict:
        return self.post("/qxj-padkglxx/queryPadkKqAyListByXh", {"paramsData": {"rq": rq, "dklb": dklb}})
