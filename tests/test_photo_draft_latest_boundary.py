import sqlite3

import pytest
import server
from meal_photo_system import get_meal_photo_draft, save_meal_photo_draft
from tests.test_photo_ingredient_controls import _payload, _postback, _text


def _save(conn, message_id, user_id="U1"):
    return save_meal_photo_draft(
        conn,
        user_id=user_id,
        source_message_id=message_id,
        payload=_payload(),
        source_image_ref=f"nutrition-image:{message_id.lower().ljust(32, 'a')[:32]}.jpg",
        meal_slot="晚餐",
        workflow_version="user_confirmed_ai_nutrition_v2",
    )


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "latest-boundary.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "left"))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    adjustment_calls = []
    monkeypatch.setattr(
        server,
        "_estimate_adjusted_meal_photo",
        lambda *args, **kwargs: adjustment_calls.append((args, kwargs))
        or (_ for _ in ()).throw(AssertionError("text crossed the newest-draft boundary")),
    )
    monkeypatch.setattr(server, "should_ai_create_food_log", lambda _msg: False)
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_args, **_kwargs: ("一般對話", None))
    return db, replies, adjustment_calls


def _old_adjust_then_new(db, newest_status):
    with sqlite3.connect(db) as conn:
        old = _save(conn, "OLD")
    server.handle_postback_event(_postback(f"mp:v1:{old}:1:request_adjust", "OLD-ADJUST"))
    with sqlite3.connect(db) as conn:
        new = _save(conn, "NEW")
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status=? WHERE token=?",
            (newest_status, new),
        )
        conn.commit()
    return old, new


@pytest.mark.parametrize(
    "newest_status",
    ["expired", "cancelled", "user_confirmed", "awaiting_confirmation", "estimated"],
)
def test_newest_non_input_status_blocks_fallback_to_older_input(
    tmp_path, monkeypatch, newest_status
):
    db, replies, adjustment_calls = _setup(tmp_path, monkeypatch)
    old, new = _old_adjust_then_new(db, newest_status)

    server.processed_messages.clear()
    server.handle_message(_text("下一段一般文字", f"TEXT-{newest_status}"))

    assert adjustment_calls == []
    assert replies[-1].text.startswith("我不確定你的需求")
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT token,status,observed_payload_json FROM pending_meal_photo_drafts ORDER BY rowid"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0:2] == (old, "awaiting_adjustment")
    assert rows[0][2] != "{}"
    assert rows[1][0:2] == (new, newest_status)


def test_newest_input_expiring_once_never_exposes_older_input_to_next_text(tmp_path, monkeypatch):
    db, replies, adjustment_calls = _setup(tmp_path, monkeypatch)
    old, new = _old_adjust_then_new(db, "awaiting_item_name")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (new,),
        )
        conn.commit()

    server.processed_messages.clear()
    server.handle_message(_text("木耳約20g", "EXPIRE-NEWEST-ONCE"))
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=new, allow_expired=True)["status"] == "expired"

    server.processed_messages.clear()
    server.handle_message(_text("下一段一般文字", "AFTER-NEWEST-EXPIRED"))

    assert adjustment_calls == []
    assert replies[-1].text.startswith("我不確定你的需求")
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=old)["status"] == "awaiting_adjustment"
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["request_adjust", "request_add"])
def test_retained_older_card_cannot_claim_input_while_newer_draft_exists(
    tmp_path, monkeypatch, action
):
    db, replies, _calls = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        old = _save(conn, "OLD-CARD")
        new = _save(conn, "NEW-CARD")

    server.handle_postback_event(_postback(f"mp:v1:{old}:1:{action}", f"OLD-{action}"))

    assert "較舊餐點" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=old)["status"] == "estimated"
        assert get_meal_photo_draft(conn, user_id="U1", token=new)["status"] == "estimated"
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0] == 0


def test_latest_boundary_is_scoped_per_owner(tmp_path, monkeypatch):
    db, replies, adjustment_calls = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        u1_old = _save(conn, "U1-OLD", "U1")
        u2_old = _save(conn, "U2-OLD", "U2")
    server.handle_postback_event(_postback(f"mp:v1:{u1_old}:1:request_adjust", "U1-ADJUST", "U1"))
    server.handle_postback_event(_postback(f"mp:v1:{u2_old}:1:request_adjust", "U2-ADJUST", "U2"))
    with sqlite3.connect(db) as conn:
        u1_new = _save(conn, "U1-NEW", "U1")

    server.processed_messages.clear()
    server.handle_message(_text("U1 一般文字", "U1-TEXT", "U1"))
    assert adjustment_calls == []
    assert replies[-1].text.startswith("我不確定你的需求")

    # U2 has no newer draft, so its own active adjustment remains routable.  Avoid
    # provider execution here: the selector itself proves owner-local resolution.
    with sqlite3.connect(db) as conn:
        assert server._latest_meal_photo_draft_for_text(conn, "U1")[0] == u1_new
        assert server._latest_meal_photo_draft_for_text(conn, "U2")[0] == u2_old
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone()[0] == 3
