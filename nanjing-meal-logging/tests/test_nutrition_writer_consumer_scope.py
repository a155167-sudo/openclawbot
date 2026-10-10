import sqlite3
from types import SimpleNamespace

import pytest

import server
from nutrition_system import ensure_nutrition_schema
from workbook_write_lease import (
    FULL_REQUIRED_WRITER_INVENTORY,
    configure_workbook_writer_capability,
    ensure_workbook_lease_schema,
)

WORKBOOK = "offline-nutrition-workbook"


def _enabled_db(tmp_path):
    db = tmp_path / "nutrition-consumer.sqlite3"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        ensure_workbook_lease_schema(conn)
        configure_workbook_writer_capability(
            conn,
            workbook_id=WORKBOOK,
            controlled_writers=FULL_REQUIRED_WRITER_INVENTORY,
            external_writer_state="controlled",
            cutover_evidence="offline-test-only",
        )
        conn.commit()
    return db


def test_registered_target_reader_is_read_only_and_not_blocked_by_pair_fence(monkeypatch):
    writes = []

    class TargetSheet:
        def get_all_records(self):
            return [{
                "plan_id": "p1", "User_ID": "U1", "狀態": "active",
                "星期": "週二", "餐別": "全日", "版本": 1,
                "生效日期": "2026-01-01", "結束日期": "",
                "熱量目標": 1800, "主食份": 6,
            }]
        def row_values(self, *_args):
            writes.append("header-read")
            raise AssertionError("target reader must not inspect/extend schema")
        def update(self, **_kwargs):
            writes.append("update")
        def append_row(self, *_args, **_kwargs):
            writes.append("append")

    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "sh", SimpleNamespace(worksheet=lambda _title: TargetSheet()))

    result = server.get_daily_nutrition_target("U1", "2026-07-21")

    assert result["calories_kcal"] == 1800
    assert result["starch_exchange"] == 6
    assert writes == []


def test_registered_outbox_consumer_owns_scope_through_local_confirmation(tmp_path, monkeypatch):
    db = _enabled_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO nutrition_sheet_outbox "
            "(outbox_id,entity_type,entity_id,status,created_at) "
            "VALUES ('o1','food','f1','pending','2026-01-01')"
        )
        conn.commit()

    events = []
    expected = server.nutrition_sheet_specs()["食品資料庫"]["headers"]

    class Sheet:
        def row_values(self, row):
            assert row == 1
            return expected
        def find(self, entity_id, **_kwargs):
            events.append(("provider-read", entity_id))
            return None
        def append_row(self, row, **_kwargs):
            events.append(("provider-write", row[0]))

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", WORKBOOK)
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "sh", SimpleNamespace(worksheet=lambda _title: Sheet()))

    def registered_food_consumer(entity_id):
        ws = server._nutrition_ws("食品資料庫", writable=True)
        server._upsert_raw_sheet_row(ws, entity_id, [entity_id])

    monkeypatch.setattr(server, "_sync_food_outbox", registered_food_consumer)
    assert server.flush_nutrition_sheet_outbox() == 1

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM nutrition_sheet_outbox WHERE outbox_id='o1'"
        ).fetchone() == ("synced",)
        assert conn.execute(
            "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id=?",
            (WORKBOOK,),
        ).fetchone() == ("released", "confirmed")
    assert events == [("provider-read", "f1"), ("provider-write", "f1")]


def test_unknown_provider_result_keeps_outbox_and_workbook_lease_active(tmp_path, monkeypatch):
    db = _enabled_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO nutrition_sheet_outbox "
            "(outbox_id,entity_type,entity_id,status,created_at) "
            "VALUES ('o-unknown','food','f-unknown','pending','2026-01-01')"
        )
        conn.commit()

    expected = server.nutrition_sheet_specs()["食品資料庫"]["headers"]

    class Sheet:
        def row_values(self, _row):
            return expected
        def find(self, *_args, **_kwargs):
            return None
        def append_row(self, *_args, **_kwargs):
            raise TimeoutError("provider response lost")

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", WORKBOOK)
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "sh", SimpleNamespace(worksheet=lambda _title: Sheet()))

    def registered_food_consumer(entity_id):
        ws = server._nutrition_ws("食品資料庫", writable=True)
        server._upsert_raw_sheet_row(ws, entity_id, [entity_id])

    monkeypatch.setattr(server, "_sync_food_outbox", registered_food_consumer)
    assert server.flush_nutrition_sheet_outbox() == 0

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status,lease_owner FROM nutrition_sheet_outbox WHERE outbox_id='o-unknown'"
        ).fetchone()[0] == "processing"
        assert conn.execute(
            "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id=?",
            (WORKBOOK,),
        ).fetchone() == ("active", None)

    # Even after the ordinary outbox timeout, an active workbook lease proves
    # the provider outcome is unresolved and must prevent reclaim/re-send.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE nutrition_sheet_outbox SET claimed_at='2000-01-01T00:00:00+08:00' "
            "WHERE outbox_id='o-unknown'"
        )
        conn.commit()
    assert server.flush_nutrition_sheet_outbox() == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM nutrition_sheet_outbox WHERE outbox_id='o-unknown'"
        ).fetchone() == ("processing",)
