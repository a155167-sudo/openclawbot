"""Durable, fail-closed LINE webhook receipt queue.

The HTTP layer verifies signature and JSON first, stores the immutable envelope, sends
its ACK, and only then wakes this bounded in-process worker.
"""
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse


def verified_line_payload(body: bytes, signature: str, channel_secret: str) -> dict:
    if not channel_secret or not signature:
        raise ValueError("invalid signature")
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError("invalid webhook shape")
    if any(not isinstance(event, dict) for event in payload["events"]):
        raise ValueError("invalid webhook shape")
    return payload


def _delivery_key(payload: dict, body: bytes) -> str:
    event_ids = [str(event.get("webhookEventId") or "").strip() for event in payload["events"]]
    if event_ids and all(event_ids):
        return "events:" + hashlib.sha256("\0".join(event_ids).encode()).hexdigest()
    return "body:" + hashlib.sha256(body).hexdigest()


class LineWebhookInbox:
    def __init__(self, db_path: str, dispatch):
        self.db_path = db_path
        self.dispatch = dispatch
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._ensure_schema()
        self._recover_after_restart()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS line_webhook_inbox (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                delivery_key TEXT NOT NULL,
                body TEXT NOT NULL,
                signature TEXT NOT NULL,
                status TEXT NOT NULL,
                received_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            )""")
            conn.commit()

    def _recover_after_restart(self):
        with self._connect() as conn:
            # These rows never crossed dispatch and are safe to resume.
            conn.execute(
                "UPDATE line_webhook_inbox SET status='ready' WHERE status IN ('accepted','received')"
            )
            # Never blindly replay an irreversible dispatch boundary.
            conn.execute(
                "UPDATE line_webhook_inbox SET status='unknown', error='restart_after_dispatch_start' WHERE status='started'"
            )
            conn.commit()

    def receive(self, body: bytes, signature: str, payload=None):
        payload = payload if payload is not None else json.loads(body.decode("utf-8"))
        key = _delivery_key(payload, body)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM line_webhook_inbox WHERE delivery_key=? LIMIT 1", (key,)
            ).fetchone():
                conn.commit()
                # Return the stable receipt so a provider retry can ACK-activate
                # an accepted row left behind by a lost prior HTTP response.
                return key
            conn.execute(
                "INSERT INTO line_webhook_inbox(delivery_key,body,signature,status,received_at) VALUES (?,?,?,?,?)",
                (key, body.decode("utf-8"), signature, "accepted", now),
            )
            conn.commit()
        return key

    def ack_response(self, delivery_key):
        # Starlette runs BackgroundTask only after http.response.body has been sent.
        return PlainTextResponse(
            "OK", background=BackgroundTask(self.activate_and_start, delivery_key)
        )

    def activate_and_start(self, delivery_key):
        if delivery_key:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE line_webhook_inbox SET status='ready' WHERE delivery_key=? AND status='accepted'",
                    (delivery_key,),
                )
                conn.commit()
        self.start()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._wake.set()
                return
            self._thread = threading.Thread(target=self._run, name="line-webhook-inbox", daemon=True)
            self._wake.set()
            self._thread.start()

    def _claim(self):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT rowid,body,signature FROM line_webhook_inbox WHERE status='ready' ORDER BY received_at,rowid LIMIT 1"
            ).fetchone()
            if row:
                conn.execute("UPDATE line_webhook_inbox SET status='started' WHERE rowid=? AND status='ready'", (row[0],))
            conn.commit()
            return row

    def _run(self):
        while True:
            row = self._claim()
            if not row:
                # Close the receive/start race: clear, recheck, then allow one
                # short wake window before this finite worker exits.
                self._wake.clear()
                row = self._claim()
                if not row:
                    if self._wake.wait(0.1):
                        continue
                    # Serialize retirement with start(). Activation commits
                    # before start takes this lock, so this final claim covers
                    # a caller that observed the old thread alive.
                    with self._lock:
                        row = self._claim()
                        if not row:
                            if self._wake.is_set():
                                continue
                            self._thread = None
                            return
            rowid, body, signature = row
            try:
                self.dispatch(body, signature)
            except Exception as exc:
                status, error = "failed", type(exc).__name__
            else:
                status, error = "finished", ""  # Existing staging schema declares error NOT NULL.
            with self._connect() as conn:
                conn.execute(
                    "UPDATE line_webhook_inbox SET status=?,finished_at=?,error=? WHERE rowid=? AND status='started'",
                    (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), error, rowid),
                )
                conn.commit()

    def wait_idle(self, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            thread = self._thread
            if not thread or not thread.is_alive():
                return True
            thread.join(min(0.05, max(0, deadline - time.monotonic())))
        return False
