import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import server
from meal_photo_system import (
    build_meal_photo_recorded_bubble,
    create_meal_photo_revision_draft,
    get_meal_photo_draft,
)
from nutrition_system import user_confirmed_meal_photo_trust_projection
from test_server_nutrition_integration import (
    _confirmed_v2_food_log,
    _daily_ledger_db,
    meal_photo_payload,
)


def _postback(data, *, event_id, user_id="U1"):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{event_id}",
        webhook_event_id=event_id,
        timestamp=1784740620000,
    )


def _text(message_id, text, *, user_id="U1"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


def _revised_payload():
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [
            {"name": "雞腿半碗飯", "portion": "飯半碗", "calories_kcal": 510, "protein_g": 31}
        ],
        "calories_kcal": {"estimate": 510, "min": 450, "max": 580},
        "protein_g": {"estimate": 31, "min": 27, "max": 36},
        "confidence": 0.8,
        "provenance": {
            "provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate",
            "nutrition_basis": "unlabeled_meal_photo",
        },
    }
    return payload


def test_confirmed_v2_real_handlers_preview_then_confirm_updates_same_log_once(
    tmp_path, monkeypatch,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "revision-handler-normal.db")
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    image_dir = tmp_path / "nutrition_images"
    image_dir.mkdir(exist_ok=True)
    (image_dir / ("a" * 32 + ".jpg")).write_bytes(b"\xff\xd8\xff" + b"0" * 200)
    original_token, log_id = _confirmed_v2_food_log(db, source_message_id="REV-HANDLER")
    with sqlite3.connect(db) as conn:
        original_draft = conn.execute(
            "SELECT * FROM pending_meal_photo_drafts WHERE token=?", (original_token,)
        ).fetchone()
        confirmed_draft = get_meal_photo_draft(conn, user_id="U1", token=original_token)

    enabled_card = build_meal_photo_recorded_bubble(
        confirmed_draft, allow_confirmed_revision=True, log_id=log_id, log_version=1,
    )
    rendered_card = json.dumps(enabled_card, ensure_ascii=False)
    assert "修改這餐" in rendered_card
    assert f"mealrev:v1:{log_id}:1:start" in rendered_card

    replies = []
    provider_calls = []
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", True)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda image_ref, payload, correction: (
            provider_calls.append((image_ref, payload, correction)) or _revised_payload()
        ),
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    server.processed_messages.clear()

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="REV-START"
    ))
    assert "一句話" in replies[-1].text
    assert server.get_daily_food_edit_state("U1")["input_type"] == "meal_photo_revision_request"

    server.handle_message(_text("REV-TEXT", "白飯改半碗"))
    assert len(provider_calls) == 1
    preview = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "確認修改" in preview
    assert "取消" in preview
    assert "450～580" in preview
    state = server.get_daily_food_edit_state("U1")
    assert state["input_type"] == "meal_photo_revision_preview"
    draft_token = state["payload"]["draft_token"]
    draft_version = state["payload"]["draft_version"]

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:{draft_token}:{draft_version}:confirm",
        event_id="REV-CONFIRM",
    ))

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT version,nutrition_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()
        assert row[0] == 2
        assert json.loads(row[1]) == {"calories_kcal": 510.0, "protein_g": 31.0}
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        assert conn.execute(
            "SELECT * FROM pending_meal_photo_drafts WHERE token=?", (original_token,)
        ).fetchone() == original_draft
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE action='confirm_ai_revision'"
        ).fetchone()[0] == 1
    assert "已修改這餐" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert server.get_daily_food_edit_state("U1") is None


def test_daily_entry_and_success_card_drive_two_real_handler_revisions_to_latest_v3(
    tmp_path, monkeypatch,
):
    db, _original_token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-twice.db"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    payload2 = _revised_payload()
    payload3 = _revised_payload()
    payload3["ai_estimate"]["items"] = [
        {"name": "雞腿半碗飯加蛋", "portion": "一份", "calories_kcal": 590, "protein_g": 38}
    ]
    payload3["ai_estimate"]["calories_kcal"] = {"estimate": 590, "min": 530, "max": 650}
    payload3["ai_estimate"]["protein_g"] = {"estimate": 38, "min": 34, "max": 42}
    provider_payloads = iter((payload2, payload3))
    provider_calls = []
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *_args: provider_calls.append(_args) or next(provider_payloads),
    )

    item_v1 = next(
        item for item in server.get_daily_food_ledger("U1", server.tw_today().isoformat())["items"]
        if item["log_id"] == log_id
    )
    daily_v1 = json.dumps(server._daily_food_item_bubble(item_v1), ensure_ascii=False)
    assert f"mealrev:v1:{log_id}:1:start" in daily_v1

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="DAILY-START-V1"
    ))
    server.handle_message(_text("REV-TEXT-V2", "白飯改半碗"))
    state2 = server.get_daily_food_edit_state("U1")
    assert state2 is not None
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:{state2['payload']['draft_token']}:{state2['payload']['draft_version']}:confirm",
        event_id="REV-CONFIRM-V2",
    ))
    success_v2_message = replies[-1].as_json_dict()
    success_v2 = json.dumps(success_v2_message, ensure_ascii=False)
    success_v2_labels = [
        button["action"].get("label")
        for button in success_v2_message["contents"]["footer"]["contents"]
    ]
    assert "再次修改" in success_v2_labels
    assert f"mealrev:v1:{log_id}:2:start" in success_v2
    assert f"foodlog:v1:{log_id}:2:delete:ask" in success_v2
    assert "重新查看" in success_v2

    with sqlite3.connect(db) as conn:
        before_stale = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="STALE-DAILY-V1"
    ))
    assert "已更新" in replies[-1].text
    assert server.get_daily_food_edit_state("U1") is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before_stale

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:2:start", event_id="SUCCESS-START-V2"
    ))
    server.handle_message(_text("REV-TEXT-V3", "再加一顆蛋"))
    state3 = server.get_daily_food_edit_state("U1")
    assert state3 is not None
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:2:{state3['payload']['draft_token']}:{state3['payload']['draft_version']}:confirm",
        event_id="REV-CONFIRM-V3",
    ))

    success_v3 = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert f"mealrev:v1:{log_id}:3:start" in success_v3
    assert f"foodlog:v1:{log_id}:3:delete:ask" in success_v3
    assert len(provider_calls) == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        version, nutrition_json = conn.execute(
            "SELECT version,nutrition_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()
        assert version == 3
        assert json.loads(nutrition_json) == {"calories_kcal": 590.0, "protein_g": 38.0}
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE action='confirm_ai_revision'"
        ).fetchone()[0] == 2
        projection = user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate"
        )
        assert projection["integrity_status"] == "verified"
        assert projection["log_version"] == 3
        assert projection["nutrition"] == {"calories_kcal": 590.0, "protein_g": 38.0}
    latest = next(
        item for item in server.get_daily_food_ledger("U1", server.tw_today().isoformat())["items"]
        if item["log_id"] == log_id
    )
    assert latest["version"] == 3
    assert latest["nutrition"] == {"calories_kcal": 590.0, "protein_g": 38.0}


def test_writer_flag_off_hides_daily_entry_and_rejects_existing_revision_postback(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-flag-off.db"
    )
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", False)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    item = next(
        item for item in server.get_daily_food_ledger("U1", server.tw_today().isoformat())["items"]
        if item["log_id"] == log_id
    )
    assert "修改這餐" not in json.dumps(server._daily_food_item_bubble(item), ensure_ascii=False)
    disabled_success = server.build_confirmed_meal_photo_revision_success_flex({
        "log_id": log_id, "to_version": 2,
    }).as_json_dict()
    disabled_actions = disabled_success["contents"]["footer"]["contents"]
    assert all(action["action"].get("label") != "修改這餐" for action in disabled_actions)
    assert any(action["action"].get("label") == "重新查看" for action in disabled_actions)
    assert any(
        action["action"].get("data") == f"foodlog:v1:{log_id}:2:delete:ask"
        for action in disabled_actions
    )
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="FLAG-OFF-START"
    ))
    assert "目前未啟用" in replies[-1].text
    assert server.get_daily_food_edit_state("U1") is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before


def _setup_revision_handler(tmp_path, monkeypatch, name):
    db = _daily_ledger_db(tmp_path, monkeypatch, name)
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    image_dir = tmp_path / "nutrition_images"
    image_dir.mkdir(exist_ok=True)
    (image_dir / ("a" * 32 + ".jpg")).write_bytes(b"\xff\xd8\xff" + b"0" * 200)
    token, log_id = _confirmed_v2_food_log(db, source_message_id=name.upper())
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", True)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    server.processed_messages.clear()
    return db, token, log_id


def _begin_and_preview(monkeypatch, log_id, replies):
    calls = []
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *args: calls.append(args) or _revised_payload(),
    )
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START"
    ))
    server.handle_message(_text("REVISION-TEXT", "白飯改半碗"))
    state = server.get_daily_food_edit_state("U1")
    assert state is not None
    return calls, state


def test_revision_preview_cancel_real_handler_preserves_original_log_and_totals(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-cancel.db"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    _calls, state = _begin_and_preview(monkeypatch, log_id, replies)
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
        before_total = conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'"
        ).fetchone()
    token = state["payload"]["draft_token"]
    version = state["payload"]["draft_version"]

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:{token}:{version}:cancel", event_id="CANCEL"
    ))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before
        assert conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'"
        ).fetchone() == before_total
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0] == "cancelled"
    assert "都沒有變更" in replies[-1].text


def test_revision_start_rejects_cross_owner_and_stale_version_before_provider(
    tmp_path, monkeypatch,
):
    _db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-guard.db"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="FOREIGN", user_id="U2"
    ))
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:2:start", event_id="STALE"
    ))

    assert "找不到" in replies[-2].text
    assert "已更新" in replies[-1].text
    assert server.get_daily_food_edit_state("U1") is None
    assert server.get_daily_food_edit_state("U2") is None


def test_revision_provider_timeout_keeps_request_actionable_and_never_changes_log(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-timeout.db"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-TIMEOUT"
    ))
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("provider timeout")),
    )
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("general AI must not run")),
    )

    server.handle_message(_text("TIMEOUT-TEXT", "白飯改半碗"))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_meal_photo_drafts "
            "WHERE workflow_version='confirmed_food_log_revision_v1'"
        ).fetchone()[0] == 0
    assert server.get_daily_food_edit_state("U1")["input_type"] == "meal_photo_revision_request"
    assert "都沒有變更" in replies[-1].text


def test_revision_duplicate_text_reply_retry_calls_provider_once_and_duplicate_confirm_replays(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-replay.db"
    )
    replies = []
    calls = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *_args: calls.append("provider") or _revised_payload(),
    )
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-REPLAY"
    ))
    failed = {"value": False}
    def flaky_reply(_token, message):
        if not failed["value"]:
            failed["value"] = True
            raise RuntimeError("reply failed")
        replies.append(message)
    monkeypatch.setattr(server.line_bot_api, "reply_message", flaky_reply)
    event = _text("REPLAY-TEXT", "白飯改半碗")
    try:
        server.handle_message(event)
    except RuntimeError:
        pass
    server.handle_message(event)
    assert calls == ["provider"]
    state = server.get_daily_food_edit_state("U1")
    token = state["payload"]["draft_token"]
    version = state["payload"]["draft_version"]
    confirm = _postback(
        f"mealrev:v1:{log_id}:1:{token}:{version}:confirm", event_id="CONFIRM-REPLAY"
    )

    server.handle_postback_event(confirm)
    server.handle_postback_event(confirm)

    assert "已修改這餐" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)


def test_revision_late_provider_result_cannot_resurrect_cancelled_state(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-late.db"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-LATE"
    ))
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()

    def late_provider(*_args):
        server.clear_daily_food_edit_state("U1")
        return _revised_payload()

    monkeypatch.setattr(server, "_estimate_adjusted_meal_photo", late_provider)
    server.handle_message(_text("LATE-TEXT", "白飯改半碗"))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before
        rows = conn.execute(
            "SELECT status FROM pending_meal_photo_drafts "
            "WHERE workflow_version='confirmed_food_log_revision_v1'"
        ).fetchall()
        assert rows == [] or rows == [("cancelled",)]
    assert server.get_daily_food_edit_state("U1") is None
    assert "已取消" in replies[-1].text


def test_revision_crash_after_preview_commit_recovers_same_preview_without_provider_retry(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-commit-gap.db"
    )
    replies = []
    calls = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *_args: calls.append("provider") or _revised_payload(),
    )
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-COMMIT-GAP"
    ))
    real_complete = server._complete_meal_photo_revision_claim
    monkeypatch.setattr(
        server, "_complete_meal_photo_revision_claim",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit("crash-after-preview-commit")),
    )
    event = _text("COMMIT-GAP-TEXT", "白飯改半碗")
    try:
        server.handle_message(event)
    except SystemExit as exc:
        assert str(exc) == "crash-after-preview-commit"
    else:
        raise AssertionError("fault injection did not terminate the handler")
    with sqlite3.connect(db) as conn:
        state_row = conn.execute(
            "SELECT input_type,payload_json FROM daily_food_edit_states WHERE user_id='U1'"
        ).fetchone()
        assert state_row[0] == "meal_photo_revision_processing"
        payload = json.loads(state_row[1])
        payload["lease_until"] = (server.tw_now() - timedelta(seconds=1)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE daily_food_edit_states SET payload_json=? WHERE user_id='U1'",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
        )
        preview_rows = conn.execute(
            "SELECT token,status FROM pending_meal_photo_drafts "
            "WHERE workflow_version='confirmed_food_log_revision_v1'"
        ).fetchall()
        assert len(preview_rows) == 1 and preview_rows[0][1] == "estimated"
        preview_token = preview_rows[0][0]
        conn.commit()

    monkeypatch.setattr(server, "_complete_meal_photo_revision_claim", real_complete)
    server.processed_messages.clear()
    server.handle_message(event)

    assert calls == ["provider"]
    state = server.get_daily_food_edit_state("U1")
    assert state["input_type"] == "meal_photo_revision_preview"
    assert state["payload"]["draft_token"] == preview_token
    assert "確認修改" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:{preview_token}:{state['payload']['draft_version']}:cancel",
        event_id="CANCEL-RECOVERED-PREVIEW",
    ))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (preview_token,)
        ).fetchone()[0] == "cancelled"


def test_revision_recovery_adopted_preview_survives_original_worker_late_cleanup_and_confirms(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-adopted-race.db"
    )
    replies = []
    calls = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "_estimate_adjusted_meal_photo",
        lambda *_args: calls.append("provider") or _revised_payload(),
    )
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-ADOPTED-RACE"
    ))
    real_complete = server._complete_meal_photo_revision_claim

    def recover_then_finish_old_worker(**kwargs):
        with sqlite3.connect(db) as conn:
            payload = json.loads(conn.execute(
                "SELECT payload_json FROM daily_food_edit_states WHERE user_id='U1'"
            ).fetchone()[0])
            payload["lease_until"] = (
                server.tw_now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE daily_food_edit_states SET payload_json=? WHERE user_id='U1'",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
            )
            conn.commit()
        recovered = server._recover_meal_photo_revision_claim(
            user_id="U1", log_id=log_id, expected_version=1,
            message_id="ADOPTED-RACE-TEXT", correction="白飯改半碗",
        )
        assert recovered["status"] == "preview"
        assert recovered["draft"]["token"] == kwargs["draft"]["token"]
        return real_complete(**kwargs)

    monkeypatch.setattr(
        server, "_complete_meal_photo_revision_claim", recover_then_finish_old_worker
    )
    server.handle_message(_text("ADOPTED-RACE-TEXT", "白飯改半碗"))

    assert calls == ["provider"]
    state = server.get_daily_food_edit_state("U1")
    assert state is not None
    assert state["input_type"] == "meal_photo_revision_preview"
    preview_token = state["payload"]["draft_token"]
    preview_version = state["payload"]["draft_version"]
    with sqlite3.connect(db) as conn:
        preview_draft = get_meal_photo_draft(
            conn, user_id="U1", token=preview_token
        )
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (preview_token,)
        ).fetchone()[0] == "estimated"

    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:{preview_token}:{preview_version}:confirm",
        event_id="CONFIRM-ADOPTED-RACE",
    ))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT version FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (preview_token,)
        ).fetchone()[0] == "revision_confirmed"
    assert server._retire_unadopted_meal_photo_revision_draft(
        user_id="U1", log_id=log_id, from_version=1, draft=preview_draft,
    ) == "already_retired"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (preview_token,)
        ).fetchone()[0] == "revision_confirmed"
    assert "已修改這餐" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)


def test_late_worker_cleanup_cancels_only_its_orphan_and_never_another_owner(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-cleanup-ownership.db"
    )
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_args: None)
    _calls, state = _begin_and_preview(monkeypatch, log_id, [])
    adopted_token = state["payload"]["draft_token"]
    with sqlite3.connect(db) as conn:
        adopted = get_meal_photo_draft(conn, user_id="U1", token=adopted_token)
        orphan = create_meal_photo_revision_draft(
            conn, user_id="U1", log_id=log_id, from_version=1,
            request_text="白飯改半碗", estimate=adopted["estimate"],
            source_message_id="OTHER-LATE-WORKER",
        )
        source_ref = orphan["source_image_ref"]

    assert server._retire_unadopted_meal_photo_revision_draft(
        user_id="U2", log_id=log_id, from_version=1, draft=orphan,
    ) == "already_retired"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (orphan["token"],)
        ).fetchone()[0] == "estimated"

    assert server._retire_unadopted_meal_photo_revision_draft(
        user_id="U1", log_id=log_id, from_version=1, draft=orphan,
    ) == "cancelled"
    with sqlite3.connect(db) as conn:
        orphan_row = conn.execute(
            "SELECT status,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (orphan["token"],),
        ).fetchone()
        assert orphan_row == ("cancelled", source_ref)
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (adopted_token,)
        ).fetchone()[0] == "estimated"

    server.clear_daily_food_edit_state("U1")
    assert server._retire_unadopted_meal_photo_revision_draft(
        user_id="U1", log_id=log_id, from_version=1, draft=adopted,
    ) == "cancelled"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (adopted_token,),
        ).fetchone() == ("cancelled", adopted["source_image_ref"])


def test_revision_live_claim_redelivery_waits_without_deleting_state_or_extra_provider(
    tmp_path, monkeypatch,
):
    _db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-live-claim.db"
    )
    replies = []
    calls = []
    event = _text("LIVE-CLAIM-TEXT", "白飯改半碗")
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-LIVE-CLAIM"
    ))

    def provider(*_args):
        calls.append("provider")
        server.processed_messages.discard("LIVE-CLAIM-TEXT")
        server.handle_message(event)
        assert server.get_daily_food_edit_state("U1")["input_type"] == "meal_photo_revision_processing"
        return _revised_payload()

    monkeypatch.setattr(server, "_estimate_adjusted_meal_photo", provider)
    server.handle_message(event)

    assert calls == ["provider"]
    assert any(getattr(reply, "text", "").startswith("⏳") for reply in replies)
    assert server.get_daily_food_edit_state("U1")["input_type"] == "meal_photo_revision_preview"


def test_revision_unknown_expired_claim_retries_once_then_fences_and_terminates(
    tmp_path, monkeypatch,
):
    db, _token, log_id = _setup_revision_handler(
        tmp_path, monkeypatch, "revision-handler-unknown-claim.db"
    )
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_args: None)
    server.handle_postback_event(_postback(
        f"mealrev:v1:{log_id}:1:start", event_id="START-UNKNOWN-CLAIM"
    ))
    original_claim = server._claim_meal_photo_revision_text(
        user_id="U1", log_id=log_id, expected_version=1,
        message_id="UNKNOWN-TEXT", correction="白飯改半碗",
    )
    assert original_claim

    def expire_lease():
        with sqlite3.connect(db) as conn:
            payload = json.loads(conn.execute(
                "SELECT payload_json FROM daily_food_edit_states WHERE user_id='U1'"
            ).fetchone()[0])
            payload["lease_until"] = (
                server.tw_now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE daily_food_edit_states SET payload_json=? WHERE user_id='U1'",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
            )
            conn.commit()

    expire_lease()
    retry = server._recover_meal_photo_revision_claim(
        user_id="U1", log_id=log_id, expected_version=1,
        message_id="UNKNOWN-TEXT", correction="白飯改半碗",
    )
    assert retry["status"] == "retry"
    assert retry["claim_token"] != original_claim
    assert not server._complete_meal_photo_revision_claim(
        user_id="U1", claim_token=original_claim, draft={},
        correction="白飯改半碗", message_id="UNKNOWN-TEXT",
    )

    expire_lease()
    terminal = server._recover_meal_photo_revision_claim(
        user_id="U1", log_id=log_id, expected_version=1,
        message_id="UNKNOWN-TEXT", correction="白飯改半碗",
    )
    assert terminal["status"] == "expired"
    assert server.get_daily_food_edit_state("U1") is None
