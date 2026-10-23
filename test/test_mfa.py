"""MFA must be completed in the original session before scheduling resumes."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import auth, checkin, db, mfa, scheduler
from app.csu import cas
from app.csu.zhxg import CAS_CALLBACK
from app.errors import BadRequestError, UpstreamError
from app.main import app
from test.test_ui import make_account

MFA_URL = cas.CAS_BASE + "/reAuthCheck/reAuthLoginView.do"


def challenge(**updates):
    params = {"service": CAS_CALLBACK, "reAuthType": "3", "isMultifactor": "true",
              "reAuthUserId": "school-id", "isSleepAccount": "0"}
    params.update(updates)
    return cas.CasMfaRequired("<script>var reAuthParams = " + json.dumps(params) + ";</script>",
                              MFA_URL, CAS_CALLBACK)


@pytest.fixture()
def account():
    user = db.upsert_user("mfa-test@example.com", db.now_iso())
    db._exec("DELETE FROM accounts WHERE user_id = ?", (user["id"],))
    row = make_account(user["id"], "123456789", dkdz="")
    db.set_auth_error(row["id"], "mfa_required")
    mfa._pending.clear()
    yield db.get_account_by_id(row["id"])
    mfa._pending.clear()


@pytest.fixture()
def client(account):
    client = TestClient(app)
    token = auth.create_session(account["user_id"], "mfa-test")
    client.cookies.set(auth.SESSION_COOKIE, token)
    return client


def pending(account, **params):
    client = Mock()
    client.session.post.return_value = SimpleNamespace(status_code=200, json=lambda: {"res": "success"})
    mfa.remember(account["id"], client, challenge(**params))
    return client


def test_password_form_is_selected_when_ids_repeat():
    html = '<form id="phoneFromId"><input id="pwdEncryptSalt" value="wrong"></form>'
    html += '<form id="pwdFromId"><input id="pwdEncryptSalt" value="correct"></form>'
    assert cas.input_value(html, "pwdEncryptSalt") == "correct"


def test_mfa_is_detected_before_password_or_captcha_submission():
    error = challenge()
    page = '<script>var reAuthParams = ' + json.dumps(error.params) + ';</script>'
    session = Mock()
    session.get.return_value = SimpleNamespace(url=MFA_URL, text=page)
    with pytest.raises(cas.CasMfaRequired):
        cas.cas_login(session, "user", "password", CAS_CALLBACK)
    session.post.assert_not_called()
    assert session.get.call_count == 1


def test_mfa_classification_does_not_confuse_sms_with_captcha():
    assert checkin._failure_kind(challenge(), str(challenge())) == "mfa_required"


@pytest.mark.parametrize("params", [{"service": "https://example.com/"}, {"reAuthType": "2"}])
def test_invalid_or_non_sms_challenge_never_sends(account, params):
    fake = pending(account, **params)
    with pytest.raises(BadRequestError):
        mfa.send_code(account)
    fake.session.post.assert_not_called()


def test_expired_challenge_is_not_reused(account):
    fake = pending(account)
    mfa._pending[account["id"]].expires = 0
    with pytest.raises(BadRequestError):
        mfa.send_code(account)
    fake.session.post.assert_not_called()


def test_sms_cooldown_prevents_duplicate_send(account):
    fake = pending(account)
    assert mfa.send_code(account)["ok"]
    with pytest.raises(BadRequestError, match="两分钟"):
        mfa.send_code(account)
    assert fake.session.post.call_count == 1
    assert fake.session.post.call_args.kwargs["data"]["authCodeTypeName"] == "reAuthDynamicCodeType"


def test_wrong_sms_never_clears_failure_or_saves_state(account):
    fake = pending(account)
    fake.session.post.return_value.json = lambda: {"code": "reAuth_failed", "msg": "验证码错误"}
    for _ in range(3):
        with pytest.raises(BadRequestError, match="未通过验证码校验"):
            mfa.finish(account, "123456")
    with pytest.raises(BadRequestError, match="上限"):
        mfa.finish(account, "123456")
    assert fake.session.post.call_count == 3
    fake.session.get.assert_not_called()
    assert db.get_account_by_id(account["id"])["auth_error"] == "mfa_required"


@pytest.mark.parametrize("trust", [False, True])
def test_valid_sms_requires_business_status_before_clearing_auth_error(account, trust):
    fake = pending(account)
    fake.session.post.return_value.json = lambda: {"code": "reAuth_success"}
    fake.session.get.return_value = SimpleNamespace(url=CAS_CALLBACK, status_code=200, text="callback")
    fake.dk_status.return_value = {"code": "200", "data": {"dksjfw": "20:00-23:30", "sfydk": False}}
    fake.token = "jwt-for-test"
    fake.casual = "test-casual"
    fake.cookies_json.return_value = "[]"
    result = mfa.finish(account, "123456", trust)
    assert result["ok"]
    assert fake.session.post.call_args.kwargs["data"]["skipTmpReAuth"] == str(trust).lower()
    fake.submit_dk.assert_not_called()
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == ""
    assert row["enabled"] == account["enabled"]
    assert row["token"] == "jwt-for-test"
    assert not mfa.ready(account["id"])
    assert scheduler._refresh_eligible(row)


def test_unconfirmed_business_login_keeps_account_paused(account):
    fake = pending(account)
    fake.session.post.return_value.json = lambda: {"code": "reAuth_success"}
    fake.session.get.return_value = SimpleNamespace(url=MFA_URL, status_code=200, text="still MFA")
    with pytest.raises(UpstreamError):
        mfa.finish(account, "123456")
    assert db.get_account_by_id(account["id"])["auth_error"] == "mfa_required"


def test_mfa_page_is_authenticated_and_trust_is_opt_in(client, account):
    pending(account)
    html = client.get(f"/ui/accounts/{account['id']}/mfa").text
    assert 'name="trust" type="checkbox" value="true"' in html
    assert "checked" not in html
    client.cookies.clear()
    assert client.get(f"/ui/accounts/{account['id']}/mfa", follow_redirects=False).status_code == 303


def test_mfa_actions_enforce_account_ownership(client, account, monkeypatch):
    other = db.upsert_user("mfa-other@example.com", db.now_iso())
    stranger = make_account(other["id"], "other123")
    send = Mock()
    monkeypatch.setattr(mfa, "send_code", send)
    response = client.post(f"/ui/accounts/{stranger['id']}/mfa/send")
    assert response.status_code == 404
    send.assert_not_called()


def test_prepare_uses_original_relogin_lock_without_nesting(account, monkeypatch):
    def relogin(row, **_kwargs):
        from app.locks import lock_for
        with lock_for(f"account:{row['id']}"):
            pending(row)
        return {"ok": False, "message": str(challenge())}
    monkeypatch.setattr(checkin, "relogin", relogin)
    assert mfa.begin(account)["ok"]


@pytest.mark.parametrize("force", [False, True])
def test_scheduler_waits_for_mfa_without_repeating_login(account, monkeypatch, force):
    now = datetime(2026, 1, 1, 20, 30)  # noqa: DTZ001 - app uses local wall time
    monkeypatch.setattr(scheduler, "local_now", lambda _tz: now)
    run = Mock()
    monkeypatch.setattr(scheduler, "run_checkin", run)
    assert scheduler.run_batch(accounts=[account], force=force) == []
    assert scheduler.refresh_logins(accounts=[account]) == []
    run.assert_not_called()


def test_success_does_not_enable_a_paused_account(account):
    db.update_account(account["id"], {"enabled": 0})
    fake = pending(account)
    fake.session.post.return_value.json = lambda: {"code": "reAuth_success"}
    fake.session.get.return_value = SimpleNamespace(url=CAS_CALLBACK, status_code=200, text="callback")
    fake.dk_status.return_value = {"code": "331", "data": None}
    fake.token, fake.casual = "test-token", "test-casual"
    fake.cookies_json.return_value = "[]"
    assert mfa.finish(account, "123456")["ok"]
    row = db.get_account_by_id(account["id"])
    assert row["enabled"] == 0 and row["auth_error"] == ""
    fake.submit_dk.assert_not_called()


@pytest.mark.parametrize("status", [{"code": "401"}, {"code": "200", "data": None}])
def test_invalid_business_status_never_persists_login(account, status):
    fake = pending(account)
    fake.session.post.return_value.json = lambda: {"code": "reAuth_success"}
    fake.session.get.return_value = SimpleNamespace(url=CAS_CALLBACK, status_code=200, text="callback")
    fake.dk_status.return_value = status
    with pytest.raises(UpstreamError):
        mfa.finish(account, "123456")
    row = db.get_account_by_id(account["id"])
    assert row["auth_error"] == "mfa_required" and not row["token"]
    fake.submit_dk.assert_not_called()


def test_password_submit_redirects_to_mfa_without_repeating_password(monkeypatch):
    page = '<form id="pwdFromId"><input id="execution" value="test-execution">'
    page += '<input id="pwdEncryptSalt" value="0123456789abcdef"></form>'
    error = challenge()
    session = Mock()
    session.get.return_value = SimpleNamespace(url=cas.CAS_BASE + "/login", text=page)
    session.post.return_value = SimpleNamespace(url=MFA_URL, text="reAuthParams=" + json.dumps(error.params))
    monkeypatch.setattr(cas, "_needs_captcha", lambda *_: False)
    with pytest.raises(cas.CasMfaRequired):
        cas.cas_login(session, "test-user", "test-password", CAS_CALLBACK)
    assert session.post.call_count == 1


def test_relogin_keeps_original_session_for_mfa(account, monkeypatch):
    fake = Mock()
    monkeypatch.setattr(checkin, "build_client", lambda _: fake)
    monkeypatch.setattr(checkin, "cas_login", Mock(side_effect=challenge()))
    result = checkin._relogin_body(account)
    assert not result["ok"]
    assert mfa._get(account["id"]).client is fake
    assert db.get_account_by_id(account["id"])["auth_error"] == "mfa_required"
    assert db.get_account_by_id(account["id"])["enabled"] == 1


def test_failed_sms_request_still_has_resend_cooldown(account):
    import requests
    fake = pending(account)
    fake.session.post.side_effect = requests.Timeout()
    with pytest.raises(requests.Timeout):
        mfa.send_code(account)
    with pytest.raises(BadRequestError, match="两分钟"):
        mfa.send_code(account)
    assert fake.session.post.call_count == 1


@pytest.mark.parametrize("action", ["begin", "send", "verify"])
def test_mfa_actions_require_management_login(client, account, action):
    fake = pending(account)
    client.cookies.clear()
    response = client.post(f"/ui/accounts/{account['id']}/mfa/{action}",
                           data={"code": "123456"}, follow_redirects=False)
    assert response.status_code == 303
    fake.session.post.assert_not_called()


def test_account_edit_invalidates_pending_challenge(account):
    from app.crypto import encrypt_secret
    fake = pending(account)
    db.update_account(account["id"], {"password_enc": encrypt_secret("different-test-password")})
    with pytest.raises(BadRequestError, match="信息已变化"):
        mfa.finish(account, "123456")
    fake.session.post.assert_not_called()


def test_school_error_cannot_echo_sms_or_session_to_user(client, account):
    fake = pending(account)
    fake.session.post.return_value.json = lambda: {
        "code": "reAuth_failed", "msg": "code=654321; token=example-private-session",
    }
    response = client.post(f"/ui/accounts/{account['id']}/mfa/verify", data={"code": "654321"})
    assert "654321" not in response.text and "example-private-session" not in response.text
