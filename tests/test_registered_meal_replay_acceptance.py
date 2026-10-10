import sqlite3
from types import SimpleNamespace

from tests.test_sheet_atomic_meal_updates import _setup
import server


def _setup_vip(db_path, uid):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO usage(user_id,status,expiry_date,remaining_meals,remaining_chat_quota,daily_chat_limit,last_date) "
            "VALUES (?,'vip','2099-12-31',99,10,10,'2099-10-01')", (uid,)
        )


# Kept for adjacent acceptance modules that share this registered-handler setup.
_setup_vip_for_entrypoint = _setup_vip


def _send(uid, text, msg_id, monkeypatch):
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message.text))
    server.handle_message(SimpleNamespace(
        message=SimpleNamespace(id=msg_id, text=text), source=SimpleNamespace(user_id=uid), reply_token="reply-" + msg_id
    ))
    return replies


_send_message = _send


def test_registered_general_ai_swap_is_safe_refusal(tmp_path, monkeypatch):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _setup_vip(db_path, uid)
    server.processed_messages.clear()
    replies = _send(uid, "把 2099/10/01 午餐與 2099/10/02 午餐互換", "M1", monkeypatch)
    assert book.batch_calls == []
    assert "本次未修改菜單" in replies[0]
    assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("餐A", "餐B")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT summary_text FROM health_profile WHERE user_id=?", (uid,)).fetchone()[0] == "before"


def test_registered_repeated_ai_swap_stays_zero_write(tmp_path, monkeypatch):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _setup_vip(db_path, uid)
    server.processed_messages.clear()
    first = _send(uid, "把 2099/10/01 午餐與 2099/10/02 午餐互換", "M1", monkeypatch)
    server.processed_messages.clear()
    second = _send(uid, "把 2099/10/01 午餐與 2099/10/02 午餐互換", "M1", monkeypatch)
    assert book.batch_calls == []
    assert "本次未修改菜單" in first[0] and "本次未修改菜單" in second[0]
    assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("餐A", "餐B")


def test_explicit_defer_command_still_bypasses_general_ai(tmp_path, monkeypatch):
    uid, db_path, _, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _setup_vip(db_path, uid)
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("general AI forbidden")))
    server.processed_messages.clear()
    replies = _send(uid, "#延餐 10/15 午餐 -> 10/17 晚餐", "DEFER", monkeypatch)
    assert replies == ["❌ 找不到原餐：10/15 午餐"]
    assert book.batch_calls == []
