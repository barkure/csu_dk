"""请求体加密：与学校前端 postDes 一致 —— DES-ECB(JSON, casual) 的 hex。

JSON 必须用 ensure_ascii=False：JS 的 JSON.stringify 直接输出 UTF-8，
Python 默认会把中文转成 \\uXXXX，密文就对不上了（dkbc/dkdz 里就是中文）。
"""
from __future__ import annotations

import json
import secrets
import string

from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

_CASUAL_CHARS = string.ascii_uppercase + string.ascii_lowercase + string.digits


def des_encrypt(payload: object, key: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    cipher = DES.new(key.encode()[:8], DES.MODE_ECB)
    return cipher.encrypt(pad(body, DES.block_size)).hex()


def generate_casual(length: int = 16) -> str:
    return "".join(secrets.choice(_CASUAL_CHARS) for _ in range(length))
