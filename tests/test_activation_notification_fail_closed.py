import sqlite3
import threading
import time
from datetime import datetime, timedelta

import server
from linebot.exceptions import LineBotApiError
from linebot.models.error import Error
from tests.test_subscription_activation_retry import UID, _install_activation_db


def _create_pending_intent(tmp_path, monkeypatch):
    db_path, pushes, _ = _install_activation_db(tmp_path, monkeypatch)
    ok, _ = server.update_subscription_order_status(1, "activated", "admin")
    assert ok is False
    assert pushes == []
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT state FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone()
    assert row == ("pending",)
    return db_path


def _canonical_request(db_path):
    with sqlite3.connect(db_path) as conn:
        user_id, code = conn.execute(
            "SELECT user_id,vip_code FROM subscription_orders WHERE id=1"
        ).fetchone()
    message = server._activation_success_message(1, code)
    return user_id, message


def test_formalize_failure_then_recovery_uses_existing_intent_and_sends_once(tmp_path, monkeypatch):
    db_path, pushes, formalize_calls = _install_activation_db(tmp_path, monkeypatch)

    first_ok, _ = server.update_subscription_order_status(1, "activated", "admin")
    second_ok, _ = server.update_subscription_order_status(1, "activated", "admin")
    third_ok, _ = server.update_subscription_order_status(1, "activated", "admin")

    assert (first_ok, second_ok, third_ok) == (False, True, True)
    assert len(formalize_calls) == 2
    assert len(pushes) == 1
    assert "專屬排餐與正式試算表已同步建立" in pushes[0][1]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone() == ("sent",)


def test_legacy_activated_order_without_intent_requires_manual_confirmation(tmp_path, monkeypatch):
    db_path, pushes, _ = _install_activation_db(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE subscription_orders SET status='activated',vip_code='#VIPORDER-ABC123',formalized_at='legacy' WHERE id=1"
        )
        conn.execute(
            "INSERT INTO vips(code,meals,duration_days,chat_limit,is_used) VALUES('#VIPORDER-ABC123',48,31,30,0)"
        )

    ok, message = server.update_subscription_order_status(1, "activated", "admin")

    assert ok is False
    assert "人工核對" in message
    assert pushes == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subscription_activation_notifications'"
        ).fetchone() is None


def test_same_key_payload_drift_is_rejected_without_push(tmp_path, monkeypatch):
    db_path = _create_pending_intent(tmp_path, monkeypatch)
    user_id, message = _canonical_request(db_path)
    pushes = []
    monkeypatch.setattr(
        server.line_bot_api, "push_message", lambda *args, **kwargs: pushes.append((args, kwargs))
    )

    delivered, attempted = server._deliver_activation_success_notification(
        1, user_id, message + " drift"
    )

    assert (delivered, attempted) == (False, False)
    assert pushes == []
    assert server._deliver_activation_success_notification(
        1, "U" + "b" * 32, message
    ) == (False, False)
    assert pushes == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone() == ("pending",)


def test_unknown_never_retries_immediately_or_after_24_hours(tmp_path, monkeypatch):
    db_path = _create_pending_intent(tmp_path, monkeypatch)
    user_id, message = _canonical_request(db_path)
    pushes = []

    def unknown(*args, **kwargs):
        pushes.append((args, kwargs))
        raise TimeoutError("provider outcome unknown")

    monkeypatch.setattr(server.line_bot_api, "push_message", unknown)
    assert server._deliver_activation_success_notification(1, user_id, message) == (False, True)
    assert server._deliver_activation_success_notification(1, user_id, message) == (False, False)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE subscription_activation_notifications SET attempted_at=? WHERE order_id=1",
            ((datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds"),),
        )
    assert server._deliver_activation_success_notification(1, user_id, message) == (False, False)
    assert len(pushes) == 1


def test_two_sqlite_callers_only_one_claims_and_pushes(tmp_path, monkeypatch):
    db_path = _create_pending_intent(tmp_path, monkeypatch)
    user_id, message = _canonical_request(db_path)
    pushes = []
    push_lock = threading.Lock()

    def slow_success(*args, **kwargs):
        with push_lock:
            pushes.append((args, kwargs))
        time.sleep(0.05)

    monkeypatch.setattr(server.line_bot_api, "push_message", slow_success)
    barrier = threading.Barrier(2)
    results = []

    def deliver():
        barrier.wait()
        results.append(server._deliver_activation_success_notification(1, user_id, message))

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert len(pushes) == 1
    assert sorted(results) == [(False, False), (True, True)]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone() == ("sent",)


def test_only_409_with_sdk_accepted_request_id_is_confirmed_sent(tmp_path, monkeypatch):
    db_path = _create_pending_intent(tmp_path, monkeypatch)
    user_id, message = _canonical_request(db_path)

    accepted = LineBotApiError(
        409, {}, accepted_request_id="accepted-request-id", error=Error(message="duplicate")
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(accepted),
    )
    assert server._deliver_activation_success_notification(1, user_id, message) == (True, True)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT state,provider_accepted_request_id FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone() == ("sent", "accepted-request-id")


def test_generic_409_is_unknown_and_not_retried(tmp_path, monkeypatch):
    db_path = _create_pending_intent(tmp_path, monkeypatch)
    user_id, message = _canonical_request(db_path)
    calls = []

    generic = LineBotApiError(
        409, {}, accepted_request_id="", error=Error(message="conflict")
    )

    def generic_409(*args, **kwargs):
        calls.append(1)
        raise generic

    monkeypatch.setattr(server.line_bot_api, "push_message", generic_409)
    assert server._deliver_activation_success_notification(1, user_id, message) == (False, True)
    assert server._deliver_activation_success_notification(1, user_id, message) == (False, False)
    assert calls == [1]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone() == ("unknown",)
