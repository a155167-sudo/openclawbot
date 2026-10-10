"""Offline checks for the operator-confirmed, empty-inventory staging cutover."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

import pytest

from reschedule_dispatch_versions import ensure_reschedule_dispatch_schema
from staging_no_device_cutover import CutoverRejected, apply_plan, build_plan


PROJECT_ID = "1220dbe3-b2ea-4119-b21b-0ccdb86a4286"
SERVICE_ID = "a7fa088c-1fe6-4aa9-b9d7-1b5858691bcd"
CONFIRMATION = "沒有，只有我用 LINE 測試，不接出單設備"
GUARD_SHA256 = "fd68deef4335d4645f18356d8311d5844b0ae7be4713de9735f10434b86c0a7a"


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    ensure_reschedule_dispatch_schema(db)
    db.execute("CREATE TABLE dispatch_authority_version_bindings (store_id TEXT NOT NULL)")
    db.execute("INSERT INTO dispatch_authority_version_bindings VALUES ('nanjing')")
    db.commit()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def runtime():
    return {
        "app_env": "staging",
        "project_id": PROJECT_ID,
        "service_id": SERVICE_ID,
        "workbook_id": "offline-workbook",
        "store_id": "nanjing",
    }


@pytest.fixture
def evidence(runtime):
    return {
        "kind": "operator_confirmed_empty_consumer_inventory",
        "confirmed": True,
        "consumers": [],
        "cache_inventory": [],
        "confirmation_text": CONFIRMATION,
        "project_id": runtime["project_id"],
        "service_id": runtime["service_id"],
        "workbook_id": runtime["workbook_id"],
        "store_id": runtime["store_id"],
        "operation_id": "offline-cutover-1",
        "confirmed_at": "2026-09-30T10:00:00+08:00",
    }


def _schema(conn):
    return conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def test_plan_is_read_only(conn, runtime, evidence):
    before = (_schema(conn), conn.total_changes)
    plan = build_plan(conn, runtime=runtime, evidence=evidence)
    assert (_schema(conn), conn.total_changes) == before
    assert plan["generation"] == 1
    assert plan["consumer_count"] == 0
    assert plan["physical_device_verified"] is False
    assert plan["basis"] == "operator-confirmed-empty-inventory"
    assert "printer_dispatch_capability_events" not in {r[1] for r in _schema(conn)}


@pytest.mark.parametrize("change", [
    {"confirmed": False}, {"confirmed": "true"}, {"consumers": ["printer"]},
    {"cache_inventory": ["cache"]}, {"cache_inventory": None},
    {"confirmation_text": ""}, {"confirmed_at": "2026-09-30T10:00:00"},
])
def test_invalid_evidence_rejected_without_writes(conn, runtime, evidence, change):
    evidence.update(change)
    before = (_schema(conn), conn.total_changes)
    with pytest.raises(CutoverRejected):
        apply_plan(conn, runtime=runtime, evidence=evidence, now="2026-09-30T10:01:00+08:00")
    assert (_schema(conn), conn.total_changes) == before


def test_missing_confirmation_rejected_without_writes(conn, runtime, evidence):
    del evidence["confirmed"]
    before = (_schema(conn), conn.total_changes)
    with pytest.raises(CutoverRejected):
        build_plan(conn, runtime=runtime, evidence=evidence)
    assert (_schema(conn), conn.total_changes) == before


@pytest.mark.parametrize("field,value", [
    ("app_env", "production"), ("project_id", "wrong"),
    ("service_id", "wrong"), ("workbook_id", "wrong"),
    ("store_id", "wrong"), ("workbook_id", ""),
])
def test_bad_runtime_rejected_without_writes(conn, runtime, evidence, field, value):
    runtime[field] = value
    before = (_schema(conn), conn.total_changes)
    with pytest.raises(CutoverRejected):
        apply_plan(conn, runtime=runtime, evidence=evidence, now="2026-09-30T10:01:00+08:00")
    assert (_schema(conn), conn.total_changes) == before


def test_authority_and_open_operation_required(conn, runtime, evidence):
    conn.execute("DELETE FROM dispatch_authority_version_bindings")
    conn.commit()
    with pytest.raises(CutoverRejected):
        build_plan(conn, runtime=runtime, evidence=evidence)
    conn.execute("INSERT INTO dispatch_authority_version_bindings VALUES ('nanjing'),('other')")
    conn.commit()
    with pytest.raises(CutoverRejected):
        build_plan(conn, runtime=runtime, evidence=evidence)
    conn.execute("DELETE FROM dispatch_authority_version_bindings WHERE store_id='other'")
    conn.commit()
    conn.execute(
        """INSERT INTO reschedule_dispatch_operations
        (operation_id,request_id,order_id,owner_user_id,old_version_id,new_version_id,
         requested_by,approved_by,source_date,target_date,expected_payload_hash,
         claim_token,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("pending-op", "request", 1, "owner", "old", "new", "requester", "approver",
         "2026-09-30", "2026-10-01", "a" * 64, "token", "pending",
         "2026-09-30T10:00:00+08:00", "2026-09-30T10:00:00+08:00"),
    )
    conn.commit()
    with pytest.raises(CutoverRejected):
        build_plan(conn, runtime=runtime, evidence=evidence)


def test_apply_and_exact_replay(conn, runtime, evidence):
    now = "2026-09-30T10:01:00+08:00"
    first = apply_plan(conn, runtime=runtime, evidence=evidence, now=now)
    assert first["generation"] == 1
    assert first["consumer_count"] == 0
    assert first["physical_device_verified"] is False
    assert first["basis"] == "operator-confirmed-empty-inventory"
    row = conn.execute(
        """SELECT operation_id,store_id,admin_scope,state,generation,cache_cleared,
                  all_consumers_confirmed,evidence_digest
           FROM printer_dispatch_capability_events"""
    ).fetchone()
    assert row[:7] == ("offline-cutover-1", "nanjing", "staging-only/no-device",
                       "v2_active", 1, 1, 1)
    assert row[7] == first["evidence_digest"]
    before = (_schema(conn), conn.total_changes)
    replay = apply_plan(conn, runtime=runtime, evidence=evidence,
                        now="2026-09-30T10:02:00+08:00")
    assert replay["generation"] == first["generation"]
    assert replay["replayed"] is True
    assert (_schema(conn), conn.total_changes) == before
    changed = deepcopy(evidence)
    changed["confirmed_at"] = "2026-09-30T09:00:00+08:00"
    with pytest.raises(CutoverRejected):
        apply_plan(conn, runtime=runtime, evidence=changed, now=now)
    assert (_schema(conn), conn.total_changes) == before


def test_guard_source_is_unchanged():
    source = Path(__file__).resolve().parents[1] / "printer_dispatch_claims.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == GUARD_SHA256
