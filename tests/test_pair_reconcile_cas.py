"""Recovery CAS regressions with real SQLite connections and sqlite.Row."""
import sqlite3
from datetime import timezone

import pytest

import pair_reschedule_coordinator as coordinator
from pair_reschedule_coordinator import PairRescheduleConflict, reconcile_pair_reschedule_readback
from test_pair_reschedule_coordinator import ADMIN, NOW, AtomicSheetFake, call, old_source_row, open_db


def unknown_case(tmp_path):
    path = tmp_path / "reconcile.sqlite3"
    conn, _ = open_db(path)
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]], "apply_then_timeout")
    assert call(conn, sheet).status == "sheet_unknown"
    assert not conn.in_transaction
    return path, conn, sheet


def reconcile(conn, sheet):
    return reconcile_pair_reschedule_readback(
        conn, sheet, order_id=1, request_id="request-1",
        admin_context=coordinator.VerifiedAdminContext(ADMIN, ADMIN), now=NOW,
    )


def state(conn):
    return (
        conn.execute("SELECT count(*) FROM reschedule_dispatch_confirmations WHERE operation_id=(SELECT operation_id FROM reschedule_dispatch_operations WHERE order_id=1)").fetchone()[0],
        tuple(conn.execute("SELECT status,updated_at FROM reschedule_dispatch_operations WHERE order_id=1").fetchone()),
        tuple(conn.execute("SELECT state,readback_payload_hash,updated_at FROM reschedule_dispatch_sheet_sync").fetchone()),
        tuple(conn.execute("SELECT * FROM workbook_write_leases").fetchone()),
    )


def race(path, sheet, sql, params=()):
    read = sheet.read_pair_rows

    def changed(**kwargs):
        observed = read(**kwargs)
        other = sqlite3.connect(path)
        try:
            other.execute(sql, params)
            other.commit()  # Proves the external read holds no SQLite write lock.
        finally:
            other.close()
        return observed

    sheet.read_pair_rows = changed


def test_admin_revoked_during_external_read_fails_closed(tmp_path):
    path, conn, sheet = unknown_case(tmp_path)
    before = state(conn)
    race(path, sheet, "UPDATE admin_settings SET value='revoked-admin' WHERE key='admin_id'")
    with pytest.raises(PairRescheduleConflict):
        reconcile(conn, sheet)
    assert state(conn) == before
    assert conn.execute("SELECT value FROM admin_settings WHERE key='admin_id'").fetchone()[0] == "revoked-admin"


@pytest.mark.parametrize("sql", [
    "UPDATE workbook_write_leases SET lease_token=(CASE substr(lease_token,1,1) WHEN 'a' THEN 'b' ELSE 'a' END) || substr(lease_token,2)",
    "UPDATE workbook_write_leases SET writer_id='other_writer'",
    "UPDATE workbook_write_leases SET workbook_id='other-book'",
    "UPDATE workbook_write_leases SET operation_id='other-operation'",
    "UPDATE workbook_write_leases SET acquired_at='changed'",
    "UPDATE workbook_write_leases SET expires_at='changed'",
    "UPDATE workbook_write_leases SET status='released',final_outcome='confirmed',released_at='changed'",
    "UPDATE reschedule_dispatch_operations SET status='pending'",
    "UPDATE reschedule_dispatch_operations SET updated_at='changed'",
    "UPDATE reschedule_dispatch_operations SET approved_by='other-admin'",
    "UPDATE reschedule_dispatch_operations SET request_id='other-request'",
    "UPDATE reschedule_dispatch_operations SET owner_user_id='other-owner'",
    "UPDATE subscription_orders SET user_id='other-owner' WHERE id=1",
    "UPDATE subscription_orders SET status='cancelled' WHERE id=1",
])
def test_binding_changed_during_read_fails_closed(tmp_path, sql):
    path, conn, sheet = unknown_case(tmp_path)
    race(path, sheet, sql)
    with pytest.raises(PairRescheduleConflict):
        reconcile(conn, sheet)
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_confirmations WHERE operation_id=(SELECT operation_id FROM reschedule_dispatch_operations WHERE order_id=1)").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM reschedule_dispatch_sheet_sync").fetchone()[0] == "sheet_unknown"


def test_confirmation_release_and_projection_commit_once(tmp_path):
    _path, conn, sheet = unknown_case(tmp_path)
    original = tuple(conn.execute("SELECT * FROM workbook_write_leases").fetchone())
    assert reconcile(conn, sheet).status == "confirmed"
    finished = state(conn)
    assert finished[0] == 1
    assert finished[1][0] == finished[2][0] == "confirmed"
    assert finished[3][:6] == original[:6]
    assert finished[3][6:] == ("released", "confirmed", NOW.astimezone(timezone.utc).isoformat())
    assert reconcile(conn, sheet).status == "confirmed"
    assert state(conn) == finished
    assert len(sheet.batch_calls) == 1


def test_confirmed_active_crash_state_releases_original_lease(tmp_path):
    _path, conn, sheet = unknown_case(tmp_path)
    assert reconcile(conn, sheet).status == "confirmed"
    conn.execute("UPDATE workbook_write_leases SET status='active',final_outcome=NULL,released_at=NULL")
    conn.commit()
    assert reconcile(conn, sheet).status == "confirmed"
    assert state(conn)[3][6:] == ("released", "confirmed", NOW.astimezone(timezone.utc).isoformat())
    assert state(conn)[0] == 1


def test_interruption_after_confirmation_rolls_back_everything(tmp_path, monkeypatch):
    _path, conn, sheet = unknown_case(tmp_path)
    before = state(conn)

    def interrupt(*args, **kwargs):
        assert conn.execute("SELECT count(*) FROM reschedule_dispatch_confirmations").fetchone()[0] == 2
        raise RuntimeError("interrupted before lease release")

    monkeypatch.setattr(coordinator, "release_workbook_lease", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        reconcile(conn, sheet)
    assert not conn.in_transaction
    assert state(conn) == before
