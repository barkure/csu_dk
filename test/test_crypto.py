"""凭据加密。"""
from __future__ import annotations

import base64

import pytest

from app import config as cfg
from app.crypto import decrypt_secret, encrypt_secret
from app.errors import MasterKeyMissingError, SecretDecryptError


def test_roundtrip():
    cfg.ensure_master_key()
    payload = encrypt_secret("my-password")
    assert payload.startswith("v1.")
    assert payload.count(".") == 2
    assert decrypt_secret(payload) == "my-password"


def test_ciphertext_is_not_plaintext():
    cfg.ensure_master_key()
    payload = encrypt_secret("secret-value")
    assert "secret-value" not in payload


def test_tampered_ciphertext_rejected():
    cfg.ensure_master_key()
    version, iv, data = encrypt_secret("x").split(".")
    raw = bytearray(base64.b64decode(data))
    raw[0] ^= 0xFF
    with pytest.raises(SecretDecryptError):
        decrypt_secret(f"{version}.{iv}.{base64.b64encode(bytes(raw)).decode()}")


def test_bad_format_rejected():
    for bad in ["", "abc", "v2.a.b", "v1..b", "v1.a."]:
        with pytest.raises(SecretDecryptError):
            decrypt_secret(bad)


def test_decrypt_never_creates_key():
    cfg.ensure_master_key()
    payload = encrypt_secret("keep-me")
    cfg.MASTER_KEY_PATH.unlink()
    cfg.reset_master_key_cache()

    with pytest.raises(MasterKeyMissingError):
        cfg.read_master_key()
    with pytest.raises(MasterKeyMissingError):
        decrypt_secret(payload)
    assert not cfg.MASTER_KEY_PATH.exists()
    fresh = encrypt_secret("new-password")
    assert cfg.MASTER_KEY_PATH.exists()
    assert decrypt_secret(fresh) == "new-password"
    with pytest.raises(SecretDecryptError):
        decrypt_secret(payload)


def test_master_key_permissions_fixed():
    cfg.ensure_master_key()
    cfg.MASTER_KEY_PATH.chmod(0o644)
    cfg.reset_master_key_cache()
    cfg.read_master_key()
    assert cfg.MASTER_KEY_PATH.stat().st_mode & 0o777 == 0o600
