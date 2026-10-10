"""Operators may release only expired, unknown-outcome formalization leases."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from workbook_write_lease import (
    WorkbookLeaseConflict,
    acquire_workbook_lease,
    configure_workbook_writer_capability,
    describe_workbook_lease,
    ensure_workbook_lease_schema,
    FULL_REQUIRED_WRITER_INVENTORY,
    operator_release_unknown_lease,
)

WB = "synthetic-workbook"
T0 = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


def _db(tmp_path, writer="subscription_formalization", op="subscription-formalization:5"):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    ensure_workbook_lease_schema(conn)
    configure_workbook_writer_capability(
        conn, workbook_id=WB, controlled_writers=sorted(FULL_REQUIRED_WRITER_INVENTORY),
        external_writer_state="controlled", cutover_evidence="test",
    )
    conn.commit()
    acquire_workbook_lease(conn, workbook_id=WB, writer_id=writer, operation_id=op, now=T0, ttl_seconds=300)
    return conn


def test_expired_formalization_lease_is_released_and_audited(tmp_path):
    conn = _db(tmp_path)
    later = T0 + timedelta(hours=1)
    assert describe_workbook_lease(conn, workbook_id=WB, now=later)["expired"] is True
    operator_release_unknown_lease(
        conn, workbook_id=WB, operation_id="subscription-formalization:5",
        operator_id="Uadmin", evidence="sheet checked", now=later,
    )
    info = describe_workbook_lease(conn, workbook_id=WB, now=later)
    assert info["status"] == "released" and info["final_outcome"] == "operator_verified"
    assert conn.execute("SELECT operator_id FROM workbook_lease_operator_releases").fetchall() == [("Uadmin",)]
    # A new writer can acquire the workbook again.
    acquire_workbook_lease(conn, workbook_id=WB, writer_id="subscription_formalization",
                           operation_id="subscription-formalization:6", now=later, ttl_seconds=300)


def test_unexpired_lease_is_refused(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(WorkbookLeaseConflict, match="not expired"):
        operator_release_unknown_lease(
            conn, workbook_id=WB, operation_id="subscription-formalization:5",
            operator_id="Uadmin", evidence="x", now=T0 + timedelta(seconds=10),
        )


def test_wrong_operation_id_is_refused(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(WorkbookLeaseConflict, match="does not match"):
        operator_release_unknown_lease(
            conn, workbook_id=WB, operation_id="subscription-formalization:4",
            operator_id="Uadmin", evidence="x", now=T0 + timedelta(hours=1),
        )


def test_reschedule_leases_are_never_operator_releasable(tmp_path):
    writer = next(w for w in sorted(FULL_REQUIRED_WRITER_INVENTORY) if w != "subscription_formalization")
    conn = _db(tmp_path, writer=writer, op="pair-op")
    assert describe_workbook_lease(conn, workbook_id=WB, now=T0 + timedelta(hours=1))["operator_releasable"] is False
    with pytest.raises(WorkbookLeaseConflict, match="cannot be released"):
        operator_release_unknown_lease(
            conn, workbook_id=WB, operation_id="pair-op",
            operator_id="Uadmin", evidence="x", now=T0 + timedelta(hours=1),
        )
