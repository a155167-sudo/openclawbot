"""The caller-owned unknown transition must reject incomplete CAS updates."""
import sqlite3

import pytest

from reschedule_dispatch_versions import RescheduleConflict, mark_sheet_unknown
from test_pair_reschedule_coordinator import AtomicSheetFake, call, old_source_row, open_db


@pytest.mark.parametrize('change', [
    'DELETE FROM reschedule_dispatch_sheet_sync',
    "UPDATE reschedule_dispatch_sheet_sync SET state='confirmed'",
])
def test_missing_or_stale_sync_row_rolls_back_caller_transaction(tmp_path, change):
    conn, _ = open_db(tmp_path / 'unknown-cas.sqlite3')
    sheet = AtomicSheetFake([old_source_row() + ['dispatch-old', '1', 'order-1-v1']], 'apply_then_timeout')
    assert call(conn, sheet).status == 'sheet_unknown'
    conn.execute(change)
    conn.commit()
    before = tuple(conn.iterdump())
    operation_id, token = conn.execute('SELECT operation_id,claim_token FROM reschedule_dispatch_operations').fetchone()
    conn.execute('BEGIN IMMEDIATE')
    with pytest.raises(RescheduleConflict):
        mark_sheet_unknown(conn, operation_id=operation_id, claim_token=token,
                           reason='readback_mismatch', now='2026-09-27T12:00:00+08:00', caller_owned=True)
    conn.rollback()
    assert tuple(conn.iterdump()) == before


def test_zero_row_operation_update_does_not_commit_caller_transaction(tmp_path):
    conn, _ = open_db(tmp_path / 'unknown-cas.sqlite3')
    sheet = AtomicSheetFake([old_source_row() + ['dispatch-old', '1', 'order-1-v1']], 'apply_then_timeout')
    assert call(conn, sheet).status == 'sheet_unknown'
    operation_id, token = conn.execute('SELECT operation_id,claim_token FROM reschedule_dispatch_operations').fetchone()
    conn.execute('''CREATE TRIGGER suppress_unknown_operation BEFORE UPDATE OF status
        ON reschedule_dispatch_operations WHEN NEW.status='sheet_unknown'
        BEGIN SELECT RAISE(IGNORE); END''')
    conn.commit()
    before = tuple(conn.iterdump())
    conn.execute('BEGIN IMMEDIATE')
    with pytest.raises(RescheduleConflict):
        mark_sheet_unknown(conn, operation_id=operation_id, claim_token=token,
                           reason='readback_mismatch', now='2026-09-27T12:00:00+08:00', caller_owned=True)
    conn.rollback()
    assert tuple(conn.iterdump()) == before
