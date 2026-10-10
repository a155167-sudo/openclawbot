import json
import sqlite3
from types import SimpleNamespace

import pytest

import server


class _SequenceCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[len(self.calls) - 1]
        return SimpleNamespace(
            id=f"trace-{len(self.calls)}",
            model="fixture-model",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(payload, ensure_ascii=False), refusal=None
                ),
            )],
        )


def _setup(tmp_path, monkeypatch, message_id="BOUNDARY-1"):
    db = tmp_path / "semantic-boundaries.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES ('U-BOUNDARY',2,10,?,'vip','2099-12-31',2)""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()
    completions = _SequenceCompletions([
        {"intent": "meal_log", "meal_slot": "午餐", "clarification": "", "items": [
            {"food_name": "燕麥奶", "amount": 300, "unit": "ml", "portion_assumption": "明示300ml"}
        ]},
        {"items": [{
            "item_id": "item-0", "food_name": "燕麥奶", "basis_amount": 100,
            "basis_unit": "ml",
            "calories_kcal": {"estimate": 45, "min": 35, "max": 55, "unit": "kcal"},
            "protein_g": {"estimate": 1.2, "min": 0.8, "max": 1.6, "unit": "g"},
            "fat_g": {"estimate": 1.5, "min": 1, "max": 2, "unit": "g"},
            "carbohydrate_g": {"estimate": 7, "min": 5, "max": 9, "unit": "g"},
        }]},
    ])
    monkeypatch.setattr(
        server, "client", SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    event = SimpleNamespace(reply_token="reply-boundary")
    return db, completions, event


def _quota(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-BOUNDARY'"
        ).fetchone()[0]


def test_preflight_failure_is_safe_unfinished_not_provider_unknown(tmp_path, monkeypatch):
    db, completions, event = _setup(tmp_path, monkeypatch, "PREFLIGHT-FAIL")
    replies = []
    monkeypatch.setattr(
        server, "_load_semantic_meal_replay",
        lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("preflight unavailable")),
    )
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, reply: replies.append(reply))

    handled, claimed = server._handle_registered_semantic_meal(
        event, "無糖豆漿500ml", "U-BOUNDARY", "PREFLIGHT-FAIL"
    )

    assert (handled, claimed) == (True, False)
    assert completions.calls == []
    assert _quota(db) == 2
    assert len(replies) == 1
    assert "尚未完成" in replies[0].text
    assert "尚未呼叫 AI" in replies[0].text
    assert "結果待確認" not in replies[0].text
    assert "額度未退還" not in replies[0].text
    assert "已記錄" not in replies[0].text


def test_claim_failure_is_safe_unfinished_not_provider_unknown(tmp_path, monkeypatch):
    db, completions, event = _setup(tmp_path, monkeypatch, "CLAIM-FAIL")
    replies = []
    monkeypatch.setattr(
        server, "_claim_semantic_meal_batch",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("claim failed")),
    )
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, reply: replies.append(reply))

    handled, claimed = server._handle_registered_semantic_meal(
        event, "無糖豆漿500ml", "U-BOUNDARY", "CLAIM-FAIL"
    )

    assert (handled, claimed) == (True, False)
    assert completions.calls == []
    assert _quota(db) == 2
    assert len(replies) == 1
    assert "尚未完成" in replies[0].text
    assert "結果待確認" not in replies[0].text
    assert "額度未退還" not in replies[0].text
    assert "已記錄" not in replies[0].text


def test_reply_failure_after_completed_persist_never_downgrades_or_reinvokes_provider(
    tmp_path, monkeypatch,
):
    db, completions, event = _setup(tmp_path, monkeypatch, "REPLY-FAIL")
    delivery_attempts = []

    def fail_first_reply(_token, reply):
        delivery_attempts.append(reply)
        if len(delivery_attempts) == 1:
            raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server.line_bot_api, "reply_message", fail_first_reply)

    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_registered_semantic_meal(
            event, "燕麥奶300ml", "U-BOUNDARY", "REPLY-FAIL"
        )

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM semantic_meal_batches WHERE source_message_id='REPLY-FAIL'"
        ).fetchone() == ("completed",)
    assert len(completions.calls) == 2
    assert _quota(db) == 1

    handled, claimed = server._handle_registered_semantic_meal(
        event, "燕麥奶300ml", "U-BOUNDARY", "REPLY-FAIL"
    )

    assert (handled, claimed) == (True, True)
    assert len(completions.calls) == 2
    assert len(delivery_attempts) == 2
    assert delivery_attempts[0].as_json_dict() == delivery_attempts[1].as_json_dict()
    assert _quota(db) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM semantic_meal_batches WHERE source_message_id='REPLY-FAIL'"
        ).fetchone() == ("completed",)
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone() == (1,)
