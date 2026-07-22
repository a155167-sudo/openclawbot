import os
import sqlite3
from datetime import datetime
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
    update_pending_consumption,
)
from daily_health_report import ensure_daily_health_schema, get_daily_health_checkin


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


def test_daily_target_prefers_all_day_row_over_meal_rows(monkeypatch):
    rows = [
        plan_row(plan_id="daily", 餐別="全日", 星期="週二", 主食份=6, 低脂蛋白份=13),
        plan_row(plan_id="breakfast", 餐別="早餐", 星期="週二", 主食份=3, 低脂蛋白份=4),
    ]
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: FakeWorksheet(rows))

    target = server.get_daily_nutrition_target("U1", "2026-07-21")

    assert target["starch_exchange"] == 6
    assert target["protein_low_exchange"] == 13


def test_daily_target_sums_latest_row_for_each_meal_when_no_all_day(monkeypatch):
    rows = [
        plan_row(plan_id="breakfast-old", 版本=1, 餐別="早餐", 星期="週二", 主食份=1, 低脂蛋白份=2),
        plan_row(plan_id="breakfast-new", 版本=2, 餐別="早餐", 星期="週二", 主食份=2, 低脂蛋白份=3),
        plan_row(plan_id="dinner", 版本=1, 餐別="晚餐", 星期="週二", 主食份=4, 低脂蛋白份=5),
    ]
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: FakeWorksheet(rows))

    target = server.get_daily_nutrition_target("U1", "2026-07-21")

    assert target["starch_exchange"] == 6
    assert target["protein_low_exchange"] == 8


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


def test_stale_outbox_lease_preserves_dirty_resync_signal(tmp_path, monkeypatch):
    db = tmp_path / "stale-outbox.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            """INSERT INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,claimed_at,
                lease_owner,resync_required,created_at,synced_at)
               VALUES ('stale','food','f1','processing',0,'','2000-01-01T00:00:00+08:00',
                       'dead-worker',1,'2000-01-01T00:00:00+08:00','')"""
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "sh", object())
    monkeypatch.setattr(server, "_sync_food_outbox", lambda _: None)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            "SELECT status FROM nutrition_sheet_outbox WHERE outbox_id='stale'"
        ).fetchone()[0]
    assert status == "pending"


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
    flex_calls = []
    monkeypatch.setattr(
        server, "build_meal_log_flex",
        lambda *args, **kwargs: flex_calls.append((args, kwargs)) or {"ok": True},
    )
    result = {
        "food": {"product_name": "豆漿"},
        "log": {
            "log_id": "l1", "consumed_at": today + "T18:00:00+08:00",
            "nutrition": {"calories_kcal": 190, "protein_g": 19},
            "exchange": {"protein_low_exchange": 2.71, "starch_exchange": 0.53},
        },
    }
    server.apply_confirmed_nutrition_to_legacy_dashboard("U1", result)
    server.apply_confirmed_nutrition_to_legacy_dashboard("U1", result)
    with sqlite3.connect(db) as conn:
        values = conn.execute("SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'").fetchone()
    assert values == (190.0, 19.0)
    assert flex_calls[0][1]["exchange_text"] == "低脂蛋白 2.71份｜主食 0.53份"


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



def test_meal_log_flex_can_show_pending_exchange_suggestion():
    flex = server.build_meal_log_flex(
        "測試豆漿", 190, 19, 190, 2000, 19, 100,
        exchange_text="低脂蛋白 2.71份｜主食 0.53份",
    )
    payload = str(flex.as_json_dict())
    assert "推算營養份數" in payload
    assert "低脂蛋白 2.71份" in payload
    assert "尚未扣入個人計畫" in payload


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


def test_photo_time_source_and_servings_survive_product_front_replay():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    partial = {
        **valid_label(),
        "product_name": "",
        "brand": "",
        "observed_at": "2026-07-21T18:00:00+08:00",
        "observed_at_confidence": 0.99,
    }
    staged = server.stage_nutrition_label(
        conn,
        user_id="U1",
        parsed=partial,
        source_message_id="BACK_REPLAY",
        meal_slot="晚餐",
        consumed_at="2026-07-21T18:00:00+08:00",
        consumed_time_source="photo_timestamp",
    )
    front = {
        "status": "success", "image_type": "product_front",
        "product_name": "高蛋白豆漿", "brand": "測試", "barcode": "123", "confidence": 0.95,
    }
    server.pair_product_front(
        conn, user_id="U1", parsed=front, source_message_id="FRONT_REPLAY"
    )
    update_pending_consumption(
        conn,
        user_id="U1",
        token=staged["token"],
        consumed_servings=2,
        consumed_at="2026-07-21T18:00:00+08:00",
        meal_slot="晚餐",
        consumed_time_source="manual",
    )
    replayed = server.pair_product_front(
        conn, user_id="U1", parsed=front, source_message_id="FRONT_REPLAY"
    )
    assert replayed["consumed_servings"] == 2
    assert replayed["consumed_at"] == "2026-07-21T18:00:00+08:00"
    assert replayed["consumed_time_source"] == "manual"


def test_nutrition_vision_prompt_supports_two_photo_flow():
    prompt = server.build_nutrition_vision_prompt()
    assert "product_front" in prompt
    assert "缺少品名仍回傳 status=success" in prompt
    assert "不可因缺少品名丟棄已讀到的營養資料" in prompt


def test_photo_timestamp_becomes_consumed_time_when_confident_and_reasonable():
    received = datetime(2026, 7, 22, 15, 0, tzinfo=server.TW_TZ)
    consumed, source = server.resolve_nutrition_consumed_at(
        {
            "observed_at": "2026-07-21T20:42:00+08:00",
            "observed_at_confidence": 0.98,
        },
        received_at=received,
    )
    assert consumed.isoformat() == "2026-07-21T20:42:00+08:00"
    assert source == "photo_timestamp"
    assert server.current_meal_slot(consumed) == "晚餐"
    prompt = server.build_nutrition_vision_prompt()
    assert "observed_at_confidence" in prompt
    assert "Asia/Taipei" in prompt


@pytest.mark.parametrize(
    "observed_at,confidence",
    [
        ("2026-07-21T20:42:00", 0.99),
        ("2026-07-21T20:42:00+08:00", 0.84),
        ("2026-07-22T15:11:00+08:00", 0.99),
        ("2026-06-21T14:59:59+08:00", 0.99),
        ("不是時間", 0.99),
        ("2026-07-21T20:42:00+08:00", float("nan")),
        ("2026-07-21T20:42:00+08:00", float("inf")),
    ],
)
def test_photo_timestamp_falls_back_for_low_confidence_or_implausible_values(
    observed_at, confidence
):
    received = datetime(2026, 7, 22, 15, 0, tzinfo=server.TW_TZ)
    consumed, source = server.resolve_nutrition_consumed_at(
        {"observed_at": observed_at, "observed_at_confidence": confidence},
        received_at=received,
    )
    assert consumed == received
    assert source == "line_timestamp"


def test_photo_timestamp_exact_reasonableness_boundaries_are_accepted():
    received = datetime(2026, 7, 22, 15, 0, tzinfo=server.TW_TZ)
    for value in (
        "2026-06-22T15:00:00+08:00",
        "2026-07-22T15:10:00+08:00",
        "2026-07-21T12:42:00Z",
    ):
        consumed, source = server.resolve_nutrition_consumed_at(
            {"observed_at": value, "observed_at_confidence": 0.99},
            received_at=received,
        )
        assert source == "photo_timestamp"
        assert consumed.tzinfo == server.TW_TZ


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
    reply_text = replies[0].text
    assert "推算營養份數" in reply_text
    assert "低脂蛋白 2.71份" in reply_text
    assert "主食 0.53份" in reply_text
    assert "尚未扣入個人計畫" in reply_text


def test_exchange_review_admin_commands_list_approve_and_replay(tmp_path, monkeypatch):
    db = tmp_path / "exchange-admin.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    food_id = confirmed["food"]["food_id"]
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "flush_nutrition_sheet_outbox", lambda: 2)

    pending_text = server.handle_exchange_review_admin_command(
        "#待審營養份量", server.ADMIN_UID
    )
    assert "測試豆漿" in pending_text
    assert "低脂蛋白 2.71份｜主食 0.53份" in pending_text
    assert f"#核准營養份量 {food_id}" in pending_text

    with pytest.raises(PermissionError):
        server.handle_exchange_review_admin_command(
            f"#核准營養份量 {food_id}", "U_NOT_ADMIN"
        )

    approved_text = server.handle_exchange_review_admin_command(
        f"#核准營養份量 {food_id}", server.ADMIN_UID
    )
    replay_text = server.handle_exchange_review_admin_command(
        f"#核准營養份量 {food_id}", server.ADMIN_UID
    )
    assert "核准完成" in approved_text
    assert "更新 1 筆飲食紀錄" in approved_text
    assert "已經核准" in replay_text
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT exchange_review_status FROM food_catalog WHERE food_id=?", (food_id,)
        ).fetchone()[0] == "approved"


def test_exchange_review_commands_are_admin_only_and_intercepted_by_line_handler(tmp_path, monkeypatch):
    assert server.is_admin_only_command("#待審營養份量")
    assert server.is_admin_only_command("#核准營養份量 food_1234567890abcdef")
    db = tmp_path / "exchange-admin-handler.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    event = _text_event("exchange-admin-list", "#待審營養份量", user_id=server.ADMIN_UID)
    server.processed_messages.discard("exchange-admin-list")
    server._handle_message_impl(event)
    assert len(replies) == 1
    assert "待審營養份量" in replies[0].text
    assert "#核准營養份量 food_" in replies[0].text
    replies.clear()
    denied = _text_event(
        "exchange-admin-denied",
        f"#核准營養份量 {confirmed['food']['food_id']}",
        user_id="U_NOT_ADMIN",
    )
    server.processed_messages.discard("exchange-admin-denied")
    server._handle_message_impl(denied)
    assert len(replies) == 1
    assert "管理員專用" in replies[0].text
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT exchange_review_status FROM food_catalog WHERE food_id=?",
            (confirmed["food"]["food_id"],),
        ).fetchone()[0] == "pending_review"


def test_exchange_approval_reply_failure_retries_idempotently(tmp_path, monkeypatch):
    db = tmp_path / "exchange-admin-retry.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    food_id = confirmed["food"]["food_id"]
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "flush_nutrition_sheet_outbox", lambda: 2)
    replies = []
    attempts = {"count": 0}

    def flaky_reply(_, message):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("LINE timeout")
        replies.append(message)

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(reply_message=flaky_reply))
    event = _text_event(
        "exchange-admin-retry",
        f"#核准營養份量 {food_id}",
        user_id=server.ADMIN_UID,
    )
    server.processed_messages.discard("exchange-admin-retry")
    with pytest.raises(RuntimeError, match="LINE timeout"):
        server._handle_message_impl(event)
    assert "exchange-admin-retry" not in server.processed_messages
    server._handle_message_impl(event)
    assert len(replies) == 1
    assert "已經核准" in replies[0].text
    with sqlite3.connect(db) as conn:
        suggestion_json, applied_json = conn.execute(
            """SELECT exchange_snapshot_json,approved_exchange_json
               FROM food_logs WHERE food_id=?""", (food_id,)
        ).fetchone()
    assert __import__("json").loads(suggestion_json)["_review_status"] == "pending_review"
    assert __import__("json").loads(applied_json)["_review_status"] == "approved"


def test_exchange_admin_error_reply_failure_allows_webhook_retry(monkeypatch):
    event = _text_event(
        "exchange-admin-error-retry",
        "#核准營養份量 food_missing",
        user_id=server.ADMIN_UID,
    )
    server.processed_messages.discard("exchange-admin-error-retry")
    monkeypatch.setattr(
        server,
        "handle_exchange_review_admin_command",
        lambda *_: (_ for _ in ()).throw(ValueError("找不到待審核食品")),
    )
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda *_: (_ for _ in ()).throw(RuntimeError("LINE timeout"))),
    )
    with pytest.raises(RuntimeError, match="LINE timeout"):
        server._handle_message_impl(event)
    assert "exchange-admin-error-retry" not in server.processed_messages


def test_meal_log_flex_distinguishes_pending_and_approved_exchange_status():
    args = ("測試食品", 100, 10, 100, 2000, 10, 100)
    pending = server.build_meal_log_flex(
        *args, exchange_text="主食 1份", exchange_review_status="pending_review"
    )
    approved = server.build_meal_log_flex(
        *args, exchange_text="主食 1份", exchange_review_status="approved"
    )
    pending_payload = __import__("json").dumps(pending.as_json_dict(), ensure_ascii=False)
    approved_payload = __import__("json").dumps(approved.as_json_dict(), ensure_ascii=False)
    assert "待營養師審核，尚未扣入個人計畫" in pending_payload
    assert "正式營養份數：主食 1份" in approved_payload
    assert "已納入個人計畫" in approved_payload


def test_food_log_sheet_exports_exchange_only_after_approval(tmp_path, monkeypatch):
    db = tmp_path / "exchange-sheet.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    captured = []
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: object())
    monkeypatch.setattr(
        server, "_upsert_raw_sheet_row",
        lambda _ws, entity_id, values: captured.append((entity_id, values)),
    )

    server._sync_food_outbox(confirmed["food"]["food_id"])
    pending_food_row = captured[-1][1]
    assert pending_food_row[17:25] == [0] * 8
    server._sync_food_log_outbox(confirmed["log"]["log_id"])
    pending_row = captured[-1][1]
    assert pending_row[16:24] == [0] * 8

    with sqlite3.connect(db) as conn:
        server.approve_food_exchange_suggestion(
            conn, food_id=confirmed["food"]["food_id"], reviewer="ADMIN"
        )
    server._sync_food_outbox(confirmed["food"]["food_id"])
    approved_food_row = captured[-1][1]
    assert approved_food_row[18] == 2.71
    assert approved_food_row[21] == 0.53
    server._sync_food_log_outbox(confirmed["log"]["log_id"])
    approved_row = captured[-1][1]
    assert approved_row[17] == 2.71
    assert approved_row[20] == 0.53
    with sqlite3.connect(db) as conn:
        applied = __import__("json").loads(conn.execute(
            "SELECT approved_exchange_json FROM food_logs WHERE log_id=?",
            (confirmed["log"]["log_id"],),
        ).fetchone()[0])
        applied["protein_low_exchange"] = 99
        conn.execute(
            "UPDATE food_logs SET approved_exchange_json=? WHERE log_id=?",
            (__import__("json").dumps(applied), confirmed["log"]["log_id"]),
        )
        conn.commit()
    server._sync_food_log_outbox(confirmed["log"]["log_id"])
    assert captured[-1][1][16:24] == [0] * 8


def test_jason_health_checkin_is_admin_scoped_and_saved_for_taipei_date(tmp_path, monkeypatch):
    db = tmp_path / "health-checkin.db"
    with sqlite3.connect(db) as conn:
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_OTHER")
    now = datetime(2026, 7, 22, 22, 31, tzinfo=server.TW_TZ)
    text = "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好"

    confirmation = server.save_jason_health_checkin("U_JASON", text, now=now)

    assert "2026/07/22" in confirmation
    with sqlite3.connect(db) as conn:
        saved = get_daily_health_checkin(
            conn, user_id="U_JASON", report_date="2026-07-22"
        )
    assert saved["water_ml"] == 2500
    with pytest.raises(PermissionError):
        server.save_jason_health_checkin("U_OTHER", text, now=now)


def test_intervals_global_fallback_is_denied_for_non_jason_uid(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "jason-athlete")
    monkeypatch.setenv("INTERVALS_API_KEY", "secret-for-test")

    assert server._jason_intervals_credentials("U_OTHER", "2026-07-22") is None


def test_build_jason_daily_health_report_uses_db_plan_and_intervals(tmp_path, monkeypatch):
    db = tmp_path / "daily-report.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(
        server, "fetch_daily_intervals_summary",
        lambda _uid, _date: {"items": [], "total_calories": 0, "total_duration_min": 0, "hr_load": 0},
    )
    monkeypatch.setattr(
        server, "get_daily_nutrition_target",
        lambda _uid, _date: {"starch_exchange": 6, "protein_low_exchange": 13},
    )

    report = server.build_jason_daily_health_report("U_JASON", "2026-07-22")

    assert "2026/07/22 一日健康日報" in report
    assert "今日無已確認飲食紀錄" in report
    assert "主食：0／6｜尚缺6" in report
    assert "蛋白質食物：0／13｜尚缺13" in report
    assert "今日運動：無活動紀錄" in report
    assert "休息日" not in report
    with pytest.raises(PermissionError):
        server.build_jason_daily_health_report("U_OTHER", "2026-07-22")


def test_scheduled_daily_report_push_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "scheduled-report.db"
    with sqlite3.connect(db) as conn:
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_OTHER")
    monkeypatch.setattr(server, "build_jason_daily_health_report", lambda _uid, _date: "REPORT")

    class FakeLineApi:
        def __init__(self):
            self.sent = []

        def push_message(self, uid, message, timeout=None):
            self.sent.append((uid, message.text, timeout))

    fake = FakeLineApi()
    monkeypatch.setattr(server, "line_bot_api", fake)
    now = datetime(2026, 7, 22, 23, 30, tzinfo=server.TW_TZ)

    assert server.send_jason_daily_health_report(now=now) is True
    assert server.send_jason_daily_health_report(now=now) is False
    assert fake.sent == [("U_JASON", "REPORT", 12)]


def test_register_daily_health_jobs_uses_retry_minutes_and_single_instance():
    jobs = []

    class FakeScheduler:
        def add_job(self, function, trigger, **kwargs):
            jobs.append((function, trigger, kwargs))

    server.register_daily_health_jobs(FakeScheduler())

    assert jobs == [
        (
            server.send_jason_health_checkin_prompt,
            "cron",
            {"hour": 22, "minute": "30,40,50", "max_instances": 1, "coalesce": True},
        ),
        (
            server.send_jason_daily_health_report,
            "cron",
            {"hour": 23, "minute": "30,40,50", "max_instances": 1, "coalesce": True},
        ),
    ]


def test_line_text_handler_saves_checkin_and_supports_manual_report(tmp_path, monkeypatch):
    db = tmp_path / "health-handler.db"
    with sqlite3.connect(db) as conn:
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_OTHER")
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message.text)),
    )
    text = "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好"
    checkin_event = _text_event("health-checkin", text, user_id="U_JASON")
    server.processed_messages.discard("health-checkin")

    server._handle_message_impl(checkin_event)

    assert "已更新" in replies[-1]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT water_ml FROM daily_health_checkins").fetchone()[0] == 2500

    monkeypatch.setattr(
        server, "build_jason_daily_health_report", lambda _uid, _date: "MANUAL REPORT"
    )
    report_event = _text_event("health-report", "今日健康日報", user_id="U_JASON")
    server.processed_messages.discard("health-report")
    server._handle_message_impl(report_event)
    assert replies[-1] == "MANUAL REPORT"
