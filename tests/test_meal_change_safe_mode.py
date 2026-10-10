import sqlite3
from types import SimpleNamespace

import pytest

import server
from tests.test_sheet_atomic_meal_updates import _setup


TAG = "[SWAP_MEAL: 2099/10/01_午餐, 2099/10/02_午餐]"


def _vip(db_path, uid):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO usage(user_id,status,expiry_date,remaining_meals,remaining_chat_quota,daily_chat_limit,last_date) "
            "VALUES (?,'vip','2099-12-31',99,10,10,'2099-10-01')",
            (uid,),
        )


def _answer(monkeypatch, answer):
    monkeypatch.setattr(
        server,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=answer))]
        )))),
    )


def _registered(tmp_path, monkeypatch, *, message, answer, message_id, history=None):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _vip(db_path, uid)
    _answer(monkeypatch, answer)
    server.user_memory[uid] = list(history or [])
    server.processed_messages.clear()
    replies, pushes = [], []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, obj: replies.append(obj.text))
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda *args: pushes.append(args))
    event = SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=message),
        source=SimpleNamespace(user_id=uid),
        reply_token="reply-" + message_id,
    )
    server.handle_message(event)
    with sqlite3.connect(db_path) as conn:
        food_count = conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0]
    return replies[0], pushes, worksheet, book, food_count


def _assert_safe(result):
    reply, pushes, worksheet, book, food_count = result
    assert "本次未修改菜單" in reply
    assert "#延餐 10/15 午餐 -> 10/17 晚餐" in reply
    assert "已調整完成" not in reply
    assert "成功將" not in reply
    assert pushes == []
    assert book.batch_calls == []
    assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("餐A", "餐B")
    assert food_count == 0


def test_registered_incident_no_tag_false_success_is_blocked(tmp_path, monkeypatch):
    _assert_safe(_registered(
        tmp_path, monkeypatch, message="10/23午晚搬到9/26",
        answer="✅ 已調整完成：午餐與晚餐都搬到 9/26", message_id="SAFE-INCIDENT",
    ))


def test_registered_direct_confirmation_cannot_execute_or_claim_success(tmp_path, monkeypatch):
    _assert_safe(_registered(
        tmp_path, monkeypatch, message="對", answer="✅ 已調整完成：午餐與晚餐都搬到 9/26",
        message_id="SAFE-CONFIRM", history=[
            {"role": "user", "content": "10/23午晚搬到9/26"},
            {"role": "assistant", "content": "請確認是否調整？"},
        ],
    ))


def test_registered_shorthand_multi_meal_single_tag_cannot_partially_write(tmp_path, monkeypatch):
    _assert_safe(_registered(
        tmp_path, monkeypatch, message="把 2099/10/01 午晚餐改到 2099/10/02",
        answer=f"✅ 都改好了 {TAG}", message_id="SAFE-SHORTHAND",
    ))


def test_registered_stale_confirmation_does_not_search_arbitrary_history(tmp_path, monkeypatch):
    _assert_safe(_registered(
        tmp_path, monkeypatch, message="好", answer=f"✅ 已換好 {TAG}", message_id="SAFE-STALE",
        history=[
            {"role": "user", "content": "把 2099/10/01 午餐改到 2099/10/02 午餐"},
            {"role": "assistant", "content": "請確認"},
            {"role": "user", "content": "一顆蛋幾卡？"},
            {"role": "assistant", "content": "約 70 大卡"},
        ],
    ))


def test_registered_hypothetical_question_with_tag_never_executes(tmp_path, monkeypatch):
    _assert_safe(_registered(
        tmp_path, monkeypatch,
        message="如果把 2099/10/01 午餐改到 2099/10/02 午餐，營養會怎樣？",
        answer=f"可以，已換好 {TAG}", message_id="SAFE-HYPOTHETICAL",
    ))


def test_registered_compound_food_log_and_meal_change_is_zero_write(tmp_path, monkeypatch):
    _assert_safe(_registered(
        tmp_path, monkeypatch,
        message="請用一般估算記錄雞胸 200 卡，並把 2099/10/01 午餐改到 2099/10/02 午餐",
        answer=f"✅ 已記錄也換好了 [LOG_NUTRITION: CAL=200, PRO=30, NAME=雞胸] {TAG}",
        message_id="SAFE-COMPOUND",
    ))
