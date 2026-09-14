"""AES-256-GCM 凭据加密。"""
from __future__ import annotations

import base64
import os

from Crypto.Cipher import AES

from . import config as cfg
from .errors import SecretDecryptError

KEY_BYTES = 32
TAG_BYTES = 16


def _master_key() -> bytes:
    """解密路径：只读，缺失/非法就抛错，绝不生成新密钥。"""
    return base64.b64decode(cfg.read_master_key())


def _master_key_for_write() -> bytes:
    """写入路径：全项目唯一允许按需生成密钥的地方（人不主动提交凭据就不会发生）。"""
    info = cfg.inspect_master_key()
    if info.exists and info.valid and info.key:
        return base64.b64decode(info.key)
    if info.exists:
        from .errors import MasterKeyInvalidError

        raise MasterKeyInvalidError(cfg.MASTER_KEY_PATH, info.detail)

    created = cfg.ensure_master_key()
    if created:
        print("[crypto] 加密密钥缺失，已生成新的 master.key；此前保存的凭据需要重新提交")
    return base64.b64decode(cfg.read_master_key())


def encrypt_secret(plain: str) -> str:
    iv = os.urandom(12)
    cipher = AES.new(_master_key_for_write(), AES.MODE_GCM, nonce=iv)
    ciphertext, tag = cipher.encrypt_and_digest(str(plain).encode())
    return f"v1.{base64.b64encode(iv).decode()}.{base64.b64encode(ciphertext + tag).decode()}"


def decrypt_secret(payload: str) -> str:
    parts = str(payload or "").split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1] or not parts[2]:
        raise SecretDecryptError("密文格式不正确（期望 v1.iv.data）")

    raw = base64.b64decode(parts[2])
    if len(raw) <= TAG_BYTES:
        raise SecretDecryptError("密文长度异常，数据可能已损坏")
    ciphertext, tag = raw[:-TAG_BYTES], raw[-TAG_BYTES:]

    cipher = AES.new(_master_key(), AES.MODE_GCM, nonce=base64.b64decode(parts[1]))
    try:
        return cipher.decrypt_and_verify(ciphertext, tag).decode()
    except ValueError as error:
        raise SecretDecryptError() from error
