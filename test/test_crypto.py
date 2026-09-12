"""加密：往返、篡改检测，以及与 Node 版落的密文/生产密钥的兼容性。"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import config as cfg
from app.crypto import decrypt_secret, encrypt_secret
from app.errors import MasterKeyMissingError, SecretDecryptError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    """解密路径缺失密钥时抛错，且不生成新密钥。"""
    cfg.ensure_master_key()
    payload = encrypt_secret("keep-me")
    cfg.MASTER_KEY_PATH.unlink()
    cfg.reset_master_key_cache()

    with pytest.raises(MasterKeyMissingError):
        cfg.read_master_key()
    # 密钥文件缺失就报"密钥不存在"，比包装成"密文解不开"更准确
    with pytest.raises(MasterKeyMissingError):
        decrypt_secret(payload)
    assert not cfg.MASTER_KEY_PATH.exists()

    # 写入路径（人主动提交新凭据）才允许生成
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


def test_decrypts_ciphertext_written_by_node_version():
    """用真实的 data/master.key 解真实数据库里的密文：GCM 认证通过才说明格式逐字节兼容。"""
    data_dir = PROJECT_ROOT / "data"
    key_file = data_dir / "master.key"
    db_file = data_dir / "csu_dk.db"
    if not key_file.exists() or not db_file.exists():
        pytest.skip("没有真实的 data/ 可验证")

    with sqlite3.connect(f"file:{db_file}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT csu_username, password_enc FROM accounts").fetchall()
    if not rows:
        pytest.skip("数据库里没有账号")

    code = (
        "import base64, json, sys\n"
        "from app.crypto import decrypt_secret\n"
        "payloads = json.loads(sys.stdin.read())\n"
        "print(json.dumps([len(decrypt_secret(p)) for p in payloads]))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "CSU_DK_TEST_DATA_DIR"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps([row[1] for row in rows]),
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )
    lengths = json.loads(result.stdout)
    assert len(lengths) == len(rows)
    assert all(length > 0 for length in lengths), "每个密码都应该能解出非空明文"
