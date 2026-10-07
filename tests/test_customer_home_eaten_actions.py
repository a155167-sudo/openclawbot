import json
import sqlite3
from types import SimpleNamespace

import pytest

import server
from customer_navigation import build_customer_home_contents


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _message_actions(payload):
    return [
        node["action"]
        for node in _walk(payload)
        if node.get("type") == "button"
        and isinstance(node.get("action"), dict)
        and node["action"].get("type") == "message"
    ]


def _home_data(**overrides):
    data = {
        "name": "按鈕測試會員",
        "today_label": "今天",
        "extra_cal": 0,
        "tdee": 1800,
        "extra_pro": 0,
        "protein_goal": 100,
        "food_list": [],
        "today_lunch": "舒肥雞胸餐",
        "today_dinner": "烤鮭魚餐",
        "lunch_checked": False,
        "dinner_checked": False,
        "ai_estimated_count": 0,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("slot", "action_text"),
    (("午餐", "午餐已吃"), ("晚餐", "晚餐已吃")),
)
def test_home_only_emits_existing_eaten_command_for_planned_unchecked_slot(slot, action_text):
    checked_key = "lunch_checked" if slot == "午餐" else "dinner_checked"
    meal_key = "today_lunch" if slot == "午餐" else "today_dinner"

    available = build_customer_home_contents(_home_data())
    actions = _message_actions(available)
    assert any(action.get("text") == action_text for action in actions)

    checked = build_customer_home_contents(_home_data(**{checked_key: True}))
    assert not any(action.get("text") == action_text for action in _message_actions(checked))

    unplanned = build_customer_home_contents(_home_data(**{meal_key: "尚未安排"}))
    assert not any(action.get("text") == action_text for action in _message_actions(unplanned))


@pytest.mark.parametrize("empty_meal", ("", "   ", "無", "尚未安排"))
def test_home_does_not_emit_eaten_action_for_empty_meal_sentinels(empty_meal):
    contents = build_customer_home_contents(_home_data(today_lunch=empty_meal))

    assert not any(
        action.get("text") == "午餐已吃" for action in _message_actions(contents)
    )


def _event(message_id, text, uid):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=uid),
        reply_token=f"reply-{message_id}",
    )


def test_real_rendered_eaten_action_routes_through_registered_handler_to_idempotent_producer(
    tmp_path, monkeypatch,
):
    uid = "U-HOME-EATEN-ROUTE"
    db_dir = tmp_path / "isolated-data"
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
            (uid, 9, 48, today, "vip", "2099-12-31", 20),
        )
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,
                today_food_items,today_date)
               VALUES (?,?,?,?,0,0,'',?)""",
            (uid, "按鈕測試會員", 1800, 100, today),
        )
        conn.commit()

    def dashboard_data(_uid, *, scope="full"):
        assert _uid == uid
        with sqlite3.connect(db_path) as conn:
            hp = conn.execute(
                """SELECT today_extra_cal,today_extra_pro,today_food_items
                   FROM health_profile WHERE user_id=?""",
                (uid,),
            ).fetchone()
            checked = conn.execute(
                """SELECT 1 FROM planned_meal_checks
                   WHERE user_id=? AND meal_date=? AND meal_slot='午餐'""",
                (uid, today),
            ).fetchone()
        return _home_data(
            extra_cal=hp[0] or 0,
            extra_pro=hp[1] or 0,
            food_list=[item for item in str(hp[2] or "").split("、") if item],
            lunch_checked=bool(checked),
            lunch_cal=500,
            lunch_pro=40,
        )

    monkeypatch.setattr(server, "get_dashboard_data", dashboard_data)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("planned-meal confirmation must not invoke AI")
        ),
    )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda token, message: replies.append((token, message)),
    )
    server.processed_messages.clear()

    rendered = server.build_dashboard_flex(uid).as_json_dict()
    action = next(
        action for action in _message_actions(rendered)
        if action.get("text") == "午餐已吃"
    )
    assert action == {
        "type": "message",
        "label": "午餐已吃",
        "text": "午餐已吃",
    }
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id=?", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT today_extra_cal,today_extra_pro,today_food_items
               FROM health_profile WHERE user_id=?""",
            (uid,),
        ).fetchone() == (0.0, 0.0, "")

    server.handle_message(_event("HOME-EATEN-1", action["text"], uid))
    server.handle_message(_event("HOME-EATEN-2", action["text"], uid))

    assert len(replies) == 2
    assert "已經確認過了" in replies[1][1].as_json_dict()["text"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id=?", (uid,)
        ).fetchone()[0] == 1
        assert conn.execute(
            """SELECT COUNT(*) FROM planned_meal_checks
               WHERE user_id=? AND meal_date=? AND meal_slot='午餐'""",
            (uid, today),
        ).fetchone()[0] == 1
        totals = conn.execute(
            """SELECT today_extra_cal,today_extra_pro,today_food_items
               FROM health_profile WHERE user_id=?""",
            (uid,),
        ).fetchone()
    assert totals == (500.0, 40.0, "舒肥雞胸餐")

    rerendered = server.build_dashboard_flex(uid).as_json_dict()
    assert not any(
        button.get("text") == "午餐已吃"
        for button in _message_actions(rerendered)
    )
    assert "舒肥雞胸餐" in json.dumps(rerendered, ensure_ascii=False)
