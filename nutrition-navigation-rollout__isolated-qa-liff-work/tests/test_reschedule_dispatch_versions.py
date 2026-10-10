import json
import sqlite3

import pytest

from reschedule_dispatch_versions import (
    RescheduleConflict,
    confirm_sheet_readback,
    create_initial_version,
    ensure_reschedule_dispatch_schema,
    exportable_versions,
    mark_sheet_unknown,
    reserve_reschedule,
)


OWNER = "U" + "1" * 32
NOW = "2026-09-26T10:00:00+08:00"
OLD_PAYLOAD = {
    "rows": [
        {"service_date": "2026-10-23", "lunch": "午餐 A", "dinner": "晚餐 A"},
    ]
}
NEW_PAYLOAD = {
    "rows": [
        {"service_date": "2026-09-26", "lunch": "午餐 A", "dinner": "晚餐 A"},
    ]
}


def open_db(path):
    conn = sqlite3.connect(path, timeout=0.2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def seed(path):
    conn = open_db(path)
    ensure_reschedule_dispatch_schema(conn)
    create_initial_version(
        conn,
        version_id="version-old",
        order_id=4,
        owner_user_id=OWNER,
        payload=OLD_PAYLOAD,
        confirmed_at=NOW,
    )
    conn.commit()
    return conn


def reserve(conn, **overrides):
    values = {
        "operation_id": "operation-1",
        "request_id": "request-1",
        "old_version_id": "version-old",
        "new_version_id": "version-new",
        "order_id": 4,
        "owner_user_id": OWNER,
        "requested_by": OWNER,
        "approved_by": "persisted-admin-uid",
        "source_date": "2026-10-23",
        "target_date": "2026-09-26",
        "new_payload": NEW_PAYLOAD,
        "claim_token": "claim-secret-1",
        "now": NOW,
    }
    values.update(overrides)
    return reserve_reschedule(conn, **values)


def test_reservation_fences_old_and_new_until_exact_sheet_confirmation(tmp_path):
    conn = seed(tmp_path / "dispatch.sqlite3")
    assert [row["version_id"] for row in exportable_versions(conn, order_id=4)] == ["version-old"]

    reservation = reserve(conn)
    conn.commit()

    assert reservation.status == "pending"
    assert exportable_versions(conn, order_id=4) == []
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_versions").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_supersessions").fetchone()[0] == 1

    mark_sheet_unknown(
        conn,
        operation_id="operation-1",
        claim_token="claim-secret-1",
        reason="apply_then_timeout",
        now=NOW,
    )
    conn.commit()
    assert exportable_versions(conn, order_id=4) == []

    with pytest.raises(RescheduleConflict, match="claim"):
        confirm_sheet_readback(
            conn,
            operation_id="operation-1",
            claim_token="wrong-claim",
            observed_payload=NEW_PAYLOAD,
            now=NOW,
        )
    assert exportable_versions(conn, order_id=4) == []

    confirmed = confirm_sheet_readback(
        conn,
        operation_id="operation-1",
        claim_token="claim-secret-1",
        observed_payload=NEW_PAYLOAD,
        now=NOW,
    )
    conn.commit()

    assert confirmed.status == "confirmed"
    rows = exportable_versions(conn, order_id=4)
    assert [row["version_id"] for row in rows] == ["version-new"]
    assert json.loads(rows[0]["payload_json"]) == NEW_PAYLOAD
    assert conn.execute(
        "SELECT count(*) FROM reschedule_dispatch_versions WHERE version_id='version-old'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM reschedule_dispatch_confirmations"
    ).fetchone()[0] == 2


@pytest.mark.parametrize("statement", [
    "UPDATE reschedule_dispatch_versions SET payload_json='{}' WHERE version_id='version-old'",
    "DELETE FROM reschedule_dispatch_versions WHERE version_id='version-old'",
    "UPDATE reschedule_dispatch_supersessions SET old_version_id='other' WHERE operation_id='operation-1'",
    "DELETE FROM reschedule_dispatch_supersessions WHERE operation_id='operation-1'",
])
def test_versions_and_supersession_evidence_are_immutable(tmp_path, statement):
    conn = seed(tmp_path / "dispatch.sqlite3")
    reserve(conn)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(statement)


def test_customer_and_admin_bindings_and_full_year_dates_fail_closed(tmp_path):
    conn = seed(tmp_path / "dispatch.sqlite3")

    with pytest.raises(RescheduleConflict):
        reserve(conn, requested_by="foreign-user")
    with pytest.raises(RescheduleConflict):
        reserve(conn, approved_by="")
    with pytest.raises(RescheduleConflict):
        reserve(conn, source_date="10/23")

    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 0
    assert [row["version_id"] for row in exportable_versions(conn, order_id=4)] == ["version-old"]


def test_two_real_connections_cannot_reserve_sibling_operations_for_one_order(tmp_path):
    path = tmp_path / "dispatch.sqlite3"
    first = seed(path)
    second = open_db(path)

    reserve(first)
    first.commit()

    with pytest.raises(RescheduleConflict, match="active reschedule"):
        reserve(
            second,
            operation_id="operation-2",
            request_id="request-2",
            new_version_id="version-new-2",
            claim_token="claim-secret-2",
        )
    second.rollback()

    assert exportable_versions(first, order_id=4) == []
    assert first.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 1


def test_repeat_schema_check_rejects_weakened_trigger_before_mutation(tmp_path):
    conn = seed(tmp_path / "dispatch.sqlite3")
    conn.execute("DROP TRIGGER reschedule_dispatch_versions_no_update")
    conn.execute("""CREATE TRIGGER reschedule_dispatch_versions_no_update
        BEFORE UPDATE ON reschedule_dispatch_versions BEGIN SELECT 1; END""")
    conn.commit()
    before = "\n".join(conn.iterdump())

    with pytest.raises(RescheduleConflict, match="schema mismatch"):
        ensure_reschedule_dispatch_schema(conn)

    assert "\n".join(conn.iterdump()) == before
