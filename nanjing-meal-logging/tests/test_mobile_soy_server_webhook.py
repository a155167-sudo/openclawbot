import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest


def test_real_registered_natural_food_route_reaches_semantic_not_legacy(monkeypatch):
    import server

    event = SimpleNamespace(
        message=SimpleNamespace(id="M-SOY-153233", text="無糖豆漿 500ml"),
        source=SimpleNamespace(user_id="U-SOY", type="user"),
        reply_token="R", webhook_event_id="W-SOY",
    )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "parse_natural_food_log_intent", lambda _text: {
        "food_name": "無糖豆漿", "amount": 500, "unit": "ml", "meal_slot": "午餐"
    })
    monkeypatch.setattr(server, "_natural_food_candidates", lambda *_a, **_k: ([], "none"))
    monkeypatch.setattr(server, "resolve_reference", lambda _request: None)
    monkeypatch.setattr(server, "create_text_meal_estimate_draft", lambda **_k: pytest.fail("legacy total AI called"))
    calls = []
    monkeypatch.setattr(server, "_handle_registered_semantic_meal", lambda ev, text, uid, mid: calls.append((text, uid, mid)) or (True, True))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_a: None)
    server.processed_messages.clear()

    server.handle_message(event)

    assert calls == [("無糖豆漿 500ml", "U-SOY", "M-SOY-153233")]


def test_inbox_validates_persists_dedupes_and_dispatches_only_after_ack(tmp_path):
    from line_webhook_inbox import LineWebhookInbox, verified_line_payload

    db = tmp_path / "inbox.db"
    secret = "test-secret"
    body = json.dumps({"events": [{"webhookEventId": "evt-1", "type": "message"}]}).encode()
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert verified_line_payload(body, signature, secret)["events"][0]["webhookEventId"] == "evt-1"

    calls = []
    inbox = LineWebhookInbox(str(db), lambda raw, sig: calls.append((raw, sig)))
    receipt = inbox.receive(body, signature)
    assert receipt
    assert inbox.receive(body, signature) == receipt
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status,COUNT(*) FROM line_webhook_inbox GROUP BY status").fetchone() == ("accepted", 1)

    sent = []
    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            assert calls == []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response = inbox.ack_response(receipt)
    asyncio.run(response({"type": "http", "method": "POST", "path": "/callback", "headers": []}, receive, send))
    inbox.wait_idle(timeout=2)
    assert calls == [(body.decode(), signature)]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM line_webhook_inbox").fetchone()[0] == "finished"

    with pytest.raises(ValueError):
        verified_line_payload(body, "forged", secret)
    malformed = b'{"events": {}}'
    malformed_sig = base64.b64encode(
        hmac.new(secret.encode(), malformed, hashlib.sha256).digest()
    ).decode()
    with pytest.raises(ValueError):
        verified_line_payload(malformed, malformed_sig, secret)


def test_restart_marks_started_unknown_and_safely_restores_accepted(tmp_path):
    from line_webhook_inbox import LineWebhookInbox

    db = tmp_path / "restart.db"
    first = LineWebhookInbox(str(db), lambda *_: None)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO line_webhook_inbox(id,delivery_key,body,signature,status,received_at) VALUES ('1','a','{}','s','started','now')")
        conn.execute("INSERT INTO line_webhook_inbox(id,delivery_key,body,signature,status,received_at) VALUES ('2','b','{}','s','accepted','now')")
        conn.commit()
    calls = []
    restarted = LineWebhookInbox(str(db), lambda body, sig: calls.append((body, sig)))
    restarted.start()
    restarted.wait_idle(timeout=2)
    with sqlite3.connect(db) as conn:
        assert dict(conn.execute("SELECT id,status FROM line_webhook_inbox")) == {"1": "unknown", "2": "finished"}
    assert calls == [("{}", "s")]


def test_running_worker_cannot_dispatch_second_delivery_before_its_ack_body(tmp_path):
    from line_webhook_inbox import LineWebhookInbox

    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    def dispatch(body, _signature):
        calls.append(body)
        if 'evt-1' in body:
            first_started.set()
            assert release_first.wait(2)

    inbox = LineWebhookInbox(str(tmp_path / "active.db"), dispatch)
    bodies = [json.dumps({"events": [{"webhookEventId": f"evt-{i}"}]}).encode() for i in (1, 2)]
    first = inbox.receive(bodies[0], "sig")

    async def send_first(_message):
        return None

    async def receive_first():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(inbox.ack_response(first)(
        {"type": "http", "method": "POST", "path": "/callback", "headers": []},
        receive_first,
        send_first,
    ))
    assert first_started.wait(1)

    second = inbox.receive(bodies[1], "sig")
    body_blocked = threading.Event()
    allow_body = threading.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            body_blocked.set()
            while not allow_body.wait(0.01):
                await asyncio.sleep(0)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response_thread = threading.Thread(
        target=lambda: asyncio.run(inbox.ack_response(second)(
            {"type": "http", "method": "POST", "path": "/callback", "headers": []}, receive, send
        )))
    response_thread.start()
    assert body_blocked.wait(1)
    release_first.set()
    assert inbox.wait_idle(1)
    assert calls == [bodies[0].decode()]
    allow_body.set()
    response_thread.join(2)
    assert inbox.wait_idle(2)
    assert calls == [body.decode() for body in bodies]


def test_worker_exit_and_start_handoff_cannot_strand_ready_delivery(tmp_path):
    from line_webhook_inbox import LineWebhookInbox

    class ExitGateEvent:
        def __init__(self):
            self._event = threading.Event()
            self.waiting = threading.Event()
            self.release = threading.Event()
        def set(self): self._event.set()
        def clear(self): self._event.clear()
        def is_set(self): return self._event.is_set()
        def wait(self, _timeout=None):
            self.waiting.set()
            assert self.release.wait(2)
            return False

    calls = []
    inbox = LineWebhookInbox(str(tmp_path / "exit.db"), lambda body, _sig: calls.append(body))
    gate = ExitGateEvent()
    inbox._wake = gate
    inbox.start()
    assert gate.waiting.wait(1)
    body = json.dumps({"events": [{"webhookEventId": "exit-race"}]}).encode()
    receipt = inbox.receive(body, "sig")
    starter = threading.Thread(target=lambda: inbox.activate_and_start(receipt))
    starter.start()
    starter.join(1)
    gate.release.set()
    starter.join(2)
    assert inbox.wait_idle(2)
    assert calls == [body.decode()]


def test_draft_cards_say_confirm_then_record_not_already_booked():
    import server

    draft = {
        "token": "a" * 40, "version": 1, "portion_multiplier": 1,
        "meal_slot": "午餐", "status": "pending",
        "estimate": {
            "food_name": "無糖豆漿", "portion_assumption": "500 ml",
            "provenance": {"method": "semantic_meal_estimate", "source_label": "AI估算"},
            **{field: {"estimate": value, "min": value, "max": value}
               for field, value in {
                   "calories_kcal": 150, "protein_g": 12.5,
                   "fat_g": 5.0, "carbohydrate_g": 15.0,
               }.items()},
        },
    }
    rendered = json.dumps(server.build_text_meal_estimate_flex(draft).as_json_dict(), ensure_ascii=False)
    assert "確認後記錄" in rendered
    assert "實際入帳" not in rendered
