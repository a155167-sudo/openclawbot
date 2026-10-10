import json
import sqlite3

import server
from meal_photo_system import (
    build_meal_photo_estimate_bubble,
    get_meal_photo_draft,
    save_meal_photo_draft,
)
from tests.test_photo_batch_single_quota import _install_usage
from tests.test_photo_ingredient_controls import _action, _payload, _postback, _text
from tests.test_photo_natural_ingredient_batch import _enter_add, _estimate, _setup


def _choice_action(message, label):
    data = message.as_json_dict()
    actions = [item["action"] for item in data["quickReply"]["items"]]
    return next(action for action in actions if action["label"] == label)


def _wood_items():
    return [
        {"name": "木耳", "portion": "10g", "calories_kcal": 10, "protein_g": 1,
         "calories_kcal_range": {"min": 8, "max": 12}, "protein_g_range": {"min": 0, "max": 2}},
        {"name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 4,
         "calories_kcal_range": {"min": 180, "max": 220}, "protein_g_range": {"min": 3, "max": 5}},
    ]


def test_registered_handler_prompts_then_extra_adds_distinct_item_once(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(dict(req)) or _estimate(req))
    _enter_add(draft, replies)

    server.handle_message(_text("木耳20g", "DUP-EXTRA"))

    prompt = replies[-1]
    assert prompt.type == "text"
    assert prompt.text == "這餐已有木耳，你想怎麼處理？"
    assert calls == []
    serialized = prompt.as_json_dict()
    labels = [x["action"]["label"] for x in serialized["quickReply"]["items"]]
    assert labels == ["修改原份量", "額外加一份", "取消新增"]
    extra = _choice_action(prompt, "額外加一份")
    assert len(extra["data"].encode()) <= 300
    assert "木耳" not in extra["data"]

    event = _postback(extra["data"], "DUP-EXTRA-CLICK")
    server.handle_postback_event(event)
    server.handle_postback_event(event)

    assert [c["food_name"] for c in calls] == ["木耳"]
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        wood = [x for x in current["estimate"]["estimate_items"] if x["name"] == "木耳"]
        assert len(wood) == 2
        assert {x["portion"] for x in wood} == {"10g", "20g"}
        assert len({x["item_id"] for x in wood}) == 2
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_modify_preserves_item_id_and_cancel_has_zero_provider_or_food_write(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(dict(req)) or _estimate(req))
    _enter_add(draft, replies)
    with sqlite3.connect(db) as conn:
        original_id = next(x["item_id"] for x in get_meal_photo_draft(conn, user_id="U1", token=token)["estimate"]["estimate_items"] if x["name"] == "木耳")
    server.handle_message(_text("木耳40g", "DUP-MODIFY"))
    server.handle_postback_event(_postback(_choice_action(replies[-1], "修改原份量")["data"], "MODIFY"))
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        wood = [x for x in current["estimate"]["estimate_items"] if x["name"] == "木耳"]
        assert len(wood) == 1 and wood[0]["item_id"] == original_id and wood[0]["portion"] == "40g"

    db2, token2, draft2, replies2 = _setup(tmp_path / "cancel", monkeypatch, _wood_items())
    before = len(calls)
    _enter_add(draft2, replies2)
    server.handle_message(_text("木耳20g、青菜20g", "DUP-CANCEL"))
    server.handle_postback_event(_postback(_choice_action(replies2[-1], "取消新增")["data"], "CANCEL-CHOICE"))
    assert len(calls) == before
    with sqlite3.connect(db2) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token2)
        assert current["status"] == "estimated"
        assert [x["name"] for x in current["estimate"]["estimate_items"]] == ["木耳", "米飯"]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM meal_photo_ingredient_choices").fetchone()[0] == "cancelled"


def test_two_collisions_wait_for_all_decisions_then_commit_mixed_batch_once(tmp_path, monkeypatch):
    items = _wood_items() + [{"name": "青菜", "portion": "10g", "calories_kcal": 5, "protein_g": .5,
                             "calories_kcal_range": {"min": 4, "max": 6}, "protein_g_range": {"min": 0, "max": 1}}]
    db, token, draft, replies = _setup(tmp_path, monkeypatch, items)
    calls, quota = [], []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(dict(req)) or _estimate(req))
    monkeypatch.setattr(server, "check_permission_and_quota", lambda uid: quota.append(uid) or (True, "left"))
    _enter_add(draft, replies)
    server.handle_message(_text("木耳20g、豆芽20g、青菜20g", "MULTI-COLLISION"))
    assert calls == [] and quota == []
    server.handle_postback_event(_postback(_choice_action(replies[-1], "額外加一份")["data"], "FIRST"))
    assert replies[-1].text == "這餐已有青菜，你想怎麼處理？"
    assert calls == [] and quota == []
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "awaiting_item_name"
    server.handle_postback_event(_postback(_choice_action(replies[-1], "修改原份量")["data"], "SECOND"))
    assert [x["food_name"] for x in calls] == ["木耳", "豆芽", "青菜"]
    assert quota == ["U1"]
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        names = [x["name"] for x in current["estimate"]["estimate_items"]]
        assert names.count("木耳") == 2 and names.count("青菜") == 1 and names.count("豆芽") == 1
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'").fetchone()[0] == 1


def test_missing_old_range_fails_before_provider_and_typed_cancel_discards_choice(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    with sqlite3.connect(db) as conn:
        payload = json.loads(conn.execute("SELECT observed_payload_json FROM pending_meal_photo_drafts WHERE token=?", (token,)).fetchone()[0])
        wood = next(x for x in payload["ai_estimate"]["items"] if x["name"] == "木耳")
        wood.pop("calories_kcal_range"); wood.pop("protein_g_range")
        conn.execute("UPDATE pending_meal_photo_drafts SET observed_payload_json=? WHERE token=?", (json.dumps(payload), token)); conn.commit()
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))
    _enter_add(draft, replies)
    server.handle_message(_text("木耳40g", "NO-RANGE-CHOICE"))
    server.handle_postback_event(_postback(_choice_action(replies[-1], "修改原份量")["data"], "NO-RANGE-MODIFY"))
    assert replies[-1].text == "這筆原辨識缺少個別營養區間，暫時無法安全修改份量；原草稿未變。"
    assert calls == []
    server.handle_message(_text("確認取消新增食材", "TYPED-CANCEL-CHOICE"))
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "estimated"
        assert conn.execute("SELECT status FROM meal_photo_ingredient_choices").fetchone()[0] == "cancelled"


def test_choice_wrong_owner_stale_and_expired_fail_closed_without_name_leak(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: (_ for _ in ()).throw(AssertionError("provider forbidden")))
    _enter_add(draft, replies)
    server.handle_message(_text("木耳20g", "BOUNDARY-CHOICE"))
    data = _choice_action(replies[-1], "額外加一份")["data"]
    server.handle_postback_event(_postback(data, "WRONG-OWNER", user_id="U2"))
    assert "木耳" not in replies[-1].text and "找不到" in replies[-1].text
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meal_photo_ingredient_choices SET expires_at='2000-01-01T00:00:00+08:00'"); conn.commit()
    server.handle_postback_event(_postback(data, "EXPIRED-CHOICE"))
    assert "已逾時" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "awaiting_item_name"
        assert [x["name"] for x in current["estimate"]["estimate_items"]] == ["木耳", "米飯"]


def test_cancel_during_provider_fences_late_result_and_keeps_original_meal(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    _enter_add(draft, replies)
    server.handle_message(_text("木耳20g", "LATE-CANCEL-BATCH"))
    extra_data = _choice_action(replies[-1], "額外加一份")["data"]
    cancelled = False

    def provider(req):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            with sqlite3.connect(db) as other:
                server.apply_meal_photo_action(
                    other, event_id="LATE-CANCEL", user_id="U1", token=token,
                    expected_version=draft["version"] + 1, action="cancel_add",
                )
        return _estimate(req)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    server.handle_postback_event(_postback(extra_data, "LATE-EXTRA-CLICK"))

    assert cancelled and "原草稿未變" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "estimated"
        assert [x["name"] for x in current["estimate"]["estimate_items"]] == ["木耳", "米飯"]
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_late_duplicate_choice_result_cannot_cross_newer_owner_draft_and_refunds_once(
    tmp_path, monkeypatch,
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    _install_usage(db, quota=1)
    monkeypatch.setattr(
        server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA,
    )
    _enter_add(draft, replies)
    server.handle_message(_text("木耳20g", "LATE-NEW-DRAFT"))
    extra_data = _choice_action(replies[-1], "額外加一份")["data"]
    created = {}

    def provider(request):
        if not created:
            with sqlite3.connect(db) as other:
                created["token"] = save_meal_photo_draft(
                    other,
                    user_id="U1",
                    source_message_id="NEWEST-PHOTO-DURING-PROVIDER",
                    payload=_payload(),
                    source_image_ref="nutrition-image:" + "b" * 32 + ".jpg",
                    meal_slot="晚餐",
                    workflow_version="user_confirmed_ai_nutrition_v2",
                )
                created["status"] = get_meal_photo_draft(
                    other, user_id="U1", token=created["token"],
                )["status"]
        return _estimate(request)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    with sqlite3.connect(db) as conn:
        before = get_meal_photo_draft(conn, user_id="U1", token=token)

    server.handle_postback_event(_postback(extra_data, "LATE-NEW-DRAFT-CLICK"))

    assert replies[-1].type == "text"
    assert "較舊餐點" in replies[-1].text or "最新" in replies[-1].text
    with sqlite3.connect(db) as conn:
        after = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (after["status"], after["version"], after["estimate"]) == (
            before["status"], before["version"], before["estimate"],
        )
        assert get_meal_photo_draft(
            conn, user_id="U1", token=created["token"],
        )["status"] == created["status"]
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE token=? AND action='batch_upsert_items'",
            (token,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM photo_ingredient_batch_quota_ops"
        ).fetchall() == [("failed",)]
        assert conn.execute(
            "SELECT status FROM text_meal_estimate_quota_ledger"
        ).fetchall() == [("refunded",)]
        assert conn.execute(
            "SELECT status FROM pending_text_meal_estimates"
        ).fetchall() == [("discarded_photo_stale",)]
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_latest_draft_commit_fence_is_scoped_to_the_same_owner(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch, _wood_items())
    _enter_add(draft, replies)
    server.handle_message(_text("木耳20g", "OWNER-SCOPED-LATEST"))
    extra_data = _choice_action(replies[-1], "額外加一份")["data"]
    created = False

    def provider(request):
        nonlocal created
        if not created:
            created = True
            with sqlite3.connect(db) as other:
                save_meal_photo_draft(
                    other,
                    user_id="U2",
                    source_message_id="OTHER-OWNER-NEWEST-PHOTO",
                    payload=_payload(),
                    source_image_ref="nutrition-image:" + "b" * 32 + ".jpg",
                    meal_slot="晚餐",
                    workflow_version="user_confirmed_ai_nutrition_v2",
                )
        return _estimate(request)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    server.handle_postback_event(_postback(extra_data, "OWNER-SCOPED-LATEST-CLICK"))

    assert created and replies[-1].type == "flex"
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "estimated"
        assert sum(
            item["name"] == "木耳" for item in current["estimate"]["estimate_items"]
        ) == 2
