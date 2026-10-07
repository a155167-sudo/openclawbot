"""Crash recovery and fail-closed lease checks for pair readback reconciliation."""
import sqlite3

import pytest

import pair_reschedule_coordinator as coordinator
from test_pair_reschedule_coordinator import (
    ADMIN, NOW, OWNER, AtomicSheetFake, call, old_source_row, open_db,
)


def _unknown(tmp_path):
    conn, _ = open_db(tmp_path / "reconcile.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]], "apply_then_timeout")
    assert call(conn, sheet).status == "sheet_unknown"
    return conn, sheet


def _reconcile(conn, sheet, **changes):
    arguments = dict(order_id=1, request_id="request-1",
                     admin_context=coordinator.verify_admin_context(conn, ADMIN), now=NOW)
    arguments.update(changes)
    return coordinator.reconcile_pair_reschedule_readback(conn, sheet, **arguments)


def _state(conn):
    return tuple(conn.execute("SELECT status FROM reschedule_dispatch_operations").fetchone()) + tuple(
        conn.execute("SELECT status,final_outcome FROM workbook_write_leases").fetchone()
    ) + (conn.execute("SELECT count(*) FROM reschedule_dispatch_confirmations WHERE operation_id IS NOT NULL").fetchone()[0],)


def test_confirmation_then_release_crash_rolls_back_and_retries_original_lease(tmp_path, monkeypatch):
    conn, sheet = _unknown(tmp_path)
    before_dump = tuple(conn.iterdump())
    before = tuple(conn.execute("SELECT workbook_id,writer_id,operation_id,lease_token FROM workbook_write_leases").fetchone())
    real_release = coordinator.release_workbook_lease

    def crashed_release(*args, **kwargs):
        assert conn.in_transaction
        assert _state(conn) == ("confirmed", "active", None, 1)
        raise RuntimeError("simulated process loss before release")

    monkeypatch.setattr(coordinator, "release_workbook_lease", crashed_release)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        _reconcile(conn, sheet)
    assert _state(conn) == ("sheet_unknown", "active", None, 0)
    assert tuple(conn.iterdump()) == before_dump
    conn.close()
    conn = sqlite3.connect(tmp_path / "reconcile.sqlite3")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(coordinator, "release_workbook_lease", real_release)
    assert _reconcile(conn, sheet).status == "confirmed"
    assert _state(conn) == ("confirmed", "released", "confirmed", 1)
    assert tuple(conn.execute("SELECT workbook_id,writer_id,operation_id,lease_token FROM workbook_write_leases").fetchone()) == before
    assert _reconcile(conn, sheet).status == "confirmed"
    assert _state(conn) == ("confirmed", "released", "confirmed", 1)
    assert call(conn, sheet).status == "confirmed"  # same durable request replay
    assert len(sheet.batch_calls) == 1


def test_legacy_already_committed_confirmation_completes_original_lease(tmp_path):
    conn, sheet = _unknown(tmp_path)
    lease_before = tuple(conn.execute("SELECT workbook_id,writer_id,operation_id,lease_token FROM workbook_write_leases").fetchone())
    operation_id = lease_before[2]
    # Seed the prior release's durable crash phase through its real helper.
    coordinator._confirm_from_snapshot(conn, operation_id=operation_id, now=NOW)
    assert _state(conn) == ("confirmed", "active", None, 1)
    conn.close()
    conn = sqlite3.connect(tmp_path / "reconcile.sqlite3")
    conn.row_factory = sqlite3.Row
    assert _reconcile(conn, sheet).status == "confirmed"
    assert _state(conn) == ("confirmed", "released", "confirmed", 1)
    assert tuple(conn.execute("SELECT workbook_id,writer_id,operation_id,lease_token FROM workbook_write_leases").fetchone()) == lease_before
    assert _reconcile(conn, sheet).status == "confirmed"
    assert _state(conn) == ("confirmed", "released", "confirmed", 1)
    assert len(sheet.batch_calls) == 1


@pytest.mark.parametrize("column,value", [
    ("writer_id", "other_writer"), ("workbook_id", "other_book"),
    ("operation_id", "other_operation"), ("lease_token", "other_token"),
])
def test_unknown_rejects_foreign_lease_before_confirmation(tmp_path, column, value):
    conn, sheet = _unknown(tmp_path)
    conn.execute(f"UPDATE workbook_write_leases SET {column}=?", (value,))
    conn.commit()
    with pytest.raises(coordinator.PairRescheduleConflict):
        _reconcile(conn, sheet)
    assert _state(conn) == ("sheet_unknown", "active", None, 0)


@pytest.mark.parametrize("lease_change", ["released", "missing"])
def test_unknown_requires_active_lease_before_confirmation(tmp_path, lease_change):
    conn, sheet = _unknown(tmp_path)
    if lease_change == "released":
        conn.execute("UPDATE workbook_write_leases SET status='released',final_outcome='confirmed'")
    else:
        conn.execute("DELETE FROM workbook_write_leases")
    conn.commit()
    with pytest.raises(coordinator.PairRescheduleConflict):
        _reconcile(conn, sheet)
    assert conn.execute("SELECT status FROM reschedule_dispatch_operations").fetchone()[0] == "sheet_unknown"
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_confirmations WHERE operation_id IS NOT NULL").fetchone()[0] == 0


def test_wrong_admin_and_owner_fail_closed(tmp_path):
    conn, sheet = _unknown(tmp_path)
    with pytest.raises(coordinator.PairRescheduleConflict):
        _reconcile(conn, sheet, admin_context=coordinator.VerifiedAdminContext("wrong", "wrong"))
    conn.execute("UPDATE reschedule_dispatch_operations SET owner_user_id='wrong'")
    conn.commit()
    with pytest.raises(coordinator.PairRescheduleConflict):
        _reconcile(conn, sheet)
    assert _state(conn) == ("sheet_unknown", "active", None, 0)


@pytest.mark.parametrize("mutation", [
    "UPDATE reschedule_dispatch_operations SET expected_payload_hash='f' || substr(expected_payload_hash,2)",
    "UPDATE reschedule_dispatch_sheet_sync SET expected_payload_hash='f' || substr(expected_payload_hash,2)",
])
def test_unknown_rejects_mismatched_payload_before_confirmation(tmp_path, mutation):
    conn, sheet = _unknown(tmp_path)
    conn.execute(mutation)
    conn.commit()
    with pytest.raises(coordinator.PairRescheduleConflict):
        _reconcile(conn, sheet)
    assert conn.execute("SELECT status FROM reschedule_dispatch_operations").fetchone()[0] == "sheet_unknown"
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_confirmations WHERE operation_id IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", [
    "UPDATE reschedule_dispatch_confirmations SET payload_hash='f' || substr(payload_hash,2) WHERE operation_id IS NOT NULL",
    "UPDATE reschedule_dispatch_sheet_sync SET readback_payload_hash='f' || substr(readback_payload_hash,2)",
    "DELETE FROM workbook_write_leases",
    "UPDATE workbook_write_leases SET writer_id='foreign'",
])
def test_confirmed_replay_rejects_broken_confirmation_sync_or_lease(tmp_path, mutation):
    conn, sheet = _unknown(tmp_path)
    assert _reconcile(conn, sheet).status == "confirmed"
    if mutation.startswith("UPDATE reschedule_dispatch_confirmations"):
        # Simulate corruption outside the immutable-table guard, then reinstall it.
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='reschedule_dispatch_confirmations_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER reschedule_dispatch_confirmations_no_update")
    conn.execute(mutation)
    if mutation.startswith("UPDATE reschedule_dispatch_confirmations"):
        conn.execute(trigger_sql)
    conn.commit()
    with pytest.raises(coordinator.PairRescheduleConflict):
        _reconcile(conn, sheet)


def test_confirmed_replay_requires_fresh_exact_two_view_readback(tmp_path):
    conn, sheet = _unknown(tmp_path)
    assert _reconcile(conn, sheet).status == "confirmed"
    sheet.rows["2026-09-28"][2] = "unexpected meal"
    with pytest.raises(coordinator.PairRescheduleConflict, match="fresh readback"):
        _reconcile(conn, sheet)
    assert _state(conn) == ("confirmed", "released", "confirmed", 1)
