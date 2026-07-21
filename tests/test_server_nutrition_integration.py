import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server
from nutrition_system import (
    confirm_pending_label,
    ensure_nutrition_schema,
    save_pending_label,
    set_nutrition_input_state,
)


class FakeWorksheet:
    def __init__(self, records):
        self._records = records

    def get_all_records(self):
        return self._records


def plan_row(**overrides):
    row = {
        "plan_id": "plan_default",
        "User_ID": "U1",
        "版本": 1,
        "生效日期": "2026-07-01",
        "結束日期": "",
        "星期": "每日",
        "餐別": "全日",
        "熱量目標": 2000,
        "蛋白質目標g": 100,
        "脂肪目標g": 60,
        "碳水目標g": 250,
        "狀態": "active",
    }
    row.update(overrides)
    return row


def test_image_magic_and_opaque_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    jpeg = b"\xff\xd8\xff" + b"x" * 100
    assert server._validate_image_bytes(jpeg) == ".jpg"
    ref = server._store_nutrition_image(jpeg, ".jpg")
    assert ref.startswith("nutrition-image:")
    assert "U" not in ref
    path = server._nutrition_image_path(ref)
    assert path is not None
    assert path.startswith(str(tmp_path))


def test_partial_reserved_image_is_atomically_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    path = server._nutrition_image_path(ref)
    assert path is not None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    image_bytes = b"\xff\xd8\xff" + b"x" * 100
    assert server._store_nutrition_image(image_bytes, ".jpg", ref) == ref
    with open(path, "rb") as image_file:
        assert image_file.read() == image_bytes
    assert not [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")]


def test_plan_exact_meal_precedes_newer_wildcard(monkeypatch):
    rows = [
        plan_row(plan_id="wild", 版本=99, 星期="每日", 餐別="全日", 熱量目標=2200),
        plan_row(plan_id="exact", 版本=1, 星期="週二", 餐別="晚餐", 熱量目標=500),
    ]
    monkeypatch.setattr(server, "_nutrition_ws", lambda title: FakeWorksheet(rows))
    plan = server.get_active_nutrition_target("U1", "晚餐", "2026-07-21T18:00:00+08:00")
    assert plan is not None
    assert plan["plan_id"] == "exact"
    assert plan["targets"]["calories_kcal"] == 500
    assert plan["consumption_meal_slot"] == "晚餐"


def test_all_day_plan_uses_all_day_consumption(monkeypatch):
    monkeypatch.setattr(server, "_nutrition_ws", lambda title: FakeWorksheet([plan_row()]))
    plan = server.get_active_nutrition_target("U1", "晚餐", "2026-07-21")
    assert plan is not None
    assert plan["meal_slot"] == "全日"
    assert plan["consumption_meal_slot"] == ""


def test_legacy_outbox_migration_deduplicates_and_adds_unique_index(tmp_path):
    db = tmp_path / "legacy-outbox.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE nutrition_sheet_outbox (
            outbox_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '', created_at TEXT NOT NULL, synced_at TEXT DEFAULT '')""")
        conn.execute("INSERT INTO nutrition_sheet_outbox VALUES ('o1','food','f1','synced',1,'','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO nutrition_sheet_outbox VALUES ('o2','food','f1','pending',2,'','2026-01-02','')")
        ensure_nutrition_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM nutrition_sheet_outbox WHERE entity_type='food' AND entity_id='f1'").fetchone()[0] == 1
        server._queue_nutrition_outbox(conn, "food", "f1")
        conn.commit()
        assert conn.execute("SELECT status FROM nutrition_sheet_outbox WHERE entity_type='food' AND entity_id='f1'").fetchone()[0] == "pending"
        indexes = conn.execute("PRAGMA index_list(nutrition_sheet_outbox)").fetchall()
        assert any(row[1] == "idx_nutrition_outbox_entity" and row[2] == 1 for row in indexes)


def test_outbox_keeps_partial_failure_pending(tmp_path, monkeypatch):
    db = tmp_path / "outbox.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute("""INSERT INTO nutrition_sheet_outbox
            (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
            VALUES ('o1','food','f1','pending',0,'','2026-01-01','')""")
        conn.execute("""INSERT INTO nutrition_sheet_outbox
            (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
            VALUES ('o2','food_log','l1','pending',0,'','2026-01-01','')""")
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "sh", object())
    monkeypatch.setattr(server, "_sync_food_outbox", lambda entity_id: None)

    def fail(_):
        raise RuntimeError("temporary sheet failure")

    monkeypatch.setattr(server, "_sync_food_log_outbox", fail)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT outbox_id, status FROM nutrition_sheet_outbox"))
        attempts = conn.execute("SELECT attempts FROM nutrition_sheet_outbox WHERE outbox_id='o2'").fetchone()[0]
    assert rows == {"o1": "synced", "o2": "pending"}
    assert attempts == 1


def test_legacy_dashboard_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute("""CREATE TABLE health_profile (
            user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
            today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL)""")
        conn.execute("INSERT INTO health_profile VALUES ('U1',0,0,'',?,2000,100)", (today,))
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,fingerprint,created_at,updated_at)
               VALUES ('f1','測試食品','fixture-f1',?,?)""",
            (today, today),
        )
        conn.execute("""INSERT INTO food_logs
            (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,consumed_amount,
             consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,source_image_ref,
             plan_id,confirmation_status,legacy_applied_at,created_at,updated_at)
            VALUES ('l1','U1','f1',?,'晚餐',1,1,'份','{}','{}','','','confirmed','',?,?)""",
            (today + "T18:00:00+08:00", today, today))
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "upsert_frequent_food", lambda *args: None)
    monkeypatch.setattr(server, "build_meal_log_flex", lambda *args: {"ok": True})
    result = {
        "food": {"product_name": "豆漿"},
        "log": {"log_id": "l1", "consumed_at": today + "T18:00:00+08:00", "nutrition": {"calories_kcal": 190, "protein_g": 19}},
    }
    server.apply_confirmed_nutrition_to_legacy_dashboard("U1", result)
    server.apply_confirmed_nutrition_to_legacy_dashboard("U1", result)
    with sqlite3.connect(db) as conn:
        values = conn.execute("SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'").fetchone()
    assert values == (190.0, 19.0)


def test_health_check_uses_configured_sqlite_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "volume"
    db_path = data_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(data_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    assert server.health_check() == {"status": "ok", "service": "openclawbot"}
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"usage", "health_profile", "food_catalog", "food_logs", "nutrition_sheet_outbox"} <= tables


def test_health_check_rejects_empty_database(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "empty.db"))
    with pytest.raises(server.HTTPException) as exc_info:
        server.health_check()
    assert exc_info.value.status_code == 503


def valid_label():
    return {
        "status": "success", "image_type": "nutrition_label", "product_name": "測試豆漿",
        "brand": "測試", "barcode": "123", "package_amount": 375, "package_unit": "ml",
        "servings_per_package": 1,
        "per_serving": {"calories_kcal": 190, "protein_g": 19, "fat_g": 9, "carbohydrate_g": 8},
        "per_100": {"calories_kcal": 50.67, "protein_g": 5.07, "fat_g": 2.4, "carbohydrate_g": 2.13},
        "confidence": 0.99,
    }


def test_failed_image_delete_keeps_reference_for_retry(tmp_path, monkeypatch):
    db = tmp_path / "cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U1", payload=valid_label(), source_image_ref=ref)
        conn.execute("UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (token,))
        conn.commit()
    original_remove = server.os.remove

    def fail_remove(_):
        raise OSError("busy")

    monkeypatch.setattr(server.os, "remove", fail_remove)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        retained = conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0]
    assert retained == ref
    monkeypatch.setattr(server.os, "remove", original_remove)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        cleared = conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0]
    assert cleared == ""


def test_awaiting_identity_expiration_deletes_stored_back_image(tmp_path, monkeypatch):
    db = tmp_path / "awaiting-cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U1", payload=partial, source_image_ref=ref,
            allow_missing_identity=True,
        )
        conn.execute(
            "UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        status, stored_ref = conn.execute(
            "SELECT status,source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()
    assert status == "expired"
    assert stored_ref == ""
    image_path = server._nutrition_image_path(ref)
    assert image_path is not None
    assert not os.path.exists(image_path)


def test_expired_draft_payload_is_scrubbed_and_tombstone_is_purged(tmp_path, monkeypatch):
    db = tmp_path / "retention.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U_RETENTION", payload=partial,
            source_message_id="BACK_RETENTION", allow_missing_identity=True,
        )
        conn.execute(
            "UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.execute(
            """INSERT INTO nutrition_message_events
               (message_id,user_id,event_type,token,created_at)
               VALUES ('EVENT_OLD','U_RETENTION','test',?,'2000-01-01T00:00:00+08:00')""",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        status, payload, retired_at = conn.execute(
            "SELECT status,label_payload_json,retired_at FROM pending_nutrition_logs WHERE token=?",
            (token,),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE token=?", (token,)
        ).fetchone()[0]
    assert status == "expired"
    assert payload == "{}"
    assert retired_at
    assert event_count == 0
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_nutrition_logs SET retired_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0] == 0


def test_old_tombstone_keeps_parent_until_young_event_reaches_retention(tmp_path, monkeypatch):
    db = tmp_path / "retention-young-event.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn,
            user_id="U_YOUNG_EVENT",
            payload=partial,
            source_message_id="BACK_YOUNG_EVENT",
            allow_missing_identity=True,
        )
        conn.execute(
            """UPDATE pending_nutrition_logs
               SET status='expired',label_payload_json='{}',source_image_ref='',
                   retired_at='2000-01-01T00:00:00+08:00'
               WHERE token=?""",
            (token,),
        )
        conn.execute(
            """INSERT INTO nutrition_message_events
               (message_id,user_id,event_type,token,created_at)
               VALUES ('EVENT_YOUNG','U_YOUNG_EVENT','test',?,?)""",
            (token, server.tw_now().isoformat(timespec="seconds")),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE token=?", (token,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0] == 1
        conn.execute(
            "UPDATE nutrition_message_events SET created_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE token=?", (token,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0] == 0


def test_nutrition_cleanup_is_registered_hourly():
    calls = []

    class FakeScheduler:
        def add_job(self, func, trigger, **kwargs):
            calls.append((func, trigger, kwargs))

    server.register_nutrition_cleanup_job(FakeScheduler())
    matching = [item for item in calls if item[0] is server.cleanup_nutrition_images]
    assert matching == [(
        server.cleanup_nutrition_images,
        "interval",
        {"hours": 1, "max_instances": 1, "coalesce": True},
    )]


def test_confirmed_image_cleanup_requeues_sheet_updates(tmp_path, monkeypatch):
    db = tmp_path / "confirmed-cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U1", payload=valid_label(), source_image_ref=ref)
        result = confirm_pending_label(conn, token=token, user_id="U1", plan_link_status="no_plan")
        conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?", (result["log"]["log_id"],))
        conn.execute("UPDATE nutrition_sheet_outbox SET status='synced'")
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        log_ref = conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0]
        food_ref = conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0]
        states = dict(conn.execute("SELECT entity_type,status FROM nutrition_sheet_outbox"))
    assert log_ref == food_ref == ""
    assert states == {"food": "pending", "food_log": "pending"}


def test_plan_link_failure_is_retried(tmp_path, monkeypatch):
    db = tmp_path / "plan-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U1", payload=valid_label(), meal_slot="晚餐", consumed_at="2026-07-21T18:00:00+08:00")
        result = confirm_pending_label(conn, token=token, user_id="U1", plan_link_status="pending")
    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *args: (_ for _ in ()).throw(RuntimeError("sheet down")))
    assert server.retry_pending_nutrition_plan_links() == 0
    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *args: {"plan_id": "plan_recovered"})
    assert server.retry_pending_nutrition_plan_links() == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT plan_id,plan_link_status FROM food_logs WHERE log_id=?", (result["log"]["log_id"],)).fetchone()
        outbox = conn.execute("SELECT status FROM nutrition_sheet_outbox WHERE entity_type='food_log'").fetchone()[0]
    assert row == ("plan_recovered", "linked")
    assert outbox == "pending"


def test_transient_image_failure_allows_webhook_redelivery(monkeypatch):
    message_id = "retry-image-1"
    event = SimpleNamespace(
        message=SimpleNamespace(id=message_id),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply",
    )
    server.processed_messages.discard(message_id)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(server.line_bot_api, "get_message_content", lambda _: (_ for _ in ()).throw(RuntimeError("temporary")))
    with pytest.raises(RuntimeError):
        server.handle_image_message(event)
    assert message_id not in server.processed_messages


def test_text_failure_discards_only_failed_event_id(monkeypatch):
    completed_id = "completed-event"
    failed_id = "failed-event"
    server.processed_messages.update({completed_id, failed_id})
    event = SimpleNamespace(message=SimpleNamespace(id=failed_id))
    monkeypatch.setattr(server, "_handle_message_impl", lambda _: (_ for _ in ()).throw(RuntimeError("temporary")))
    with pytest.raises(RuntimeError):
        server.handle_message(event)
    assert completed_id in server.processed_messages
    assert failed_id not in server.processed_messages
    server.processed_messages.discard(completed_id)


def test_outbox_update_during_processing_is_resynced(tmp_path, monkeypatch):
    db = tmp_path / "outbox-resync.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "sh", object())
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        server._queue_nutrition_outbox(conn, "food", "f1")
        conn.commit()
    calls = []

    def sync_and_dirty(entity_id):
        calls.append(entity_id)
        if len(calls) == 1:
            with sqlite3.connect(db) as conn:
                server._queue_nutrition_outbox(conn, "food", entity_id)
                conn.commit()

    monkeypatch.setattr(server, "_sync_food_outbox", sync_and_dirty)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        first_state = conn.execute("SELECT status,resync_required FROM nutrition_sheet_outbox").fetchone()
    assert first_state == ("pending", 0)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        final_state = conn.execute("SELECT status,resync_required FROM nutrition_sheet_outbox").fetchone()
    assert final_state == ("synced", 0)
    assert calls == ["f1", "f1"]


def test_server_stages_missing_identity_and_pairs_product_front():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    partial = {**valid_label(), "product_name": "", "brand": ""}
    staged = server.stage_nutrition_label(
        conn, user_id="U1", parsed=partial, source_message_id="BACK1",
        meal_slot="晚餐", consumed_at="2026-07-21T20:42:00+08:00",
    )
    assert staged["needs_identity"] is True
    assert staged["label"]["per_serving"]["calories_kcal"] == 190
    paired = server.pair_product_front(
        conn, user_id="U1", source_message_id="FRONT1",
        parsed={"status": "success", "image_type": "product_front", "product_name": "無加糖高蛋白豆漿", "brand": "測試品牌", "barcode": "123", "confidence": 0.98},
    )
    assert paired["token"] == staged["token"]
    assert paired["label"]["product_name"] == "無加糖高蛋白豆漿"
    assert conn.execute("SELECT status FROM pending_nutrition_logs WHERE token=?", (staged["token"],)).fetchone()[0] == "pending"


def test_nutrition_vision_prompt_supports_two_photo_flow():
    prompt = server.build_nutrition_vision_prompt()
    assert "product_front" in prompt
    assert "缺少品名仍回傳 status=success" in prompt
    assert "不可因缺少品名丟棄已讀到的營養資料" in prompt


def test_parse_manual_nutrition_correction_command():
    assert server.parse_nutrition_correction_command("修正營養 鈉 48") == ("sodium_mg", 48.0)
    assert server.parse_nutrition_correction_command("修正營養 熱量 228") == ("calories_kcal", 228.0)
    assert server.parse_nutrition_correction_command("修正營養 蛋白質 21.2") == ("protein_g", 21.2)
    assert server.parse_nutrition_correction_command("修正營養 咖啡因 100") is None
    assert server.parse_nutrition_correction_command("商品名稱 高蛋白豆漿") is None


def _text_event(message_id, text, user_id="U_EDIT"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


def test_text_handler_completes_missing_product_name(tmp_path, monkeypatch):
    db = tmp_path / "edit-name.db"
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U_EDIT", payload=partial, allow_missing_identity=True
        )
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(_text_event("edit-name-button", f"修改營養品名:{token}"))
    server._handle_message_impl(_text_event("edit-name-value", "商品名稱 無加糖高蛋白豆漿"))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status,label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
    assert row[0] == "pending"
    assert "無加糖高蛋白豆漿" in row[1]
    assert state_count == 0
    assert len(replies) == 2


def test_text_handler_corrects_nutrient_and_recalculates_per100(tmp_path, monkeypatch):
    db = tmp_path / "edit-nutrient.db"
    payload = {
        **valid_label(), "package_amount": 400,
        "per_serving": {**valid_label()["per_serving"], "sodium_mg": 488},
        "per_100": {**valid_label()["per_100"], "sodium_mg": 122},
    }
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_EDIT", payload=payload)
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(_text_event("edit-nutrient-button", f"修改營養數字:{token}"))
    edit_event = _text_event("edit-nutrient-value", "鈉 48")
    server._handle_message_impl(edit_event)
    server.processed_messages.discard("edit-nutrient-value")
    server._handle_message_impl(edit_event)
    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0]
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE message_id='edit-nutrient-value'"
        ).fetchone()[0]
    label = __import__("json").loads(stored)
    assert label["per_serving"]["sodium_mg"] == 48
    assert label["per_100"]["sodium_mg"] == 12
    assert state_count == 0
    assert event_count == 1
    assert len(replies) == 3


def test_committed_text_edit_reply_failure_is_retriable(tmp_path, monkeypatch):
    db = tmp_path / "edit-reply-failure.db"
    payload = {
        **valid_label(), "package_amount": 400,
        "per_serving": {**valid_label()["per_serving"], "sodium_mg": 488},
        "per_100": {**valid_label()["per_100"], "sodium_mg": 122},
    }
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_REPLY", payload=payload)
        set_nutrition_input_state(conn, user_id="U_REPLY", token=token, input_type="nutrient")
    event = _text_event("edit-reply-failure", "鈉 48", user_id="U_REPLY")
    monkeypatch.setattr(server, "DB_PATH", str(db))

    def fail_reply(*_):
        raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(reply_message=fail_reply))
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_message_impl(event)
    assert "edit-reply-failure" not in server.processed_messages
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE message_id='edit-reply-failure'"
        ).fetchone()[0] == 1
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(event)
    assert len(replies) == 1


def test_committed_confirmation_reply_failure_is_retriable(tmp_path, monkeypatch):
    db = tmp_path / "confirm-reply-failure.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CONFIRM_REPLY", payload=valid_label())
    event = _text_event(
        "confirm-reply-failure",
        f"確認營養紀錄:{token}",
        user_id="U_CONFIRM_REPLY",
    )
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *_: None)
    monkeypatch.setattr(server, "sync_confirmed_nutrition_to_sheet", lambda *_: None)
    monkeypatch.setattr(server, "apply_confirmed_nutrition_to_legacy_dashboard", lambda *_: None)

    def fail_reply(*_):
        raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(reply_message=fail_reply))
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_message_impl(event)
    assert "confirm-reply-failure" not in server.processed_messages
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(event)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert len(replies) == 1
