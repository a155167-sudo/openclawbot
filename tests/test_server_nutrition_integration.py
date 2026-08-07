import os
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server
from nutrition_system import (
    confirm_pending_label,
    daily_consumed_totals,
    ensure_nutrition_schema,
    insert_approved_meal_photo_log,
    new_id,
    save_pending_label,
    set_nutrition_input_state,
    update_pending_consumption,
    utcish_now,
)
from daily_health_report import ensure_daily_health_schema, get_daily_health_checkin
from meal_photo_system import (
    apply_meal_photo_action,
    ensure_meal_photo_schema,
    get_meal_photo_draft,
    save_meal_photo_draft,
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


def test_add_frequent_food_resets_stale_dashboard_totals_before_first_log(tmp_path, monkeypatch):
    db = tmp_path / "frequent-food-cross-day.db"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
                today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL);
            CREATE TABLE frequent_foods (
                user_id TEXT, meal_name TEXT, last_cal REAL, last_pro REAL,
                use_count INTEGER DEFAULT 1, last_used_at TEXT,
                PRIMARY KEY (user_id, meal_name));
            CREATE TABLE recent_meal_logs (
                user_id TEXT PRIMARY KEY, meal_name TEXT, base_cal REAL, base_pro REAL,
                current_cal REAL, current_pro REAL, meal_date TEXT,
                source_text TEXT, updated_at TEXT);
        """)
        conn.execute(
            "INSERT INTO health_profile VALUES ('U1',900,60,'昨天早餐、昨天晚餐','2000-01-01',2000,100)"
        )
        conn.execute(
            "INSERT INTO frequent_foods VALUES ('U1','無糖優格',62,4,1,'2000-01-01')"
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "build_meal_log_flex", lambda *args, **kwargs: {"ok": True})

    server.add_frequent_food_to_today("U1", "無糖優格")

    with sqlite3.connect(db) as conn:
        values = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items,today_date "
            "FROM health_profile WHERE user_id='U1'"
        ).fetchone()
    assert values == (62.0, 4.0, "無糖優格", today)


def test_mark_planned_meal_resets_stale_dashboard_totals_before_first_log(tmp_path, monkeypatch):
    db = tmp_path / "planned-meal-cross-day.db"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
                today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL);
            CREATE TABLE planned_meal_checks (
                user_id TEXT, meal_date TEXT, meal_slot TEXT, meal_name TEXT,
                cal REAL, pro REAL, checked_at TEXT,
                PRIMARY KEY (user_id, meal_date, meal_slot));
            CREATE TABLE recent_meal_logs (
                user_id TEXT PRIMARY KEY, meal_name TEXT, base_cal REAL, base_pro REAL,
                current_cal REAL, current_pro REAL, meal_date TEXT,
                source_text TEXT, updated_at TEXT);
        """)
        conn.execute(
            "INSERT INTO health_profile VALUES ('U1',900,60,'昨天早餐、昨天晚餐','2000-01-01',2000,100)"
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "get_dashboard_data",
        lambda user_id: {"today_lunch": "舒肥雞胸餐", "lunch_cal": 500, "lunch_pro": 40},
    )
    monkeypatch.setattr(server, "build_meal_log_flex", lambda *args, **kwargs: {"ok": True})

    server.mark_planned_meal_as_eaten("U1", "午餐")

    with sqlite3.connect(db) as conn:
        values = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items,today_date "
            "FROM health_profile WHERE user_id='U1'"
        ).fetchone()
    assert values == (500.0, 40.0, "舒肥雞胸餐", today)


def test_health_check_uses_configured_sqlite_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "volume"
    db_path = data_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(data_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    assert server.health_check() == {"status": "ok", "service": "openclawbot"}
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "usage", "health_profile", "food_catalog", "food_logs",
        "nutrition_sheet_outbox", "pending_meal_photo_drafts", "meal_photo_events",
        "meal_photo_schema_versions",
    } <= tables
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE meal_photo_events")
        conn.commit()
    with pytest.raises(server.HTTPException) as exc_info:
        server.health_check()
    assert exc_info.value.status_code == 503


def test_health_check_rejects_meal_photo_schema_version_one(tmp_path, monkeypatch):
    data_dir = tmp_path / "volume-version"
    db_path = data_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(data_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meal_photo_schema_versions SET version=1 WHERE component='meal_photo_system'"
        )
        conn.commit()
    with pytest.raises(server.HTTPException) as exc_info:
        server.health_check()
    assert exc_info.value.status_code == 503


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


def test_parse_multiple_nutrition_corrections_accepts_common_chinese_punctuation():
    corrections, errors = server.parse_nutrition_corrections(
        "熱量 204、蛋白質 16.4；鈉：48mg"
    )
    assert errors == []
    assert corrections == [
        ("calories_kcal", 204.0),
        ("protein_g", 16.4),
        ("sodium_mg", 48.0),
    ]


def test_parse_multiple_nutrition_corrections_reports_invalid_fragment_atomically():
    corrections, errors = server.parse_nutrition_corrections(
        "熱量 204、蛋白質 很多、鈉 48"
    )
    assert corrections == [("calories_kcal", 204.0), ("sodium_mg", 48.0)]
    assert errors == ["蛋白質 很多"]


def test_parse_multiple_nutrition_corrections_rejects_conflicting_units():
    corrections, errors = server.parse_nutrition_corrections(
        "熱量 204g、鈉 48g、蛋白質 16.4mg"
    )
    assert corrections == []
    assert errors == ["熱量單位應為 kcal", "鈉單位應為 mg", "蛋白質單位應為 g"]


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


def test_text_handler_applies_multiple_nutrients_in_one_atomic_message(tmp_path, monkeypatch):
    db = tmp_path / "edit-multiple-nutrients.db"
    payload = {
        **valid_label(), "package_amount": 400,
        "per_serving": {
            **valid_label()["per_serving"],
            "calories_kcal": 228,
            "protein_g": 21.2,
            "sodium_mg": 488,
        },
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
    server._handle_message_impl(_text_event("multi-edit-button", f"修改營養數字:{token}"))
    server._handle_message_impl(
        _text_event("multi-edit-value", "熱量 204、蛋白質 16.4；鈉：48mg")
    )
    with sqlite3.connect(db) as conn:
        stored = json.loads(conn.execute(
            "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0])
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
    assert stored["per_serving"]["calories_kcal"] == 204
    assert stored["per_serving"]["protein_g"] == 16.4
    assert stored["per_serving"]["sodium_mg"] == 48
    assert state_count == 0
    assert isinstance(replies[-1], list)
    assert "已更新 3 個項目" in replies[-1][0].text


def test_text_handler_does_not_partially_apply_malformed_multiple_edit(tmp_path, monkeypatch):
    db = tmp_path / "edit-multiple-invalid.db"
    payload = {
        **valid_label(),
        "per_serving": {**valid_label()["per_serving"], "calories_kcal": 228, "protein_g": 21.2},
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
    server._handle_message_impl(_text_event("invalid-multi-button", f"修改營養數字:{token}"))
    server._handle_message_impl(
        _text_event("invalid-multi-value", "熱量 204、蛋白質 很多")
    )
    with sqlite3.connect(db) as conn:
        stored = json.loads(conn.execute(
            "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0])
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
    assert stored["per_serving"]["calories_kcal"] == 228
    assert stored["per_serving"]["protein_g"] == 21.2
    assert state_count == 1
    assert "蛋白質 很多" in replies[-1].text


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
    assert isinstance(replies[0], list)
    assert "已更新 1 個項目（重送確認）" in replies[0][0].text
    assert "鈉：488 → 48 mg" in replies[0][0].text
    assert "0 個項目" not in replies[0][0].text

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


def meal_photo_payload():
    return {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": [
            {"name": "高麗菜", "category": "vegetable", "confidence": 0.98},
            {"name": "青花菜", "category": "vegetable", "confidence": 0.97},
        ],
        "uncertain_items": ["上方棕色主菜種類不明"],
        "starch_visibility": "not_visible",
        "oil_sauce_status": "unknown",
        "observed_at": "2026-07-22T22:37:00+08:00",
        "observed_at_confidence": 0.99,
    }


def test_vision_prompt_requests_observations_not_fake_food_photo_nutrients():
    prompt = server.build_nutrition_vision_prompt()
    food_section = prompt.split("若只有餐盤照片", 1)[1]
    assert "visible_items" in food_section
    assert "uncertain_items" in food_section
    assert "starch_visibility" in food_section
    assert "不可估算熱量" in food_section
    assert '"calories_kcal":0' not in food_section


def test_unknown_image_reply_mentions_meal_photos(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(
        server.line_bot_api, "get_message_content",
        lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "status": "success", "image_type": "unknown"
        })))]
    )
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **_: response)
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    event = SimpleNamespace(
        message=SimpleNamespace(id="unknown-image-message"),
        source=SimpleNamespace(user_id="U_UNKNOWN"), reply_token="reply-unknown",
        timestamp=1784740620000,
    )
    server.processed_messages.discard(event.message.id)

    server.handle_image_message(event)

    assert len(replies) == 1
    assert "餐點照片" in replies[0].text
    assert "Garmin" in replies[0].text
    assert "營養標示" in replies[0].text


def test_food_photo_image_handler_stages_durable_unknown_safe_flex(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-handler.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(
        server.line_bot_api,
        "get_message_content",
        lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=__import__("json").dumps(meal_photo_payload())))]
    )
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **_: response)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        message=SimpleNamespace(id="meal-photo-message"),
        source=SimpleNamespace(user_id="U_MEAL"),
        reply_token="reply",
        timestamp=1784740620000,
    )
    server.processed_messages.discard(event.message.id)

    server.handle_image_message(event)

    assert len(replies) == 1
    text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "餐點照片辨識" in text
    assert "NA（尚未估算）" in text
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        row = conn.execute(
            "SELECT token,source_image_ref,status FROM pending_meal_photo_drafts WHERE user_id='U_MEAL'"
        ).fetchone()
    assert row[1].startswith("nutrition-image:")
    assert row[2] == "awaiting_confirmation"
    image_path = server._nutrition_image_path(row[1])
    assert image_path is not None and os.path.exists(image_path)


def test_meal_photo_postback_request_add_then_text_updates_confirmation_card(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-add-item.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_ADD", payload=meal_photo_payload()
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    request_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:add"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-add-request",
        webhook_event_id="WEBHOOK-ADD-REQUEST", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(request_event)

    assert len(replies) == 1
    assert "請直接輸入" in replies[0].text
    with sqlite3.connect(db) as conn:
        waiting = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert waiting["status"] == "awaiting_item_name"
    assert waiting["version"] == 2

    replies.clear()
    text_event = _text_event("ADD-ITEM-NAME", "玉米筍", user_id="U_MEAL")
    server.processed_messages.discard(text_event.message.id)
    server._handle_message_impl(text_event)

    assert len(replies) == 1
    assert "選擇食材分類" in replies[0].text
    category_actions = [item.action.data for item in replies[0].quick_reply.items]
    assert any(f"mp:v1:{token}:2:add_item:vegetable:" in data for data in category_actions)
    assert f"mp:v1:{token}:2:cancel_add" in category_actions
    with sqlite3.connect(db) as conn:
        still_waiting = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert still_waiting["status"] == "awaiting_item_name"
    assert still_waiting["version"] == 2

    replies.clear()
    vegetable_data = next(
        data for data in category_actions
        if f"mp:v1:{token}:2:add_item:vegetable:" in data
    )
    category_event = SimpleNamespace(
        postback=SimpleNamespace(data=vegetable_data),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-add-category",
        webhook_event_id="WEBHOOK-ADD-CATEGORY", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(category_event)

    assert len(replies) == 1
    card_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "玉米筍" in card_text
    assert f"mp:v1:{token}:3:start" in card_text
    with sqlite3.connect(db) as conn:
        updated = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert updated["status"] == "awaiting_confirmation"
    assert updated["version"] == 3
    assert updated["payload"]["visible_items"][-1] == {
        "name": "玉米筍", "category": "vegetable", "confidence": 1.0,
    }


def test_meal_photo_request_add_can_be_cancelled_from_quick_reply(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-cancel-add-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CANCEL", source_message_id="M_CANCEL", payload=meal_photo_payload()
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    request_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:request_add"),
        source=SimpleNamespace(user_id="U_CANCEL"), reply_token="reply-request-cancel",
        webhook_event_id="WEBHOOK-REQUEST-CANCEL", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(request_event)

    cancel_data = next(
        item.action.data for item in replies[0].quick_reply.items
        if item.action.data.endswith(":cancel_add")
    )
    replies.clear()
    cancel_event = SimpleNamespace(
        postback=SimpleNamespace(data=cancel_data),
        source=SimpleNamespace(user_id="U_CANCEL"), reply_token="reply-cancel-add",
        webhook_event_id="WEBHOOK-CANCEL-ADD", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(cancel_event)

    assert len(replies) == 1
    assert replies[0].type == "flex"
    with sqlite3.connect(db) as conn:
        restored = get_meal_photo_draft(conn, user_id="U_CANCEL", token=token)
    assert restored["status"] == "awaiting_confirmation"
    assert restored["version"] == 3


def test_meal_photo_typed_cancel_is_not_treated_as_an_ingredient(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-typed-cancel.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_TYPED_CANCEL", source_message_id="M_TYPED_CANCEL",
            payload=meal_photo_payload(),
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-TYPED-CANCEL", user_id="U_TYPED_CANCEL",
            token=token, expected_version=1, action="request_add",
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    text_event = _text_event("TYPED-CANCEL", "取消新增", user_id="U_TYPED_CANCEL")
    server.processed_messages.discard(text_event.message.id)

    server._handle_message_impl(text_event)

    assert len(replies) == 1
    assert "不會加入" in replies[0].text
    assert any(
        item.action.data.endswith(":cancel_add")
        for item in replies[0].quick_reply.items
    )
    with sqlite3.connect(db) as conn:
        waiting = get_meal_photo_draft(conn, user_id="U_TYPED_CANCEL", token=token)
    assert waiting["status"] == "awaiting_item_name"
    assert [item["name"] for item in waiting["payload"]["visible_items"]] == [
        "高麗菜", "青花菜"
    ]


def test_meal_photo_add_item_rejects_name_when_encoded_postback_exceeds_line_limit(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-long-item.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_LONG", source_message_id="M_LONG", payload=meal_photo_payload()
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-LONG", user_id="U_LONG", token=token,
            expected_version=1, action="request_add",
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    text_event = _text_event("ADD-LONG-NAME", "🍣" * 60, user_id="U_LONG")
    server.processed_messages.discard(text_event.message.id)

    server._handle_message_impl(text_event)

    assert len(replies) == 1
    assert "名稱過長" in replies[0].text
    assert replies[0].quick_reply is None
    with sqlite3.connect(db) as conn:
        waiting = get_meal_photo_draft(conn, user_id="U_LONG", token=token)
    assert waiting["status"] == "awaiting_item_name"
    assert waiting["version"] == 2


def test_meal_photo_postback_finalizes_estimate(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-postback-final.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_MEAL")
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M1", payload=meal_photo_payload()
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"PREP-{index}", user_id="U_MEAL", token=token,
                expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:7:answer:sauce_level:half"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-final",
        webhook_event_id="WEBHOOK-FINAL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    assert len(replies) == 1
    estimate_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "照片估算" in estimate_text
    assert "審核並加入" in estimate_text
    assert f"mpr:v1:{token}:8:start" in estimate_text
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert draft["status"] == "estimated"
    assert draft["estimate"]["starch_exchange"] == {"min": 0.0, "max": 0.0, "basis": "user_confirmed_none"}


def test_customer_estimate_pushes_review_request_to_configured_admin(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-customer-push.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_FALLBACK_ADMIN")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_CUSTOMER",
            payload=meal_photo_payload(),
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"CUSTOMER-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    pushes = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: replies.append(message),
            push_message=lambda target, message, **_kwargs: pushes.append((target, message)),
        ),
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:7:answer:sauce_level:half"),
        source=SimpleNamespace(user_id="U_CUSTOMER"), reply_token="reply-customer-final",
        webhook_event_id="WEBHOOK-CUSTOMER-FINAL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    customer_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "審核並加入" not in customer_text
    assert len(pushes) == 1 and pushes[0][0] == "U_ADMIN"
    admin_payload = pushes[0][1]
    assert isinstance(admin_payload, list)
    admin_text = json.dumps(
        [json.loads(str(message)) for message in admin_payload], ensure_ascii=False
    )
    assert "新的餐點審核需求" in admin_text
    assert f"mpr:v1:{token}:8:start" in admin_text


def test_owner_notification_retries_after_failure_and_then_deduplicates(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-notification-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_NOTIFY",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    attempts = {"count": 0}

    def flaky_push(_target, _message, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(push_message=flaky_push))
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is False
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert attempts["count"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM meal_photo_notification_events
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0] == 1


def test_notification_retry_postback_works_after_original_version_changes(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-notification-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_NOTIFY_POSTBACK",
            payload=meal_photo_payload(),
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='rejected',version=10 WHERE token=?",
            (token,),
        )
        conn.commit()
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    attempts = {"count": 0}
    replies = []

    def flaky_push(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")

    api = SimpleNamespace(
        push_message=flaky_push,
        reply_message=lambda _token, message: replies.append(message),
    )
    monkeypatch.setattr(server, "line_bot_api", api)
    assert server.push_meal_photo_return_to_owner(draft) is False
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mprn:v1:{token}:rejected"),
        source=SimpleNamespace(user_id="U_ADMIN"),
        reply_token="RETRY-REPLY",
    )
    server.handle_meal_photo_postback(event)
    assert attempts["count"] == 2
    assert replies[-1].text == "✅ 客戶通知已送達。"


def test_approved_notification_retry_uses_exact_protein_exchange_shape(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-approved-notification-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    review = {
        "protein_class": "medium", "protein_exchange": 2.5,
        "starch_exchange": 6, "vegetable_exchange": 2,
        "milk_exchange": 0, "fruit_exchange": 1,
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_APPROVED_NOTIFY_RETRY",
            payload=meal_photo_payload(),
        )
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET status='approved',version=15,review_json=? WHERE token=?""",
            (json.dumps(review), token),
        )
        conn.commit()
    pushes = []
    replies = []
    api = SimpleNamespace(
        push_message=lambda target, message, **kwargs: pushes.append((target, message, kwargs)),
        reply_message=lambda _token, message: replies.append(message),
    )
    monkeypatch.setattr(server, "line_bot_api", api)
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mprn:v1:{token}:approved"),
        source=SimpleNamespace(user_id="U_ADMIN"),
        reply_token="APPROVED-RETRY-REPLY",
    )
    server.handle_meal_photo_postback(event)
    assert len(pushes) == 1
    assert "估算熱量 698.5 kcal" in pushes[0][1].text
    assert "蛋白質 31.5g" in pushes[0][1].text
    assert replies[-1].text == "✅ 客戶通知已送達。"


def test_successful_push_with_marker_failure_is_not_immediately_released(
    tmp_path, monkeypatch,
):
    db = tmp_path / "meal-photo-marker-failure.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_MARKER_FAILURE",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    api_attempts = []

    def successful_push(_target, _message, **kwargs):
        api_attempts.append(kwargs.get("retry_key"))

    monkeypatch.setattr(
        server, "line_bot_api", SimpleNamespace(push_message=successful_push)
    )
    monkeypatch.setattr(
        server, "complete_meal_photo_notification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("marker unavailable")),
    )
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert server.push_meal_photo_approval_to_owner(draft, result) is False
    assert len(api_attempts) == 1
    assert api_attempts[0] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"meal-photo:{token}:owner_approved")
    )
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            """SELECT status FROM meal_photo_notification_claims
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0]
    assert status == "sending"


def test_line_retry_conflict_with_accepted_request_id_completes_marker(
    tmp_path, monkeypatch,
):
    db = tmp_path / "meal-photo-accepted-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_ACCEPTED_RETRY",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)

    class AcceptedRetryConflict(Exception):
        status_code = 409
        accepted_request_id = "REQ-ALREADY-ACCEPTED"

    def accepted_conflict(*_args, **_kwargs):
        raise AcceptedRetryConflict("already accepted")

    monkeypatch.setattr(
        server, "line_bot_api", SimpleNamespace(push_message=accepted_conflict)
    )
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    with sqlite3.connect(db) as conn:
        marker_count = conn.execute(
            """SELECT COUNT(*) FROM meal_photo_notification_events
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0]
        claim_status = conn.execute(
            """SELECT status FROM meal_photo_notification_claims
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0]
    assert marker_count == 1
    assert claim_status == "delivered"


def test_retry_key_does_not_leak_into_shared_line_client_headers(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-retry-header.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_RETRY_HEADER",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)

    class HeaderMutatingLineApi:
        def __init__(self):
            self.headers = {"Authorization": "Bearer test"}
            self.calls = []

        def push_message(self, target, message, **kwargs):
            retry_key = kwargs.get("retry_key")
            if retry_key:
                self.headers["X-Line-Retry-Key"] = retry_key
            self.calls.append((target, message, kwargs))

    api = HeaderMutatingLineApi()
    monkeypatch.setattr(server, "line_bot_api", api)
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert len(api.calls) == 1
    assert "X-Line-Retry-Key" not in api.headers
    assert api.headers == {"Authorization": "Bearer test"}


def test_owner_notification_claim_blocks_concurrent_duplicate_push(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-notification-concurrent.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_NOTIFY_CONCURRENT",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    entered = threading.Event()
    release = threading.Event()
    attempts = []

    def blocking_push(*_args, **_kwargs):
        attempts.append(1)
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(push_message=blocking_push))
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    first_result = []
    worker = threading.Thread(
        target=lambda: first_result.append(
            server.push_meal_photo_approval_to_owner(draft, result)
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    second_result = server.push_meal_photo_approval_to_owner(draft, result)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert first_result == [True]
    assert second_result is False
    assert len(attempts) == 1


def test_bound_admin_authorization_fails_closed_on_database_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "missing" / "db.sqlite"))
    monkeypatch.setattr(server, "ADMIN_UID", "U" + "a" * 32)
    with pytest.raises(PermissionError, match="無法驗證管理員"):
        server.get_bound_admin_uid_for_authorization()


def test_pending_meal_photo_admin_command_lists_cross_user_review_buttons(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-pending-command.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_BOUND_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_BOUND_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_PENDING",
            payload=meal_photo_payload(),
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"PENDING-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )

    with pytest.raises(PermissionError, match="管理員限定"):
        server.build_pending_meal_photo_review_message("U_FORGED")
    message = server.build_pending_meal_photo_review_message("U_BOUND_ADMIN")
    assert "待審餐點（1筆" in message.text
    assert "U_CUSTOMER"[-8:] in message.text
    actions = [item.action.data for item in message.quick_reply.items]
    assert actions == [f"mpr:v1:{token}:8:start"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET status='reviewing',version=9,review_json='{}' WHERE token=?""",
            (token,),
        )
        conn.commit()
    resumed = server.build_pending_meal_photo_review_message("U_BOUND_ADMIN")
    assert "審核中" in resumed.text
    assert resumed.quick_reply.items[0].action.data == f"mpr:v1:{token}:9:resume"


def test_admin_meal_photo_review_postbacks_apply_formal_totals(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-admin-review.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_CUSTOMER", payload=meal_photo_payload(),
            consumed_at="2026-07-23T12:10:00+08:00", meal_slot="午餐",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("starch_portion", "one_half_bowl"),
            ("vegetable_portion", "none"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"PREP-ADMIN-{index}", user_id="U_CUSTOMER", token=token,
                expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    pushes = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    monkeypatch.setattr(
        server.line_bot_api, "push_message",
        lambda target, message, **_kwargs: pushes.append((target, message)),
    )

    def send(data, event_id):
        event = SimpleNamespace(
            postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id="U_ADMIN"),
            reply_token=f"reply-{event_id}", webhook_event_id=event_id,
            timestamp=1784740620000,
        )
        server.handle_meal_photo_postback(event)
        return replies[-1]

    first = send(f"mpr:v1:{token}:8:start", "ADMIN-START")
    assert "蛋白質分類" in first.text
    assert any(":9:set:protein_class:medium" in item.action.data for item in first.quick_reply.items)
    resumed_step = send(f"mpr:v1:{token}:9:resume", "ADMIN-RESUME")
    assert "蛋白質分類" in resumed_step.text
    assert any(":9:set:protein_class:medium" in item.action.data for item in resumed_step.quick_reply.items)
    send(f"mpr:v1:{token}:9:set:protein_class:medium", "ADMIN-CLASS")
    send(f"mpr:v1:{token}:10:set:protein_exchange:2.5", "ADMIN-PROTEIN")
    send(f"mpr:v1:{token}:11:set:starch_exchange:6", "ADMIN-STARCH")
    send(f"mpr:v1:{token}:12:set:milk_exchange:0", "ADMIN-MILK")
    ready = send(f"mpr:v1:{token}:13:set:fruit_exchange:0", "ADMIN-FRUIT")
    ready_text = json.dumps(json.loads(str(ready.contents)), ensure_ascii=False)
    assert "最終核准份量" in ready_text
    assert f"mpr:v1:{token}:14:approve" in ready_text

    done = send(f"mpr:v1:{token}:14:approve", "ADMIN-APPROVE")
    done_text = json.dumps(json.loads(str(done.contents)), ensure_ascii=False)
    assert "已核准｜已計入正式份量" in done_text
    assert '"主食"' in done_text and '"6份"' in done_text
    assert '"中脂蛋白"' in done_text and '"2.5份"' in done_text
    with sqlite3.connect(db) as conn:
        totals = daily_consumed_totals(conn, user_id="U_CUSTOMER", date_iso="2026-07-23")
        admin_totals = daily_consumed_totals(conn, user_id="U_ADMIN", date_iso="2026-07-23")
        assert totals["starch_exchange"] == 6.0
        assert totals["protein_medium_exchange"] == 2.5
        assert admin_totals["starch_exchange"] == 0.0
    assert pushes and pushes[-1][0] == "U_CUSTOMER"
    assert "已由營養師核准" in pushes[-1][1].text
    push_count = len(pushes)
    replayed_done = send(f"mpr:v1:{token}:14:approve", "ADMIN-APPROVE")
    assert "已核准｜已計入正式份量" in json.dumps(
        json.loads(str(replayed_done.contents)), ensure_ascii=False
    )
    assert len(pushes) == push_count
    with sqlite3.connect(db) as conn:
        assert daily_consumed_totals(
            conn, user_id="U_CUSTOMER", date_iso="2026-07-23"
        )["starch_exchange"] == 6.0


def test_admin_meal_photo_reject_returns_result_to_customer_without_formal_log(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-admin-reject.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_REJECT",
            payload=meal_photo_payload(),
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"REJECT-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
    replies, pushes = [], []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: replies.append(message),
            push_message=lambda target, message, **_kwargs: pushes.append((target, message)),
        ),
    )
    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mpr:v1:{token}:8:start"),
        source=SimpleNamespace(user_id="U_ADMIN"), reply_token="reject-start",
        webhook_event_id="REJECT-START", timestamp=1784740620000,
    ))
    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mpr:v1:{token}:9:reject"),
        source=SimpleNamespace(user_id="U_ADMIN"), reply_token="reject-final",
        webhook_event_id="REJECT-FINAL", timestamp=1784740620001,
    ))
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
        assert draft["status"] == "rejected"
        assert conn.execute("SELECT COUNT(*) FROM food_logs WHERE user_id='U_CUSTOMER'").fetchone()[0] == 0
    assert "已退回客戶" in replies[-1].text
    assert pushes[-1][0] == "U_CUSTOMER"
    assert "退回" in pushes[-1][1].text


def test_meal_photo_postback_replays_after_reply_failure_without_double_update(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_POST", payload=meal_photo_payload()
        )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:answer:scope:visible_only"),
        source=SimpleNamespace(user_id="U_MEAL"),
        reply_token="reply-postback",
        webhook_event_id="WEBHOOK-EVENT-1",
        timestamp=1784740620000,
    )
    calls = {"count": 0}

    def fail_first_reply(_token, _message):
        calls["count"] += 1
        raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server.line_bot_api, "reply_message", fail_first_reply)
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server.handle_meal_photo_postback(event)
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
        events = conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0]
    assert draft["version"] == 2
    assert draft["answers"]["scope"] == "visible_only"
    assert events == 1

    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    server.handle_meal_photo_postback(event)
    with sqlite3.connect(db) as conn:
        replayed = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
        events = conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0]
    assert replayed["version"] == 2
    assert events == 1
    assert len(replies) == 1
    assert "主要蛋白質食物" in replies[0].text
    option_data = [item.action.data for item in replies[0].quick_reply.items]
    answer_data = [data for data in option_data if ":answer:" in data]
    assert answer_data and all(
        data.startswith(f"mp:v1:{token}:2:answer:protein_type:") for data in answer_data
    )
    assert f"mp:v1:{token}:2:cancel" in option_data


def test_cancel_meal_photo_deletes_file_before_clearing_reference(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-cancel.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn,
            user_id="U_MEAL",
            source_message_id="M2",
            payload=meal_photo_payload(),
            source_image_ref="nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        )
    deleted = []
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda ref: deleted.append(ref) or True)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:cancel"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-cancel",
        webhook_event_id="WEBHOOK-CANCEL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    assert deleted == ["nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"]
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status,source_image_ref,observed_payload_json FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
    assert row == ("cancelled", "", "{}")
    assert "已取消" in replies[0].text


def test_cleanup_retries_expired_meal_photo_image_and_scrubs_payload(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-expiry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M3",
            payload=meal_photo_payload(), source_image_ref=ref,
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: False)

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        status, payload, retained_ref = conn.execute(
            "SELECT status,observed_payload_json,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
    assert (status, payload, retained_ref) == ("expired", "{}", ref)

    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: True)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        cleared_ref = conn.execute(
            "SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0]
    assert cleared_ref == ""


def test_cleanup_removes_old_meal_photo_event_before_parent_tombstone(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-retention.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_OLD",
            payload=meal_photo_payload(),
        )
        apply_meal_photo_action(
            conn, event_id="OLD-CANCEL", user_id="U_MEAL", token=token,
            expected_version=1, action="cancel",
        )
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET retired_at='2000-01-01T00:00:00+08:00',source_image_ref=''
               WHERE token=?""", (token,)
        )
        conn.execute(
            "UPDATE meal_photo_events SET created_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE token=?", (token,)
        ).fetchone()[0]
        draft_count = conn.execute(
            "SELECT COUNT(*) FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0]
    assert event_count == 0
    assert draft_count == 0


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


def test_search_category_button_lists_single_items_instead_of_searching_literal_label(
    tmp_path, monkeypatch,
):
    db = tmp_path / "search-menu-category.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [
            {
                "name": "雞肉", "category": "side", "calories_kcal": 85,
                "protein_g": 18, "fat_g": 2, "carbohydrate_g": 0,
            },
            {
                "name": "冷泡茶", "category": "drink", "calories_kcal": 2,
                "protein_g": 0, "fat_g": 0, "carbohydrate_g": 0,
            },
            {
                "name": "雞肉便當", "category": "main", "calories_kcal": 484,
                "protein_g": 35, "fat_g": 19, "carbohydrate_g": 17,
            },
        ],
    )
    server.sync_menu_to_food_catalog()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-MENU", "搜尋", user_id="U1")
    )
    menu_payload = json.loads(replies[-1].as_json_string())
    single_action = next(
        item["action"]["text"]
        for item in menu_payload["contents"]["body"]["contents"]
        if item.get("type") == "box"
        for item in item.get("contents", [])
        if item.get("action", {}).get("label") == "🔍 單品"
    )
    assert single_action == "搜尋 單品"

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-SIDE", single_action, user_id="U1")
    )
    assert replies[-1].type == "flex"
    result_payload = json.dumps(
        json.loads(replies[-1].as_json_string()), ensure_ascii=False
    )
    assert "雞肉" in result_payload
    assert "冷泡茶" not in result_payload
    assert "雞肉便當" not in result_payload
    assert "找不到「單品」" not in result_payload


def test_search_drink_category_excludes_single_items_and_main_dishes(tmp_path, monkeypatch):
    db = tmp_path / "search-drink-category.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [
            {
                "name": "豆腐", "category": "side", "calories_kcal": 137,
                "protein_g": 15, "fat_g": 6, "carbohydrate_g": 9,
            },
            {
                "name": "燕麥豆漿", "category": "drink", "calories_kcal": 287,
                "protein_g": 11.3, "fat_g": 5, "carbohydrate_g": 49,
            },
            {
                "name": "豆腐便當", "category": "main", "calories_kcal": 536,
                "protein_g": 32, "fat_g": 23, "carbohydrate_g": 26,
            },
        ],
    )
    server.sync_menu_to_food_catalog()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-DRINK", "搜尋 飲品", user_id="U1")
    )
    assert replies[-1].type == "flex"
    result_payload = json.dumps(
        json.loads(replies[-1].as_json_string()), ensure_ascii=False
    )
    assert "燕麥豆漿" in result_payload
    assert "豆腐便當" not in result_payload
    assert '"text": "豆腐"' not in result_payload


def test_search_single_item_category_paginates_without_losing_category(
    tmp_path, monkeypatch,
):
    db = tmp_path / "search-single-category-pages.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    side_names = [f"配菜{index:02d}" for index in range(1, 14)]
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [
            {
                "name": name, "category": "side", "calories_kcal": 50 + index,
                "protein_g": 5, "fat_g": 1, "carbohydrate_g": 5,
            }
            for index, name in enumerate(side_names, start=1)
        ],
    )
    server.sync_menu_to_food_catalog()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-SIDE-P1", "搜尋 單品", user_id="U1")
    )
    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    assert len(page1) == 12
    next_action = page1[-1]["body"]["contents"][0]["action"]
    assert next_action["text"] == "搜尋下一頁 2 單品"
    page1_text = json.dumps(page1[:-1], ensure_ascii=False)

    server._handle_message_impl(
        _text_event(
            "SEARCH-CATEGORY-SIDE-P2", next_action["text"], user_id="U1"
        )
    )
    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    assert len(page2) == 2
    page2_text = json.dumps(page2, ensure_ascii=False)
    assert all((name in page1_text) != (name in page2_text) for name in side_names)


def test_menu_category_sync_never_publicizes_same_named_private_label(
    tmp_path, monkeypatch,
):
    db = tmp_path / "menu-category-private-collision.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,source_type,owner_user_id,visibility,
                per_serving_json,per_100_json,exchange_json,exchange_review_status,
                fingerprint,verification_status,created_at,updated_at)
               VALUES ('food_private_chicken','雞肉','label','U_PRIVATE','private',
                       '{}','{}','{}','approved','private-chicken','user_confirmed',?,?)""",
            (now, now),
        )
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [{
            "name": "雞肉", "category": "side", "calories_kcal": 85,
            "protein_g": 18, "fat_g": 2, "carbohydrate_g": 0,
        }],
    )

    server.sync_menu_to_food_catalog()

    with sqlite3.connect(db) as conn:
        private_row = conn.execute(
            """SELECT visibility,menu_category FROM food_catalog
               WHERE food_id='food_private_chicken'"""
        ).fetchone()
        official_row = conn.execute(
            """SELECT visibility,menu_category FROM food_catalog
               WHERE product_name='雞肉' AND owner_user_id='system'"""
        ).fetchone()
    assert private_row == ("private", "")
    assert official_row == ("public", "side")


def test_search_and_quick_relog_creates_food_log(tmp_path, monkeypatch):
    db = tmp_path / "search-relog.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        fid = new_id("food")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES (?,'舒肥雞胸','好市多','','user_private_food','U1','private',
                       100,'g',1,'{"calories_kcal":120,"protein_g":25}','{}',
                       '{"starch_exchange":0,"protein_medium_exchange":1}',
                       'approved','fp_chicken','',0,'user_confirmed',?,?)""",
            (fid, now, now),
        )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    search_event = _text_event("SEARCH-1", "搜尋 雞胸", user_id="U1")
    server._handle_message_impl(search_event)
    assert len(replies) == 1
    carousel = replies[0]
    assert carousel.type == "flex"
    # contents is a CarouselContainer; check structure via serialization
    raw = json.loads(carousel.as_json_string())
    bubble_inner = json.dumps(raw, ensure_ascii=False)
    assert "舒肥雞胸" in bubble_inner
    assert f"relog:v1:{fid}:start" in bubble_inner

    start_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"relog:v1:{fid}:start"),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-relog-start", webhook_event_id="RELOG-START",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(start_event)
    assert "請選擇份量" in replies[-1].text
    assert any(f"relog:v1:{fid}:servings:1" in item.action.data for item in replies[-1].quick_reply.items)

    sv_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"relog:v1:{fid}:servings:1.5"),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-relog-sv", webhook_event_id="RELOG-SV",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(sv_event)
    assert "1.5" in replies[-1].text and "餐別" in replies[-1].text

    meal_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"relog:v1:{fid}:sv:1.5:meal:午餐"),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-relog-meal", webhook_event_id="RELOG-MEAL",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(meal_event)
    assert "✅ 已記錄" in replies[-1].text
    assert "舒肥雞胸" in replies[-1].text
    assert "1.5" in replies[-1].text
    assert "180" in replies[-1].text
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT consumed_servings,meal_slot FROM food_logs WHERE user_id='U1'"
        ).fetchone()
        assert row[0] == 1.5
        assert row[1] == "午餐"


def test_search_my_food_natural_language_alias_returns_private_library(tmp_path, monkeypatch):
    db = tmp_path / "search-my-alias.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for owner, name in (("U1", "Jason私人雞胸"), ("U2", "別人的私人雞胸")):
            fid = new_id("food")
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food',?,'private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, name, owner, json.dumps({"calories_kcal": 123}), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(
        _text_event("SEARCH-MY-ALIAS-1", "搜尋 我的食物", user_id="U1")
    )

    assert len(replies) == 1
    assert replies[0].type == "flex"
    payload = json.loads(replies[0].as_json_string())
    text = json.dumps(payload, ensure_ascii=False)
    assert "Jason私人雞胸" in text
    assert "別人的私人雞胸" not in text


def test_my_food_search_paginates_with_context_and_within_line_carousel_limit(tmp_path, monkeypatch):
    db = tmp_path / "search-my-pagination.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for index in range(13):
            fid = new_id("food")
            timestamp = f"2026-07-{index + 1:02d}T08:00:00+08:00"
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, f"我的食物{index:02d}", json.dumps({"calories_kcal": 100 + index}), fid, timestamp, timestamp),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("SEARCH-MY-P1", "搜尋 _my", user_id="U1"))

    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    assert len(page1) == 12
    assert {bubble.get("size") for bubble in page1} == {"kilo"}
    next_action = page1[-1]["body"]["contents"][0]["action"]
    assert next_action["text"] == "搜尋下一頁 2 _my"

    server._handle_message_impl(
        _text_event("SEARCH-MY-P2", next_action["text"], user_id="U1")
    )

    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    page2_text = json.dumps(page2, ensure_ascii=False)
    assert len(page2) == 2
    assert "我的食物01" in page2_text
    assert "我的食物00" in page2_text


def test_keyword_food_search_second_page_does_not_repeat_first_page(tmp_path, monkeypatch):
    db = tmp_path / "search-keyword-pagination.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for index in range(13):
            fid = new_id("food")
            timestamp = f"2026-07-{index + 1:02d}T08:00:00+08:00"
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, f"雞胸餐{index:02d}", json.dumps({"calories_kcal": 100 + index}), fid, timestamp, timestamp),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("SEARCH-CHICKEN-P1", "搜尋 雞胸", user_id="U1"))
    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    next_action = page1[-1]["body"]["contents"][0]["action"]
    assert len(page1) == 12
    assert next_action["text"] == "搜尋下一頁 2 雞胸"

    server._handle_message_impl(
        _text_event("SEARCH-CHICKEN-P2", next_action["text"], user_id="U1")
    )
    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    page2_text = json.dumps(page2, ensure_ascii=False)
    assert len(page2) == 2
    assert "雞胸餐01" in page2_text
    assert "雞胸餐00" in page2_text
    assert "雞胸餐12" not in page2_text


def test_keyword_food_search_reaches_items_after_twentieth_result(tmp_path, monkeypatch):
    db = tmp_path / "search-keyword-page-three.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for index in range(30):
            fid = new_id("food")
            timestamp = f"2026-07-30T00:{index:02d}:00+08:00"
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, f"雞胸大量{index:02d}", json.dumps({"calories_kcal": 100 + index}), fid, timestamp, timestamp),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("SEARCH-30-P1", "搜尋 雞胸大量", user_id="U1"))
    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    next2 = page1[-1]["body"]["contents"][0]["action"]["text"]
    server._handle_message_impl(_text_event("SEARCH-30-P2", next2, user_id="U1"))
    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    next3 = page2[-1]["body"]["contents"][0]["action"]["text"]
    server._handle_message_impl(_text_event("SEARCH-30-P3", next3, user_id="U1"))

    page3 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    page3_text = json.dumps(page3, ensure_ascii=False)
    assert len(page3) == 8
    assert "雞胸大量07" in page3_text
    assert "雞胸大量00" in page3_text


def test_search_rejects_pathologically_large_page_number(tmp_path, monkeypatch):
    db = tmp_path / "search-invalid-page.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(
        _text_event("SEARCH-HUGE-PAGE", "搜尋下一頁 " + ("9" * 5000) + " _my", user_id="U1")
    )

    assert "頁碼" in replies[-1].text


def test_search_no_results_shows_guidance(tmp_path, monkeypatch):
    db = tmp_path / "search-empty.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    event = _text_event("SEARCH-EMPTY", "搜尋 不存在的食物", user_id="U1")
    server._handle_message_impl(event)
    assert "找不到" in replies[0].text


def test_breakfast_combo_logs_multiple_foods_at_once(tmp_path, monkeypatch):
    db = tmp_path / "combo.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, exch, kcal in [
            ("穀麥高粱 OATS & HONEY", {"starch_exchange": 1.98}, 197),
            ("草莓穀物脆片", {"starch_exchange": 2.37}, 223),
            ("無糖優格", {"milk_exchange": 0.5}, 62),
        ]:
            fid = new_id("food")
            ps = json.dumps({"calories_kcal": kcal})
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',
                           100,'g',1,?, '{}',
                           ?,'approved',?,'',0,'user_confirmed',?,?)""",
                (fid, name, ps, json.dumps(exch), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    event = _text_event("COMBO-1", "早餐1", user_id="U1")
    server._handle_message_impl(event)
    assert len(replies) == 1
    reply = replies[0]
    assert reply.type == "flex"
    raw = json.loads(reply.as_json_string())
    bubble_text = json.dumps(raw, ensure_ascii=False)
    assert "已記錄" in bubble_text
    assert "穀麥高粱" in bubble_text
    assert "無糖優格" in bubble_text

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT meal_slot FROM food_logs WHERE user_id='U1'"
        ).fetchall()
        assert len(rows) == 3
        assert all(r[0] == "早餐" for r in rows)


def test_dashboard_counts_approved_meal_photo_estimates_once_including_legacy_na_snapshot(tmp_path, monkeypatch):
    db_dir = tmp_path / "photo-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U1','Jason',2000,100,0,0,'',?)""",
            (server.tw_today().isoformat(),),
        )
        ensure_nutrition_schema(conn)
        insert_approved_meal_photo_log(
            conn,
            token="abcdef123456",
            user_id="U1",
            reviewer="U1",
            consumed_at=server.tw_now().isoformat(),
            meal_slot="早餐",
            source_image_ref="photo.jpg",
            observed_payload={
                "visible_items": [
                    {"name": "雞胸肉", "category": "protein", "confidence": 0.9},
                    {"name": "青花菜", "category": "vegetable", "confidence": 0.9},
                ]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0,
                "protein_low_exchange": 0,
                "protein_medium_exchange": 2,
                "protein_high_exchange": 0,
                "starch_exchange": 1,
                "vegetable_exchange": 1,
                "fruit_exchange": 0,
                "fat_exchange": 0,
            },
        )
        # 模擬上線前已核准、但 nutrition_snapshot_json 仍為空的舊照片紀錄。
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json='{}' WHERE source_image_ref='photo.jpg'"
        )
        taipei_0030 = server.tw_now().replace(hour=0, minute=30, second=0, microsecond=0)
        insert_approved_meal_photo_log(
            conn,
            token="abcdef123457",
            user_id="U1",
            reviewer="U1",
            consumed_at=taipei_0030.astimezone(timezone.utc).isoformat(),
            meal_slot="早餐",
            source_image_ref="photo-utc.jpg",
            observed_payload={
                "visible_items": [
                    {"name": "UTC跨日雞胸", "category": "protein", "confidence": 0.9},
                ]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0,
                "protein_low_exchange": 1,
                "protein_medium_exchange": 0,
                "protein_high_exchange": 0,
                "starch_exchange": 0,
                "vegetable_exchange": 0,
                "fruit_exchange": 0,
                "fat_exchange": 0,
            },
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U1")

    assert set(dashboard["food_list"]) == {
        "餐點照片：雞胸肉、青花菜",
        "餐點照片：UTC跨日雞胸",
    }
    assert dashboard["recorded_count"] == 2
    assert dashboard["extra_cal"] == 293.0
    assert dashboard["extra_pro"] == 24.0

    replayed_dashboard = server.get_dashboard_data("U1")
    assert replayed_dashboard["extra_cal"] == 293.0
    assert replayed_dashboard["extra_pro"] == 24.0


def test_dashboard_excludes_other_user_old_unconfirmed_and_non_photo_logs(tmp_path, monkeypatch):
    db_dir = tmp_path / "photo-dashboard-isolation"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    old_day = datetime.fromordinal(server.tw_today().toordinal() - 1).date().isoformat()

    def add_photo(conn, token, user_id, name, consumed_at):
        return insert_approved_meal_photo_log(
            conn, token=token, user_id=user_id, reviewer=user_id,
            consumed_at=consumed_at, meal_slot="早餐", source_image_ref=f"{token}.jpg",
            observed_payload={
                "visible_items": [{"name": name, "category": "protein", "confidence": 0.9}]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0, "protein_low_exchange": 1,
                "protein_medium_exchange": 0, "protein_high_exchange": 0,
                "starch_exchange": 0, "vegetable_exchange": 0,
                "fruit_exchange": 0, "fat_exchange": 0,
            },
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U1','Jason',2000,100,0,0,'',?)""",
            (today,),
        )
        ensure_nutrition_schema(conn)
        add_photo(conn, "a00000000001", "U2", "別人的雞胸", f"{today}T08:00:00+08:00")
        add_photo(conn, "b00000000001", "U1", "昨天的雞胸", f"{old_day}T08:00:00+08:00")
        pending = add_photo(conn, "c00000000001", "U1", "未確認雞胸", f"{today}T09:00:00+08:00")
        conn.execute(
            "UPDATE food_logs SET confirmation_status='pending' WHERE log_id=?",
            (pending["log_id"],),
        )
        non_photo = add_photo(conn, "d00000000001", "U1", "一般食物卡", f"{today}T10:00:00+08:00")
        conn.execute(
            "UPDATE food_catalog SET source_type='user_private_food' WHERE food_id=?",
            (non_photo["food_id"],),
        )
        tampered = add_photo(conn, "e00000000001", "U1", "驗證鏈被改過", f"{today}T11:00:00+08:00")
        conn.execute(
            "UPDATE food_exchange_approvals SET approved_exchange_hash='tampered' WHERE approval_id=?",
            (tampered["approval_id"],),
        )
        fingerprint_tampered = add_photo(
            conn, "f00000000001", "U1", "指紋鏈被改過", f"{today}T12:00:00+08:00"
        )
        conn.execute(
            "UPDATE food_exchange_approvals SET food_fingerprint='tampered' WHERE approval_id=?",
            (fingerprint_tampered["approval_id"],),
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U1")

    assert dashboard["food_list"] == []
    assert dashboard["recorded_count"] == 0
    assert dashboard["extra_cal"] == 0
    assert dashboard["extra_pro"] == 0


def test_meal_photo_final_confirmation_cards_show_exchange_estimate_and_confirm_button():
    draft = {
        "token": "abcdef123456",
        "version": 8,
        "consumed_at": "2026-08-02T12:00:00+08:00",
        "review": {
            "protein_class": "low",
            "protein_exchange": 3,
            "starch_exchange": 1.5,
            "vegetable_exchange": 0.5,
            "milk_exchange": 0,
            "fruit_exchange": 0,
        },
    }
    ready = server.build_meal_photo_review_ready_bubble(draft)
    ready_text = json.dumps(ready, ensure_ascii=False)
    assert "確認加入正式份量" in ready_text
    assert "代換估算" in ready_text
    assert "279 kcal" in ready_text
    assert "蛋白質 24.5g" in ready_text
    assert "NA" not in ready_text

    approved = server.build_meal_photo_approved_bubble(
        draft,
        {
            "approved_exchange": {
                "protein_low_exchange": 3,
                "starch_exchange": 1.5,
                "vegetable_exchange": 0.5,
                "milk_exchange": 0,
                "fruit_exchange": 0,
            },
            "estimated_nutrition": {
                "calories_kcal": 279,
                "protein_g": 24.5,
                "fat_g": 9,
                "carbohydrate_g": 25,
            },
        },
    )
    approved_text = json.dumps(approved, ensure_ascii=False)
    assert "代換估算" in approved_text
    assert "279 kcal" in approved_text
    assert "蛋白質 24.5g" in approved_text
    assert "NA" not in approved_text


def test_forged_meal_photo_review_postback_is_rejected_before_database_access(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_UID", "REAL_ADMIN")
    monkeypatch.setattr(
        server, "get_bound_admin_uid_for_authorization", lambda: "REAL_ADMIN"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("非管理員請求不應進入資料庫審核流程")

    monkeypatch.setattr(server.sqlite3, "connect", forbidden_connect)
    event = SimpleNamespace(
        postback=SimpleNamespace(data="mpr:v1:abcdef123456:8:approve"),
        source=SimpleNamespace(user_id="REGULAR_USER"),
        reply_token="REPLY",
        webhook_event_id="FORGED-MPR-1",
        timestamp=0,
    )

    server.handle_meal_photo_postback(event)

    assert len(replies) == 1
    assert "管理員限定" in replies[0].text



def test_meal_photo_estimate_cards_disclose_low_fat_milk_assumption():
    draft = {
        "token": "abcdef123456",
        "version": 8,
        "consumed_at": "2026-08-02T12:00:00+08:00",
        "review": {
            "protein_class": "none",
            "protein_exchange": 0,
            "starch_exchange": 0,
            "vegetable_exchange": 0,
            "milk_exchange": 1,
            "fruit_exchange": 0,
        },
    }
    ready_text = json.dumps(server.build_meal_photo_review_ready_bubble(draft), ensure_ascii=False)
    approved_text = json.dumps(
        server.build_meal_photo_approved_bubble(
            draft,
            {
                "approved_exchange": {
                    "milk_exchange": 1,
                    "protein_low_exchange": 0,
                    "protein_medium_exchange": 0,
                    "protein_high_exchange": 0,
                    "starch_exchange": 0,
                    "vegetable_exchange": 0,
                    "fruit_exchange": 0,
                    "fat_exchange": 0,
                },
                "estimated_nutrition": {
                    "calories_kcal": 116,
                    "protein_g": 8,
                    "fat_g": 4,
                    "carbohydrate_g": 12,
                    "_warnings": ["milk_assumed_low_fat"],
                },
            },
        ),
        ensure_ascii=False,
    )
    assert "奶類以低脂奶估算" in ready_text
    assert "奶類以低脂奶估算" in approved_text


def test_dashboard_frequent_breakfast_combo_logs_three_foods(tmp_path, monkeypatch):
    db = tmp_path / "combo-from-dashboard.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, exch, kcal in [
            ("穀麥高粱 OATS & HONEY", {"starch_exchange": 1.98}, 197),
            ("草莓穀物脆片", {"starch_exchange": 2.37}, 223),
            ("無糖優格", {"milk_exchange": 0.5}, 62),
        ]:
            fid = new_id("food")
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',100,'g',1,?,'{}',
                           ?,'approved',?,'',0,'user_confirmed',?,?)""",
                (fid, name, json.dumps({"calories_kcal": kcal}), json.dumps(exch), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(
        _text_event("COMBO-DASHBOARD-1", "加入常吃：早餐1", user_id="U1")
    )

    assert len(replies) == 1
    assert replies[0].type == "flex"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U1' AND meal_slot='早餐'"
        ).fetchone()[0] == 3


def test_dashboard_breakfast_combo_retry_after_reply_failure_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "combo-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, kcal in [
            ("穀麥高粱 OATS & HONEY", 197),
            ("草莓穀物脆片", 223),
            ("無糖優格", 62),
        ]:
            fid = new_id("food")
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, name, json.dumps({"calories_kcal": kcal}), fid, now, now),
            )
    attempts = 0

    def flaky_reply(_token, _message):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server.line_bot_api, "reply_message", flaky_reply)
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_message_impl(
            _text_event("COMBO-RETRY-1", "加入常吃：早餐1", user_id="U1")
        )
    server._handle_message_impl(
        _text_event("COMBO-RETRY-1", "加入常吃：早餐1", user_id="U1")
    )

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U1'"
        ).fetchone()[0] == 3


def test_breakfast_combo_missing_item_rolls_back_whole_combo(tmp_path, monkeypatch):
    db = tmp_path / "combo-atomic.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        fid = new_id("food")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                       'approved',?,'',1,'user_confirmed',?,?)""",
            (fid, "穀麥高粱 OATS & HONEY", json.dumps({"calories_kcal": 197}), fid, now, now),
        )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("COMBO-ATOMIC-1", "早餐1", user_id="U1"))

    assert "找不到" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_breakfast_combo2_logs_different_portions(tmp_path, monkeypatch):
    db = tmp_path / "combo2.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, exch, kcal in [
            ("穀麥高粱 OATS & HONEY", {"starch_exchange": 1.98}, 197),
            ("草莓穀物脆片", {"starch_exchange": 2.37}, 223),
            ("無糖優格", {"milk_exchange": 0.5}, 62),
        ]:
            fid = new_id("food")
            ps = json.dumps({"calories_kcal": kcal})
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',
                           100,'g',1,?, '{}',
                           ?,'approved',?,'',0,'user_confirmed',?,?)""",
                (fid, name, ps, json.dumps(exch), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    event = _text_event("COMBO-2", "早餐2", user_id="U1")
    server._handle_message_impl(event)
    assert len(replies) == 1
    reply = replies[0]
    assert reply.type == "flex"
    raw = json.loads(reply.as_json_string())
    bubble_text = json.dumps(raw, ensure_ascii=False)
    assert "已記錄" in bubble_text
    assert "679" in bubble_text or "678" in bubble_text or "677" in bubble_text

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT consumed_servings,meal_slot FROM food_logs WHERE user_id='U1' ORDER BY consumed_servings"
        ).fetchall()
        assert len(rows) == 3
