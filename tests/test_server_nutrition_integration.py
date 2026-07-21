import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server
from nutrition_system import confirm_pending_label, ensure_nutrition_schema, save_pending_label


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
