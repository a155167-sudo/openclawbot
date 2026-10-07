import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from linebot import WebhookHandler

import server

SECRET = "inbox-test-secret"


def _sign(body):
    return base64.b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode()


def _body(*event_ids):
    return json.dumps({"destination": "U0", "events": [
        {"type": "unfollow", "mode": "active", "timestamp": 1, "webhookEventId": eid,
         "deliveryContext": {"isRedelivery": False}, "source": {"type": "user", "userId": "U1"}}
        for eid in event_ids
    ]})


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "inbox.db"))
    fake = WebhookHandler(SECRET)
    handled = []
    gate = threading.Event()
    gate.set()

    def handle(body, signature):
        assert gate.wait(5)
        handled.append(json.loads(body)["events"][0]["webhookEventId"])
    monkeypatch.setattr(fake, "handle", handle)
    monkeypatch.setattr(server, "handler", fake)
    return handled, gate


def _drain(timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with sqlite3.connect(server.DB_PATH) as conn:
            server._ensure_line_inbox_schema(conn)
            left = conn.execute(
                "SELECT COUNT(*) FROM line_webhook_inbox WHERE status IN ('pending','processing')"
            ).fetchone()[0]
        if not left:
            return
        time.sleep(0.02)
    raise AssertionError("inbox not drained")


def test_bad_signature_is_rejected_and_not_recorded(inbox):
    client = TestClient(server.app)
    body = _body("EV-BAD")
    assert client.post("/callback", content=body, headers={"X-Line-Signature": "wrong"}).status_code == 400
    with sqlite3.connect(server.DB_PATH) as conn:
        server._ensure_line_inbox_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM line_webhook_inbox").fetchone()[0] == 0


def test_callback_answers_before_slow_processing_finishes(inbox):
    handled, gate = inbox
    gate.clear()  # processing is blocked, yet LINE must get 200 immediately
    client = TestClient(server.app)
    body = _body("EV-FAST")
    started = time.time()
    response = client.post("/callback", content=body, headers={"X-Line-Signature": _sign(body)})
    assert response.status_code == 200 and time.time() - started < 1.5
    assert handled == []
    gate.set()
    _drain()
    assert handled == ["EV-FAST"]


def test_redelivery_with_same_webhook_event_id_is_processed_once(inbox):
    handled, _gate = inbox
    client = TestClient(server.app)
    body = _body("EV-DUP")
    for _ in range(3):
        assert client.post("/callback", content=body, headers={"X-Line-Signature": _sign(body)}).status_code == 200
    _drain()
    assert handled == ["EV-DUP"]


def test_deliveries_are_processed_in_arrival_order(inbox):
    handled, gate = inbox
    gate.clear()
    client = TestClient(server.app)
    for eid in ("EV-1", "EV-2", "EV-3"):
        body = _body(eid)
        client.post("/callback", content=body, headers={"X-Line-Signature": _sign(body)})
    gate.set()
    _drain()
    assert handled == ["EV-1", "EV-2", "EV-3"]


def test_handler_error_is_recorded_and_worker_keeps_going(inbox, monkeypatch):
    calls = []

    def handle(body, signature):
        eid = json.loads(body)["events"][0]["webhookEventId"]
        calls.append(eid)
        if eid == "EV-BOOM":
            raise RuntimeError("boom")
    monkeypatch.setattr(server.handler, "handle", handle)
    client = TestClient(server.app)
    for eid in ("EV-BOOM", "EV-OK"):
        body = _body(eid)
        client.post("/callback", content=body, headers={"X-Line-Signature": _sign(body)})
    _drain()
    assert calls == ["EV-BOOM", "EV-OK"]
    with sqlite3.connect(server.DB_PATH) as conn:
        assert dict(conn.execute(
            "SELECT delivery_key,status FROM line_webhook_inbox"
        ).fetchall()) == {"ev:EV-BOOM": "failed", "ev:EV-OK": "done"}


def test_delivery_interrupted_by_restart_is_retried_on_startup(inbox):
    handled, _gate = inbox
    body = _body("EV-RESTART")
    with sqlite3.connect(server.DB_PATH) as conn:
        server._ensure_line_inbox_schema(conn)
        conn.execute(
            "INSERT INTO line_webhook_inbox(delivery_key,body,signature,status,received_at) VALUES(?,?,?,?,?)",
            ("ev:EV-RESTART", body, _sign(body), "processing", "2026-10-07T00:00:00"),
        )
        conn.commit()
    server.recover_line_inbox_on_startup()
    _drain()
    assert handled == ["EV-RESTART"]


class _DupHeaderSheet:
    def __init__(self):
        import gspread
        self._exc = gspread.exceptions.GSpreadException

    def get_all_records(self, expected_headers=None):
        if expected_headers is None:
            raise self._exc("the header row in the worksheet contains duplicates: ['']")
        assert expected_headers == ["Date", "User_ID"]
        return [{"Date": "2026/10/07", "User_ID": "U1", "": ""}]

    def row_values(self, index):
        assert index == 1
        return ["Date", "User_ID", "", ""]


def test_sheet_records_tolerates_trailing_blank_headers():
    assert server._sheet_records(_DupHeaderSheet()) == [{"Date": "2026/10/07", "User_ID": "U1"}]


def test_sheet_records_still_rejects_duplicated_real_headers():
    sheet = _DupHeaderSheet()
    sheet.row_values = lambda index: ["Date", "Date", ""]
    with pytest.raises(Exception, match="duplicates"):
        server._sheet_records(sheet)
