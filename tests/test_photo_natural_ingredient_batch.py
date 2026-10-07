import json
import sqlite3
from types import SimpleNamespace

import pytest
import server
from meal_photo_system import build_meal_photo_estimate_bubble, get_meal_photo_draft, save_meal_photo_draft
from tests.test_photo_ingredient_controls import _action, _payload, _postback, _text


def _setup(tmp_path, monkeypatch, items=None):
    db = tmp_path / "photo-natural-batch.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    image_dir = tmp_path / "nutrition_images"
    image_dir.mkdir(parents=True)
    image_name = "b" * 32 + ".jpg"
    (image_dir / image_name).write_bytes(b"\xff\xd8\xff" + b"0" * 200)
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-NATURAL-BATCH",
            payload=_payload(items), source_image_ref=f"nutrition-image:{image_name}",
            meal_slot="午餐", workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    server.processed_messages.clear()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "left"))
    return db, token, draft, replies


def _estimate(request):
    grams = float(request["amount"]) if request["unit"] == "g" else 30.0
    calories = grams * 0.5
    return {
        "food_name": request["food_name"],
        "portion_assumption": f"{grams:g}g" if request["unit"] == "g" else "1/6顆",
        "calories_kcal": {"estimate": calories, "min": max(0, calories - 2), "max": calories + 2},
        "protein_g": {"estimate": grams * 0.05, "min": 0, "max": grams * 0.1},
        "provenance": {"provider": "test", "model": "offline", "method": "text_meal_estimate"},
    }


def _enter_add(server_draft, replies):
    card = build_meal_photo_estimate_bubble(server_draft)
    server.handle_postback_event(_postback(_action(card, "➕ 新增食材")["data"], "REQUEST-NATURAL-BATCH"))
    assert "一次輸入多項" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)


def test_real_handler_accepts_four_natural_items_and_applies_one_atomic_preview(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(dict(req)) or _estimate(req))
    _enter_add(draft, replies)

    text = "木耳20g、牛番茄1/6顆、金針菇20g、青菜20g"
    server.handle_message(_text(text, "NATURAL-FOUR"))

    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert [call["food_name"] for call in calls] == ["木耳", "牛番茄", "金針菇", "青菜"]
    assert calls[0]["amount"] == 20 and calls[0]["unit"] == "g"
    assert calls[1]["amount"] == pytest.approx(1 / 6) and calls[1]["unit"] == "piece"
    for expected in ("木耳：20g", "牛番茄：1/6顆", "金針菇：20g", "青菜：20g"):
        assert expected in rendered
    assert "AI估算" in rendered
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "estimated"
        assert current["version"] == draft["version"] + 2  # request_add + one batch mutation
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 4

    # Same webhook text id is durable replay: no additional provider/quota child or mutation.
    server.processed_messages.clear()
    server.handle_message(_text(text, "NATURAL-FOUR"))
    assert len(calls) == 4
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'").fetchone()[0] == 1

    replayed_card = replies[-1].as_json_dict()["contents"]
    server.handle_postback_event(_postback(_action(replayed_card, "確認記錄")["data"], "CONFIRM-NATURAL-BATCH"))
    server.processed_messages.clear()
    server.handle_message(_text(text, "NATURAL-FOUR"))
    assert "已加入" in replies[-1].text and "已確認記錄" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1


def test_four_ai_additions_preserve_valid_aggregate_interval_when_photo_details_do_not_sum_to_total(
    tmp_path, monkeypatch,
):
    """Provider aggregates and rounded photo details are independent estimates."""
    rounded_details = [
        {"name": "米飯", "portion": "1碗", "calories_kcal": 80, "protein_g": 2},
        {"name": "豬肉", "portion": "1掌", "calories_kcal": 220, "protein_g": 22},
        {"name": "炒蛋", "portion": "1顆", "calories_kcal": 60, "protein_g": 6},
        {"name": "蔬菜", "portion": "半碗", "calories_kcal": 10, "protein_g": 1},
    ]
    db, token, draft, replies = _setup(tmp_path, monkeypatch, rounded_details)
    calls = []
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda request: calls.append(dict(request)) or _estimate(request),
    )
    _enter_add(draft, replies)
    waiting_version = draft["version"] + 1

    server.handle_message(
        _text("木耳約20g、牛番茄約1/6顆、金針菇約20g、青菜約20g", "FOUR-RANGE-REGRESSION")
    )

    assert [call["food_name"] for call in calls] == ["木耳", "牛番茄", "金針菇", "青菜"]
    assert "估算區間無法確定" not in getattr(replies[-1], "text", "")
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("estimated", waiting_version + 1)
        # Keep the original aggregate estimate/interval authoritative and add
        # each independently validated child estimate/interval.
        assert current["estimate"]["calories_kcal"] == pytest.approx(565)
        assert current["estimate"]["calories_kcal_range"] == {
            "min": pytest.approx(437), "max": pytest.approx(703),
            "basis": "ai_vision_estimate_range_v1",
        }
        assert current["estimate"]["protein_g"] == pytest.approx(35.5)
        assert current["estimate"]["protein_g_range"] == {
            "min": pytest.approx(22), "max": pytest.approx(49),
            "basis": "ai_vision_estimate_range_v1",
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_existing_unique_name_is_updated_not_duplicated_and_ambiguous_name_is_rejected(tmp_path, monkeypatch):
    # A non-zero replacement needs a trustworthy interval for the old item;
    # rounded vision points alone are not enough to invent update bounds.
    existing = [
        {
            "name": "青菜", "portion": "半碗", "calories_kcal": 20, "protein_g": 1,
            "calories_kcal_range": {"min": 18, "max": 22},
            "protein_g_range": {"min": 0, "max": 2},
        },
        {
            "name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 4,
            "calories_kcal_range": {"min": 180, "max": 220},
            "protein_g_range": {"min": 3, "max": 5},
        },
    ]
    db, token, draft, replies = _setup(tmp_path, monkeypatch, existing)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(dict(req)) or _estimate(req))
    _enter_add(draft, replies)
    server.handle_message(_text("青菜約20克、木耳20g", "UPDATE-AND-ADD"))
    prompt = replies[-1].as_json_dict()
    assert prompt["text"] == "這餐已有青菜，你想怎麼處理？"
    server.handle_postback_event(_postback(_action(prompt, "修改原份量")["data"], "CHOOSE-UPDATE-AND-ADD"))
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
    names = [item["name"] for item in current["estimate"]["estimate_items"]]
    assert names.count("青菜") == 1 and names.count("木耳") == 1
    assert next(item for item in current["estimate"]["estimate_items"] if item["name"] == "青菜")["portion"] == "約20克"
    assert current["estimate"]["calories_kcal"] == pytest.approx(520)
    assert current["estimate"]["calories_kcal_range"] == {
        "min": pytest.approx(394), "max": pytest.approx(656),
        "basis": "ai_vision_estimate_range_v1",
    }
    assert current["estimate"]["protein_g"] == pytest.approx(32)
    assert current["estimate"]["protein_g_range"] == {
        "min": pytest.approx(22), "max": pytest.approx(42),
        "basis": "ai_vision_estimate_range_v1",
    }

    duplicate = [
        {"name": "豆腐", "portion": "半盒", "calories_kcal": 100, "protein_g": 10},
        {"name": "豆腐", "portion": "一盒", "calories_kcal": 200, "protein_g": 20},
    ]
    db2, _token2, draft2, replies2 = _setup(tmp_path / "ambiguous", monkeypatch, duplicate)
    _enter_add(draft2, replies2)
    before_calls = len(calls)
    server.handle_message(_text("豆腐20g", "AMBIGUOUS-DUPLICATE"))
    assert "同名食材" in replies2[-1].text and "請先選擇" in replies2[-1].text
    assert len(calls) == before_calls
    with sqlite3.connect(db2) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=_token2)
        assert current["status"] == "awaiting_item_name"


def test_invalid_batch_and_provider_failure_never_partially_mutate_photo_draft(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    calls = []
    def provider(req):
        calls.append(dict(req))
        if req["food_name"] == "壞掉":
            raise RuntimeError("provider failed")
        return _estimate(req)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    _enter_add(draft, replies)
    waiting_version = draft["version"] + 1

    for i, invalid in enumerate(("", "請問青菜20g？", "不要青菜20g", "牛番茄1/0顆", "青菜-20g", "青菜NaNg")):
        server.handle_message(_text(invalid, f"INVALID-{i}"))
        assert "原草稿未變" in replies[-1].text
    assert calls == []
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "awaiting_item_name" and current["version"] == waiting_version

    server.handle_message(_text("木耳20g、壞掉20g、青菜20g", "PROVIDER-FAIL"))
    assert "估算失敗" in replies[-1].text and "原草稿未變" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "awaiting_item_name" and current["version"] == waiting_version
        assert [item["name"] for item in current["estimate"]["estimate_items"]] == ["米飯", "豬肉", "炒蛋", "蔬菜"]
        statuses = [row[0] for row in conn.execute("SELECT status FROM pending_text_meal_estimates ORDER BY created_at,token")]
        assert "failed" in statuses


def test_range_reconciliation_failure_keeps_draft_and_does_not_blame_valid_input(
    tmp_path, monkeypatch,
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", _estimate)
    _enter_add(draft, replies)
    with sqlite3.connect(db) as conn:
        before = get_meal_photo_draft(conn, user_id="U1", token=token)
    monkeypatch.setattr(
        server,
        "apply_meal_photo_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("食材更新後的估算區間無法確定，請重新估算")
        ),
    )

    server.handle_message(_text("木耳約20g", "RANGE-FAILURE-COPY"))

    assert replies[-1].text == (
        "⚠️ 食材更新後的估算區間無法確定。"
        "未更新餐點，原草稿已保留。可取消新增返回餐點。"
    )
    with sqlite3.connect(db) as conn:
        after = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (after["status"], after["version"], after["estimate"]) == (
            before["status"], before["version"], before["estimate"],
        )
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_original_pipe_formats_remain_compatible(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: _estimate(req))
    _enter_add(draft, replies)
    server.handle_message(_text("無糖豆漿｜1杯｜100｜9", "PIPE-FOUR"))
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "無糖豆漿：1杯（約100 kcal／蛋白質9 g）" in rendered
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "estimated"


def test_invalid_then_valid_retry_and_expired_or_quota_failure_have_explicit_state(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(dict(req)) or _estimate(req))
    _enter_add(draft, replies)
    server.handle_message(_text("只有名稱", "INVALID-THEN-VALID-1"))
    assert "原草稿未變" in replies[-1].text
    server.handle_message(_text("青菜20g", "INVALID-THEN-VALID-2"))
    assert "青菜：20g" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert len(calls) == 1

    db2, token2, draft2, replies2 = _setup(tmp_path / "expired", monkeypatch)
    _enter_add(draft2, replies2)
    with sqlite3.connect(db2) as conn:
        conn.execute("UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (token2,))
        conn.commit()
    before = len(calls)
    server.handle_message(_text("木耳20g", "EXPIRED-NATURAL"))
    assert "已逾時" in replies2[-1].text and "原草稿未變" in replies2[-1].text
    assert len(calls) == before
    with sqlite3.connect(db2) as conn:
        assert conn.execute("SELECT status FROM pending_meal_photo_drafts WHERE token=?", (token2,)).fetchone()[0] == "expired"

    db3, token3, draft3, replies3 = _setup(tmp_path / "quota", monkeypatch)
    _enter_add(draft3, replies3)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (False, "none"))
    server.handle_message(_text("木耳20g", "QUOTA-NATURAL"))
    assert "估算失敗" in replies3[-1].text and "原草稿未變" in replies3[-1].text
    with sqlite3.connect(db3) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token3)
        assert current["status"] == "awaiting_item_name"
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_second_provider_failure_refunds_whole_batch_and_corrected_retry_uses_original_draft(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """CREATE TABLE usage (
               user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
               remaining_meals INTEGER, last_date TEXT, status TEXT,
               expiry_date TEXT, daily_chat_limit INTEGER)"""
        )
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES ('U1',10,10,?,'active','',10)""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()

    fail_second = True
    calls = []

    def provider(req):
        calls.append(req["food_name"])
        if fail_second and req["food_name"] == "壞掉":
            raise RuntimeError("second child failed")
        return _estimate(req)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    _enter_add(draft, replies)
    waiting_version = draft["version"] + 1
    server.handle_message(_text("木耳20g、壞掉20g", "BATCH-PROVIDER-FAIL"))
    assert "估算失敗" in replies[-1].text and "原草稿未變" in replies[-1].text

    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("awaiting_item_name", waiting_version)
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 10
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status"
        ).fetchall() == [("refunded", 1)]
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0] == 0

    fail_second = False
    server.handle_message(_text("木耳20g、青菜20g", "BATCH-CORRECTED-RETRY"))
    assert "木耳：20g" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("estimated", waiting_version + 1)
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 9
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
        ).fetchall() == [("charged", 1), ("refunded", 1)]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_batch_event_insert_failure_rolls_back_parent_and_refunds_all_children(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """CREATE TABLE usage (
               user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
               remaining_meals INTEGER, last_date TEXT, status TEXT,
               expiry_date TEXT, daily_chat_limit INTEGER)"""
        )
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES ('U1',10,10,?,'active','',10)""",
            (server.tw_today().isoformat(),),
        )
        conn.execute(
            """CREATE TRIGGER reject_natural_batch_event
               BEFORE INSERT ON meal_photo_events
               WHEN NEW.action='batch_upsert_items'
               BEGIN SELECT RAISE(ABORT, 'reject batch event'); END"""
        )
        conn.commit()
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", _estimate)
    _enter_add(draft, replies)
    waiting_version = draft["version"] + 1

    server.handle_message(_text("木耳20g、青菜20g", "BATCH-ATOMIC-FAIL"))
    assert "估算失敗" in replies[-1].text and "原草稿未變" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("awaiting_item_name", waiting_version)
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 10
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status"
        ).fetchall() == [("refunded", 1)]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_reply_failure_replays_committed_batch_without_provider_or_quota(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    quota_calls = []
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda req: provider_calls.append(req["food_name"]) or _estimate(req),
    )
    monkeypatch.setattr(
        server, "check_permission_and_quota",
        lambda uid: quota_calls.append(uid) or (True, "left"),
    )
    replies = []
    card = build_meal_photo_estimate_bubble(draft)
    server.handle_postback_event(_postback(_action(card, "➕ 新增食材")["data"], "REQUEST-REPLY-FAIL"))

    def flaky_reply(_token, reply):
        replies.append(reply)
        if len(replies) == 1:
            raise RuntimeError("LINE reply failed")

    monkeypatch.setattr(server.line_bot_api, "reply_message", flaky_reply)
    event = _text("木耳20g、青菜20g", "BATCH-REPLY-FAIL")
    with pytest.raises(RuntimeError, match="LINE reply failed"):
        server.handle_message(event)
    assert "BATCH-REPLY-FAIL" not in server.processed_messages
    assert provider_calls == ["木耳", "青菜"]
    assert quota_calls == ["U1"]

    server.handle_message(event)
    assert len(replies) == 2
    assert provider_calls == ["木耳", "青菜"]
    assert quota_calls == ["U1"]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='batch_upsert_items'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_failed_child_retry_reuses_stable_key_and_refunds_only_failed_attempt(tmp_path, monkeypatch):
    db, _token, _draft, _replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """CREATE TABLE usage (
               user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
               remaining_meals INTEGER, last_date TEXT, status TEXT,
               expiry_date TEXT, daily_chat_limit INTEGER)"""
        )
        conn.execute(
            """INSERT INTO usage VALUES ('U1',10,10,?,'active','',10)""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()

    attempts = 0

    def provider(req):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient provider failure")
        return _estimate(req)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    request = {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
    with pytest.raises(RuntimeError, match="transient provider failure"):
        server.create_text_meal_estimate_draft(
            user_id="U1", message_id="STABLE-CHILD", request=request,
        )
    retried = server.create_text_meal_estimate_draft(
        user_id="U1", message_id="STABLE-CHILD", request=request,
    )
    assert retried["status"] == "pending"
    assert attempts == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_text_meal_estimates WHERE source_message_id='STABLE-CHILD'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 9
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
        ).fetchall() == [("charged", 1), ("refunded", 1)]
