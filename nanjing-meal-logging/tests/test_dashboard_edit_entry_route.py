import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


def _text_event(message_id, text, user_id):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


def _postback_event(event_id, data, user_id):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{event_id}",
        webhook_event_id=event_id,
    )


def _dashboard_action(payload, label):
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            action = node.get("action")
            if isinstance(action, dict) and action.get("label") == label:
                return action
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    raise AssertionError(f"dashboard action not found: {label}")


@pytest.mark.parametrize("initial_quota", [19, 0])
def test_dashboard_edit_button_reaches_owner_ledger_picker_without_ai_quota_or_writes(
    tmp_path, monkeypatch, initial_quota,
):
    uid = f"U-EDIT-ENTRY-{initial_quota}"
    db_dir = tmp_path / f"entry-{initial_quota}"
    db_path = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, initial_quota, 48, today, "vip", "2099-12-31", 20),
        )
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,
                today_food_items,today_date)
               VALUES (?,?,?,?,0,0,'',?)""",
            (uid, "入口測試", 2000, 100, today),
        )
        server.create_daily_food_log(
            conn, user_id=uid, product_name="本人早餐", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 300, "protein_g": 20},
            source_type="user_private_food",
        )
        server.create_daily_food_log(
            conn, user_id="U-FOREIGN", product_name="他人私密餐", meal_slot="早餐",
            consumed_at=f"{today}T09:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 999, "protein_g": 99},
            source_type="user_private_food",
        )
        conn.commit()

    dashboard = server.build_dashboard_flex(uid)
    button_action = _dashboard_action(
        dashboard.as_json_dict(), "今日明細"
    )
    button_text = button_action["text"]
    assert button_text == "我要修改飲食紀錄"

    with sqlite3.connect(db_path) as conn:
        quota_before = conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id=?", (uid,)
        ).fetchone()[0]
        food_before = conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id=?", (uid,)
        ).fetchone()[0]
        events_before = conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE user_id=?", (uid,)
        ).fetchone()[0]

    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message",
        lambda _token, message: replies.append(message),
    )
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("general AI must not run for ledger browse")
        ),
    )
    server.processed_messages.clear()
    event = _text_event(f"EDIT-ENTRY-{initial_quota}", button_text, uid)

    server.handle_message(event)
    server.handle_message(event)

    assert len(replies) == 1
    picker_rendered = json.dumps(replies[0].as_json_dict(), ensure_ascii=False)
    assert "今天的紀錄" in picker_rendered
    assert "昨天的紀錄" in picker_rendered
    assert "foodlog:v1:day:today:page:0" in picker_rendered
    assert "foodlog:v1:day:yesterday:page:0" in picker_rendered

    server.handle_postback_event(_postback_event(
        f"EDIT-ENTRY-DAY-{initial_quota}",
        "foodlog:v1:day:today:page:0", uid,
    ))
    assert len(replies) == 2
    ledger_rendered = json.dumps(replies[1].as_json_dict(), ensure_ascii=False)
    assert "本人早餐" in ledger_rendered
    assert "他人私密餐" not in ledger_rendered
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id=?", (uid,)
        ).fetchone()[0] == quota_before
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id=?", (uid,)
        ).fetchone()[0] == food_before
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE user_id=?", (uid,)
        ).fetchone()[0] == events_before


def test_pending_ledger_edit_cancel_still_wins_before_browse_and_ai(tmp_path, monkeypatch):
    uid = "U-EDIT-CANCEL"
    db_dir = tmp_path / "pending-cancel"
    db_path = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, 7, 48, today, "vip", "2099-12-31", 20),
        )
        log = server.create_daily_food_log(
            conn, user_id=uid, product_name="取消前原餐", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 400, "protein_g": 30},
            source_type="user_private_food",
        )
        conn.commit()
    server.set_daily_food_edit_state(uid, log["log_id"], 1, "rename")

    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message",
        lambda _token, message: replies.append(message),
    )
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("general AI must not run for pending edit cancel")
        ),
    )
    server.processed_messages.clear()

    server.handle_message(_text_event("EDIT-CANCEL-1", "取消修改", uid))

    assert len(replies) == 1
    assert replies[0].text == "已取消修改飲食紀錄。"
    assert server.get_daily_food_edit_state(uid) is None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT fc.product_name,fl.version
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.log_id=? AND fl.user_id=?""",
            (log["log_id"], uid),
        ).fetchone()
        assert row == ("取消前原餐", 1)
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id=?", (uid,)
        ).fetchone()[0] == 7
