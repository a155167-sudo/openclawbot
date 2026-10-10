import json
import sqlite3

import server
from meal_photo_system import get_meal_photo_draft, save_meal_photo_draft
from tests.test_photo_ingredient_controls import _payload, _postback, _text


def _save_v2(conn, *, user_id, message_id):
    return save_meal_photo_draft(
        conn,
        user_id=user_id,
        source_message_id=message_id,
        payload=_payload(),
        source_image_ref=f"nutrition-image:{message_id.lower().replace('-', 'a')[:32].ljust(32, 'a')}.jpg",
        meal_slot="晚餐",
        workflow_version="user_confirmed_ai_nutrition_v2",
    )


def _estimate(request):
    grams = float(request["amount"]) if request["unit"] == "g" else 30.0
    return {
        "food_name": request["food_name"],
        "portion_assumption": request["unit"],
        "calories_kcal": {"estimate": grams, "min": grams * 0.8, "max": grams * 1.2},
        "protein_g": {"estimate": grams * 0.1, "min": 0, "max": grams * 0.2},
        "provenance": {"provider": "test", "model": "offline", "method": "text_meal_estimate"},
    }


def test_newer_add_draft_owns_text_when_older_draft_awaits_ai_adjustment(tmp_path, monkeypatch):
    """Real callback→text order: old adjust, new add, stale old add, then four foods."""
    db = tmp_path / "two-draft-routing.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "left"))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    add_provider_calls = []
    adjustment_provider_calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: add_provider_calls.append(dict(request)) or _estimate(request),
    )
    monkeypatch.setattr(
        server,
        "_estimate_adjusted_meal_photo",
        lambda *_args, **_kwargs: adjustment_provider_calls.append((_args, _kwargs))
        or (_ for _ in ()).throw(AssertionError("new add text must not enter adjustment provider")),
    )

    with sqlite3.connect(db) as conn:
        old_token = _save_v2(conn, user_id="U1", message_id="OLD-PHOTO")
    server.handle_postback_event(
        _postback(f"mp:v1:{old_token}:1:request_adjust", "OLD-REQUEST-ADJUST")
    )

    with sqlite3.connect(db) as conn:
        new_token = _save_v2(conn, user_id="U1", message_id="NEW-PHOTO")
    server.handle_postback_event(
        _postback(f"mp:v1:{new_token}:1:request_add", "NEW-REQUEST-ADD")
    )
    # A retained control from the old meal must not mutate or steal the new input owner.
    server.handle_postback_event(
        _postback(f"mp:v1:{old_token}:1:request_add", "OLD-STALE-ADD")
    )
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=old_token)["status"] == "awaiting_adjustment"
        assert get_meal_photo_draft(conn, user_id="U1", token=new_token)["status"] == "awaiting_item_name"

    text = "木耳約20g、牛番茄約1/6顆、金針菇約20g、青菜約20g"
    server.processed_messages.clear()
    server.handle_message(_text(text, "FOUR-FOODS-AFTER-COLLISION"))

    assert adjustment_provider_calls == []
    assert [call["food_name"] for call in add_provider_calls] == ["木耳", "牛番茄", "金針菇", "青菜"]
    with sqlite3.connect(db) as conn:
        old_draft = get_meal_photo_draft(conn, user_id="U1", token=old_token)
        new_draft = get_meal_photo_draft(conn, user_id="U1", token=new_token)
        assert old_draft["status"] == "awaiting_adjustment"
        assert new_draft["status"] == "estimated"
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        batch_token = conn.execute(
            "SELECT token FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0]
        assert batch_token == new_token


def test_expired_newer_add_draft_is_not_revived_by_text_pipeline(tmp_path, monkeypatch):
    db = tmp_path / "expired-input-routing.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "left"))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    add_calls = []
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: add_calls.append(dict(request)) or _estimate(request),
    )

    with sqlite3.connect(db) as conn:
        token = _save_v2(conn, user_id="U1", message_id="EXPIRED-PHOTO")
    server.handle_postback_event(
        _postback(f"mp:v1:{token}:1:request_add", "EXPIRED-REQUEST-ADD")
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='expired', expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()

    # Keep the assertion at the real text pipeline while isolating unrelated chat fallback.
    monkeypatch.setattr(server, "should_ai_create_food_log", lambda _msg: False)
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: ("一般對話", None),
    )
    server.processed_messages.clear()
    server.handle_message(_text("木耳約20g", "TEXT-AFTER-EXPIRED-ADD"))

    assert add_calls == []
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token, allow_expired=True)["status"] == "expired"
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0] == 0
