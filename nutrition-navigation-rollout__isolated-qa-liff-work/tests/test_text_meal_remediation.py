import sqlite3
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
import server


def _event(message_id, text, user_id="U-REMEDIATION"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
        webhook_event_id=f"webhook-{message_id}",
    )


def _setup(tmp_path, monkeypatch, uid="U-REMEDIATION", quota=3):
    db = tmp_path / "remediation.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, quota, 10, today, "vip", "2099-12-31", quota),
        )
        conn.commit()
    return db


@pytest.mark.parametrize("text", [
    "我要記錄飲食 我沒吃鮪魚蛋吐司",
    "我要記錄飲食 我 沒 吃 鮪魚蛋吐司",
    "我要記錄飲食 請問鮪魚蛋吐司熱量",
    "我要記錄飲食 請 問 鮪魚蛋吐司 熱 量",
])
def test_real_text_route_rejects_prefixed_negation_and_question_without_any_ai_or_draft(
    tmp_path, monkeypatch, text,
):
    db = _setup(tmp_path, monkeypatch)
    provider_calls = []
    general_ai_calls = []
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(request) or pytest.fail("estimate provider called"),
    )
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_a, **_k: general_ai_calls.append(True) or pytest.fail("general AI called"),
    )
    server.processed_messages.clear()

    server._handle_message_impl(_event("B1-ROUTE", text))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-REMEDIATION'"
        ).fetchone()[0] == 3
    assert provider_calls == []
    assert general_ai_calls == []
    assert len(replies) == 1


def test_b1_gate_preserves_supported_positive_forms_and_explicit_values():
    assert server.parse_natural_food_log_intent(
        "我要記錄飲食 鮪魚蛋吐司"
    )["food_name"] == "鮪魚蛋吐司"
    parsed = server.parse_natural_food_log_intent("早餐吃一份鮪魚蛋吐司")
    assert parsed["food_name"] == "鮪魚蛋吐司"
    assert parsed["meal_slot"] == "早餐"
    assert parsed["amount"] == 1.0
    explicit = server.parse_explicit_text_nutrition_log(
        "鮪魚蛋吐司 熱量350大卡 蛋白質17g"
    )
    assert explicit["calories_kcal"] == 350
    assert explicit["protein_g"] == 17


def _estimate(food_name="測試餐"):
    return {
        "food_name": food_name,
        "portion_assumption": "1份",
        "calories_kcal": {"estimate": 300, "min": 250, "max": 350},
        "protein_g": {"estimate": 15, "min": 10, "max": 20},
        "provenance": {
            "provider": "offline", "model": "mock", "method": "text_meal_estimate"
        },
    }


def test_expired_crash_lease_refunds_once_then_same_message_retries(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch, uid="U-CRASH", quota=2)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    request = {"food_name": "中斷餐", "amount": 1.0, "unit": "serving", "meal_slot": "早餐"}
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda _request: (_ for _ in ()).throw(KeyboardInterrupt("worker died")),
    )

    with pytest.raises(KeyboardInterrupt):
        server.create_text_meal_estimate_draft(
            user_id="U-CRASH", message_id="CRASH-1", request=request
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM pending_text_meal_estimates"
        ).fetchone()[0] == "estimating"
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-CRASH'"
        ).fetchone()[0] == 1
    with pytest.raises(ValueError, match="正在處理"):
        server.create_text_meal_estimate_draft(
            user_id="U-CRASH", message_id="CRASH-1", request=request
        )

    clock[0] += timedelta(minutes=3)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda _request: _estimate("中斷餐"))
    draft = server.create_text_meal_estimate_draft(
        user_id="U-CRASH", message_id="CRASH-1", request=request
    )

    assert draft["status"] == "pending"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-CRASH'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
        ).fetchall() == [("charged", 1), ("refunded", 1)]


def test_expired_worker_late_result_is_fenced_from_new_attempt(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch, uid="U-FENCE", quota=2)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    entered = threading.Event()
    release = threading.Event()
    call_lock = threading.Lock()
    calls = [0]

    def provider(_request):
        with call_lock:
            calls[0] += 1
            number = calls[0]
        if number == 1:
            entered.set()
            assert release.wait(5)
            return _estimate("舊工作結果")
        return _estimate("新工作結果")

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    request = {"food_name": "競態餐", "amount": 1.0, "unit": "serving", "meal_slot": "午餐"}
    old_errors = []
    worker = threading.Thread(
        target=lambda: _capture_error(
            old_errors,
            lambda: server.create_text_meal_estimate_draft(
                user_id="U-FENCE", message_id="FENCE-1", request=request
            ),
        )
    )
    worker.start()
    assert entered.wait(5)
    clock[0] += timedelta(minutes=3)
    fresh = server.create_text_meal_estimate_draft(
        user_id="U-FENCE", message_id="FENCE-1", request=request
    )
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert fresh["estimate"]["food_name"] == "新工作結果"
    assert len(old_errors) == 1 and "狀態已變更" in str(old_errors[0])
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status,estimate_json FROM pending_text_meal_estimates"
        ).fetchone()
        assert row[0] == "pending"
        assert "新工作結果" in row[1] and "舊工作結果" not in row[1]
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-FENCE'"
        ).fetchone()[0] == 1


def _capture_error(errors, operation):
    try:
        operation()
    except BaseException as exc:
        errors.append(exc)


def test_old_pending_draft_schema_migrates_before_first_write(tmp_path, monkeypatch):
    db = tmp_path / "old-schema.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE pending_text_meal_estimates (
               token TEXT PRIMARY KEY,user_id TEXT NOT NULL,source_message_id TEXT NOT NULL,
               request_json TEXT NOT NULL,estimate_json TEXT NOT NULL DEFAULT '{}',
               portion_multiplier REAL NOT NULL DEFAULT 1,meal_slot TEXT NOT NULL DEFAULT '',
               status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
               confirmed_log_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,expires_at TEXT NOT NULL,
               UNIQUE(user_id,source_message_id))"""
        )
        server.ensure_daily_food_ledger_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_text_meal_estimates)")}
        ledger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_meal_estimate_quota_ledger'"
        ).fetchone()
    assert {"lease_owner", "lease_expires_at"} <= columns
    assert ledger == (1,)
