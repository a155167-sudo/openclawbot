import json
import sqlite3
from types import SimpleNamespace

import pytest

import server
from meal_photo_system import (
    apply_meal_photo_action,
    build_meal_photo_estimate_bubble,
    get_meal_photo_draft,
    save_meal_photo_draft,
)


def _payload():
    items = [
        {"name": "雞胸", "portion": "1份", "calories_kcal": 200, "protein_g": 35},
        {"name": "青菜", "portion": "1碗", "calories_kcal": 40, "protein_g": 2},
    ]
    return {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": [
            {"name": item["name"], "category": "unknown", "confidence": 0.9}
            for item in items
        ],
        "uncertain_items": [],
        "starch_visibility": "not_visible",
        "oil_sauce_status": "unknown",
        "observed_at_confidence": 0.9,
        "ai_estimate": {
            "items": items,
            "calories_kcal": {"estimate": 240, "min": 190, "max": 300},
            "protein_g": {"estimate": 37, "min": 30, "max": 44},
            "confidence": 0.8,
            "provenance": {
                "provider": "fixture",
                "model": "offline",
                "method": "vision_model_estimate",
                "nutrition_basis": "unlabeled_meal_photo",
            },
        },
    }


def _postback(data, event_id, user_id="U1"):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{event_id}",
        webhook_event_id=event_id,
        timestamp=1,
    )


def _text(text, message_id, user_id="U1"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


def _actions(node):
    if hasattr(node, "as_json_dict"):
        node = node.as_json_dict()
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("action"), dict):
            found.append(node["action"])
        for value in node.values():
            found.extend(_actions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_actions(value))
    return found


def _cancel_actions(message):
    return [
        action for action in _actions(message)
        if action.get("type") == "postback" and action.get("label") == "取消新增"
    ]


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "photo-add-cancel-ux.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("generic AI forbidden")),
    )
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ingredient AI forbidden")),
    )
    monkeypatch.setattr(
        server, "check_permission_and_quota",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("quota forbidden")),
    )
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn,
            user_id="U1",
            source_message_id="PHOTO-ADD-CANCEL",
            payload=_payload(),
            meal_slot="午餐",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply)
    )
    server.processed_messages.clear()
    return db, token, draft, replies


def _enter_add(token, draft, replies):
    card = build_meal_photo_estimate_bubble(draft)
    add = next(action for action in _actions(card) if action.get("label") == "➕ 新增食材")
    server.handle_postback_event(_postback(add["data"], "ENTER-ADD"))
    return replies[-1]


def test_add_prompt_is_short_text_with_serialized_quick_reply_cancel(tmp_path, monkeypatch):
    _db, token, draft, replies = _setup(tmp_path, monkeypatch)

    prompt = _enter_add(token, draft, replies)

    assert prompt.type == "text"
    assert prompt.text == "請輸入要新增的食材與份量，可一次輸入多項。"
    actions = _cancel_actions(prompt)
    assert len(actions) == 1
    assert actions[0]["data"] == f"mp:v1:{token}:2:cancel_add"
    assert actions[0]["displayText"] == "確認取消新增食材"
    serialized = prompt.as_json_dict()
    assert serialized["quickReply"]["items"][0]["action"] == actions[0]
    assert "contents" not in serialized


@pytest.mark.parametrize("typed", ["取消", "取消新增", "取消新增食材"])
def test_exact_typed_cancel_immediately_returns_latest_same_meal_card_without_side_effects(
    tmp_path, monkeypatch, typed
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _enter_add(token, draft, replies)
    replies.clear()

    server.handle_message(_text(typed, f"TEXT-CANCEL-{typed}"))

    assert replies[-1].type == "flex"
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "雞胸" in rendered and "青菜" in rendered and "午餐" in rendered
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"], current["meal_slot"]) == (
            "estimated", 3, "午餐"
        )
        assert [item["name"] for item in current["payload"]["ai_estimate"]["items"]] == [
            "雞胸", "青菜"
        ]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='cancel_add'"
        ).fetchone()[0] == 1


def test_typed_cancel_replay_is_idempotent_and_returns_current_card(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _enter_add(token, draft, replies)
    event = _text("取消新增", "TEXT-CANCEL-REPLAY")

    server.handle_message(event)
    server.processed_messages.discard(event.message.id)
    server.handle_message(event)

    assert replies[-1].type == "flex"
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("estimated", 3)
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='cancel_add'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_cancel_postback_replay_with_new_delivery_id_is_idempotent(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    prompt = _enter_add(token, draft, replies)
    cancel_data = _cancel_actions(prompt)[0]["data"]

    server.handle_postback_event(_postback(cancel_data, "CANCEL-ONE"))
    server.handle_postback_event(_postback(cancel_data, "CANCEL-TWO"))

    assert replies[-1].type == "flex"
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("estimated", 3)
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='cancel_add'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_stale_cancel_during_add_only_refreshes_current_cancel_control(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _enter_add(token, draft, replies)

    server.handle_postback_event(
        _postback(f"mp:v1:{token}:1:cancel_add", "STALE-CANCEL")
    )

    assert replies[-1].type == "text"
    assert replies[-1].text == "請輸入要新增的食材與份量，可一次輸入多項。"
    assert _cancel_actions(replies[-1])[0]["data"] == f"mp:v1:{token}:2:cancel_add"
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("awaiting_item_name", 2)
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='cancel_add'"
        ).fetchone()[0] == 0


def test_other_current_card_action_during_add_does_not_mutate_and_shows_visible_cancel(
    tmp_path, monkeypatch
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _enter_add(token, draft, replies)

    server.handle_postback_event(
        _postback(f"mp:v1:{token}:2:meal:晚餐", "OTHER-ACTION-WHILE-ADDING")
    )

    assert replies[-1].type == "text"
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "請輸入要新增的食材與份量，可一次輸入多項。" in rendered
    assert _cancel_actions(replies[-1])[0]["data"] == f"mp:v1:{token}:2:cancel_add"
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"], current["meal_slot"]) == (
            "awaiting_item_name", 2, "午餐"
        )
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_foreign_user_never_receives_cancel_control_or_meal_metadata(tmp_path, monkeypatch):
    _db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _enter_add(token, draft, replies)
    replies.clear()

    server.handle_postback_event(
        _postback(f"mp:v1:{token}:2:meal:晚餐", "FOREIGN-ACTION", user_id="U2")
    )

    serialized = json.dumps(
        replies[-1].as_json_dict() if hasattr(replies[-1], "as_json_dict") else {"text": replies[-1].text},
        ensure_ascii=False,
    )
    assert token not in serialized
    assert "雞胸" not in serialized and "青菜" not in serialized and "午餐" not in serialized
    assert not _cancel_actions(replies[-1])


def test_expired_add_draft_does_not_offer_a_cancel_that_can_resurrect_it(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _enter_add(token, draft, replies)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()

    server.handle_postback_event(
        _postback(f"mp:v1:{token}:2:cancel_add", "EXPIRED-CANCEL")
    )

    assert not _cancel_actions(replies[-1])
    assert "逾時" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(
            conn, user_id="U1", token=token, allow_expired=True
        )["status"] == "expired"
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
