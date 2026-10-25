"""账号最早自动打卡时间。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, checkin, db, scheduler, views
from app import config as cfg
from app.crypto import encrypt_secret
from app.domain import Trigger
from app.errors import AppError
from app.main import app


@pytest.fixture()
def saved(monkeypatch):
    now = "2026-10-25T18:00:00"
    user = db.upsert_user("not-before@example.com", now)
    account = db.insert_account({
        "user_id": user["id"], "csu_username": "not-before-test", "password_enc": encrypt_secret("test"),
        "enabled": 1, "created_at": now, "updated_at": now,
        "checkin_start": "20:00", "checkin_end": "23:30",
    })
    monkeypatch.setattr(cfg.config, "checkin_start", "12:00")
    yield user, account
    db.delete_user(user["email"])


def test_save_clear_and_ownership(saved):
    user, account = saved
    db.update_account(account['id'], {'checkin_start': '09:00'})
    accounts.update(user, account["id"], {"notBefore": "9:05"})
    assert db.get_account_by_id(account["id"])["not_before"] == "09:05"
    accounts.update(user, account["id"], {"enabled": True})
    assert db.get_account_by_id(account["id"])["not_before"] == "09:05"
    with pytest.raises(AppError):
        accounts.update({"id": -1}, account["id"], {"notBefore": "21:00"})
    with pytest.raises(AppError):
        accounts.update(user, account["id"], {"notBefore": "25:00"})
    assert db.get_account_by_id(account["id"])["not_before"] == "09:05"
    accounts.update(user, account["id"], {"notBefore": ""})
    assert db.get_account_by_id(account["id"])["not_before"] is None


@pytest.mark.parametrize(("time", "ready", "label"), [
    ("19:00", False, "未到时间"), ("20:59", False, "未到时间"),
    ("21:00", True, "排队打卡中"), ("23:31", False, "时段已结束"),
])
def test_schedule_and_display(saved, monkeypatch, time, ready, label):
    _, account = saved
    account["not_before"] = "21:00"
    now = datetime.fromisoformat(f"2026-10-25T{time}:00")
    monkeypatch.setattr(views, "_now", lambda: (now.isoformat(), time))
    assert scheduler._ready(account, now) is ready
    assert views.today_result(account)["label"] == label


def test_school_window_still_applies(saved):
    _, account = saved
    account["not_before"] = "13:00"
    assert not scheduler._ready(account, datetime.fromisoformat("2026-10-25T19:00:00"))
    assert scheduler._ready(account, datetime.fromisoformat("2026-10-25T20:00:00"))


def test_locked_execution_uses_latest_preference_and_manual_bypasses(saved, monkeypatch):
    user, account = saved
    accounts.update(user, account["id"], {"notBefore": "21:00"})
    monkeypatch.setattr(checkin, "local_now", lambda _: datetime.fromisoformat("2026-10-25T20:00:00"))
    calls = []
    monkeypatch.setattr(checkin, "_attempt", lambda a, t: calls.append(t) or {"status": "success"})
    assert checkin.run_checkin(account, Trigger.SCHEDULE)["deferred"]
    assert calls == []
    assert checkin.run_checkin(account, Trigger.MANUAL)["status"] == "success"
    assert calls == [Trigger.MANUAL]
    assert db.list_records(account["id"]) == []


def test_ui_and_api(saved, monkeypatch):
    user, account = saved
    monkeypatch.setattr(auth, "resolve_session", lambda _: user)
    client = TestClient(app, base_url="http://localhost")
    html = client.get('/ui/accounts').text
    assert 'data-label="不早于">--:--</td>' in html
    assert 'type="time"' not in html
    url = f"/ui/accounts/{account['id']}/not-before?open={account['id']}"
    response = client.post(url, data={"notBefore": "21:00"})
    assert response.status_code == 200
    assert '<th>不早于</th>' in response.text
    assert 'value="21:00"' in response.text
    assert 'is-open' in response.text
    assert f'id="not-before-{account["id"]}" class="hidden"' in response.text
    assert '>设置不早于</button>' in response.text
    assert response.text.index('type="time"') < response.text.index('data-role="actions"')
    html = client.get('/ui/accounts').text
    assert 'data-label="不早于">21:00</td>' in html
    assert 'type="time"' not in html
    response = client.patch(f"/api/accounts/{account['id']}", json={"enabled": True})
    assert response.status_code == 200
    assert db.get_account_by_id(account["id"])["not_before"] == "21:00"
    response = client.get('/api/accounts')
    assert response.json()["accounts"][0]["notBefore"] == "21:00"
    client.post(url, data={"notBefore": ""})
    assert db.get_account_by_id(account["id"])["not_before"] is None


@pytest.mark.parametrize('value', ['25:00', '19:59', '23:31'])
def test_invalid_time_rejected_by_api_and_ui(saved, monkeypatch, value):
    user, account = saved
    accounts.update(user, account['id'], {'notBefore': '21:00'})
    monkeypatch.setattr(auth, 'resolve_session', lambda _: user)
    client = TestClient(app, base_url='http://localhost')
    response = client.patch(f"/api/accounts/{account['id']}", json={'notBefore': value})
    assert response.status_code == 400
    response = client.post(f"/ui/accounts/{account['id']}/not-before", data={'notBefore': value})
    assert response.status_code == 200
    assert 'data-label="不早于">21:00</td>' in response.text
    assert f'value="{value}"' not in response.text
    assert 'is-open' not in response.text
    assert response.headers["X-Not-Before-Error"]
    assert db.get_account_by_id(account['id'])['not_before'] == '21:00'


@pytest.mark.parametrize(('start', 'end', 'value'), [
    ('20:00', '23:30', '20:00'),
    ('20:00', '23:30', '23:30'),
    ('23:00', '01:00', '23:30'),
    ('23:00', '01:00', '00:30'),
    (None, None, '23:59'),
])
def test_allowed_time_boundaries(saved, start, end, value):
    user, account = saved
    db.update_account(account['id'], {'checkin_start': start, 'checkin_end': end})
    accounts.update(user, account['id'], {'notBefore': value})
    assert db.get_account_by_id(account['id'])['not_before'] == value


def test_reset_keeps_today_result_and_does_not_add_click_warning(saved, monkeypatch):
    user, account = saved
    accounts.update(user, account['id'], {'notBefore': '21:00'})
    db.update_account(account['id'], {'last_status': 'success', 'last_run_at': '2026-10-25T20:00:00'})
    account = db.get_account_by_id(account['id'])
    checkin._save_checkin_window(account, {'kdk': False, 'bkyy': '不在打卡时间', 'dksjfw': '18:00-20:30'})
    monkeypatch.setattr(views, '_now', lambda: ('2026-10-25T21:00:00', '21:00'))
    assert views.account_view(account)['today']['label'] == '打卡成功'
    monkeypatch.setattr(auth, 'resolve_session', lambda _: user)
    client = TestClient(app, base_url='http://localhost')
    html = client.get('/ui/accounts').text
    assert 'data-not-before-error' not in html
    assert 'data-label="不早于">--:--</td>' in html
    html = client.get(f"/ui/accounts?open={account['id']}").text
    assert 'name="notBefore" value=""' in html


def test_new_account_validates_learned_window(saved, monkeypatch):
    user, _ = saved
    monkeypatch.setattr(accounts, 'verify_login', lambda *_args, **_kwargs: {
        'session': {'token': 'test', 'casual': '', 'cookies': '[]'},
        'data': {'dksjfw': '18:00-20:00'},
    })
    with pytest.raises(AppError, match='不能晚于学校打卡结束时间 20:00'):
        accounts.create_or_update(user, {'csuUsername': 'new-conflict', 'password': 'test', 'notBefore': '23:00'})
    assert db.get_account_by_username(user['id'], 'new-conflict') is None


@pytest.mark.parametrize('window', ['18:00-20:00', '22:00-23:30'])
def test_school_change_resets_preference_and_queues_notice(saved, window):
    user, account = saved
    accounts.update(user, account['id'], {'notBefore': '21:00'})
    account = db.get_account_by_id(account['id'])
    checkin._save_checkin_window(account, {'kdk': False, 'bkyy': '不在打卡时间', 'dksjfw': window})
    assert account['not_before'] is None
    saved_row = db.get_account_by_id(account['id'])
    assert saved_row['not_before'] is None
    start, end = window.split('-')
    assert saved_row['checkin_start'] == start
    assert saved_row['checkin_end'] == end
    task = db.get_schedule_notice(account['id'])
    assert (task['not_before'], task['checkin_start'], task['checkin_end']) == ('21:00', start, end)
    checkin._save_checkin_window(account, {'kdk': False, 'bkyy': '不在打卡时间', 'dksjfw': window})
    assert db.get_schedule_notice(account['id']) == task


def test_password_verification_can_reset_conflicting_saved_preference(saved, monkeypatch):
    user, account = saved
    accounts.update(user, account['id'], {'notBefore': '21:00'})
    monkeypatch.setattr(accounts, 'verify_login', lambda *_args, **_kwargs: {
        'session': {'token': 'test', 'casual': '', 'cookies': '[]'},
        'data': {'dksjfw': '18:00-20:00'},
    })
    accounts.update(user, account['id'], {'password': 'new-password'})
    assert db.get_account_by_id(account['id'])['not_before'] is None
    assert db.get_schedule_notice(account['id'])['not_before'] == '21:00'


def test_reset_and_notice_roll_back_together(saved, monkeypatch):
    user, account = saved
    accounts.update(user, account['id'], {'notBefore': '21:00'})
    account = db.get_account_by_id(account['id'])
    monkeypatch.setattr(db, 'update_account', lambda *_: (_ for _ in ()).throw(RuntimeError('write failed')))
    with pytest.raises(RuntimeError, match='write failed'):
        checkin._save_checkin_window(account, {'kdk': False, 'bkyy': '不在打卡时间', 'dksjfw': '18:00-20:00'})
    assert db.get_account_by_id(account['id'])['not_before'] == '21:00'
    assert db.get_schedule_notice(account['id']) is None
    assert account['checkin_end'] == '23:30'


def test_schedule_notice_retries_and_preserves_new_user_setting(saved, monkeypatch):
    from datetime import timedelta
    from unittest.mock import Mock

    from app import notifications
    from app.clock import local_now

    user, account = saved
    db.queue_schedule_notice(account['id'], '21:00', '18:00', '20:00')
    now = local_now(cfg.config.tz)
    monkeypatch.setattr(notifications, 'local_now', lambda _: now)
    monkeypatch.setattr(notifications, 'mailer_enabled', lambda: True)
    sender = Mock(side_effect=[RuntimeError('unavailable'), {'sent': True}])
    monkeypatch.setattr(notifications, 'send_schedule_reset_notice', sender)
    notifications.retry_schedule_notices()
    assert db.get_schedule_notice(account['id'])['attempts'] == 1
    notifications.retry_schedule_notices()
    assert sender.call_count == 1
    accounts.update(user, account['id'], {'notBefore': '22:00'})
    now += timedelta(seconds=notifications.NOTICE_RETRY_SECONDS)
    notifications.retry_schedule_notices()
    assert sender.call_count == 2
    assert db.get_schedule_notice(account['id']) is None
    assert db.get_account_by_id(account['id'])['not_before'] == '22:00'


@pytest.mark.parametrize("raises", [False, True])
def test_schedule_notice_stops_after_three_attempts(saved, monkeypatch, raises):
    from datetime import timedelta
    from unittest.mock import Mock

    from app import notifications
    from app.clock import local_now

    _, account = saved
    db.queue_schedule_notice(account['id'], '21:00', '18:00', '20:00')
    now = local_now(cfg.config.tz)
    monkeypatch.setattr(notifications, 'local_now', lambda _: now)
    monkeypatch.setattr(notifications, 'mailer_enabled', lambda: True)
    sender = Mock(side_effect=RuntimeError('unavailable')) if raises else Mock(return_value={'sent': False})
    events = []
    monkeypatch.setattr(notifications, 'log_event', lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(notifications, 'send_schedule_reset_notice', sender)
    for _ in range(5):
        notifications.retry_schedule_notices()
        now += timedelta(seconds=notifications.NOTICE_RETRY_SECONDS)
    assert sender.call_count == 3
    assert db.get_schedule_notice(account['id']) is None
    assert ('account.schedule_notify_abandoned',
            {'level': 'warning', 'account_id': account['id'], 'attempt': 3}) in events


def test_schedule_mail_has_no_frame_and_only_username_tail(monkeypatch):
    from app import mailer

    sent = []
    monkeypatch.setattr(mailer, 'mailer_enabled', lambda: True)
    monkeypatch.setattr(mailer, '_send', lambda *args: sent.append(args) or {'sent': True, 'id': 'test'})
    mailer.send_schedule_reset_notice('test@example.com', '1234567890', '21:00', '18:00', '20:00')
    _, subject, body = sent[0]
    assert subject == '【妙妙道具】打卡时间已调整'
    assert '7890' in body and '1234567890' not in body
    assert '18:00–20:00' in body and '不早于 21:00' in body
    assert '系统已清空该设置，恢复默认调度' in body
    assert 'border' not in body and 'mail/base' not in body


def test_disabled_mail_preserves_task_until_enabled(saved, monkeypatch):
    from unittest.mock import Mock

    from app import notifications

    _, account = saved
    db.queue_schedule_notice(account['id'], '21:00', '18:00', '20:00')
    task = db.get_schedule_notice(account['id'])
    sender = Mock(return_value={'sent': True})
    monkeypatch.setattr(notifications, 'send_schedule_reset_notice', sender)
    monkeypatch.setattr(notifications, 'mailer_enabled', lambda: False)
    for _ in range(5):
        notifications.retry_schedule_notices()
    sender.assert_not_called()
    assert db.get_schedule_notice(account['id']) == task
    monkeypatch.setattr(notifications, 'mailer_enabled', lambda: True)
    notifications.retry_schedule_notices()
    sender.assert_called_once()
    assert db.get_schedule_notice(account['id']) is None


def test_exhausted_task_is_cleaned_without_sending(saved, monkeypatch):
    from unittest.mock import Mock

    from app import notifications

    _, account = saved
    db.queue_schedule_notice(account['id'], '21:00', '18:00', '20:00')
    for _ in range(3):
        db.attempt_schedule_notice(account['id'], '2099-01-01T00:00:00')
    sender = Mock()
    monkeypatch.setattr(notifications, 'send_schedule_reset_notice', sender)
    monkeypatch.setattr(notifications, 'mailer_enabled', lambda: True)
    notifications.retry_schedule_notices()
    sender.assert_not_called()
    assert db.get_schedule_notice(account['id']) is None
