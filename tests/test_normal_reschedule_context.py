import sqlite3
from datetime import date

from normal_reschedule_context import confirmed_reschedule_menu_context
from reschedule_dispatch_versions import (
    confirm_sheet_readback, create_initial_version, ensure_reschedule_dispatch_schema,
    mark_sheet_unknown, reserve_reschedule, exportable_versions,
)


OWNER = "U" + "1" * 32
NOW = "2026-09-30T10:00:00+08:00"
OLD = "2026-10-23"
NEW = "2026-10-30"


def _row(day, lunch, dinner):
    values = [day, "第1週", lunch, "500", "30", dinner, "600", "35"]
    return values + [""] * 9


def test_normal_context_waits_for_confirmed_readback_and_uses_current_pair(tmp_path):
    conn = sqlite3.connect(tmp_path / "version.db")
    conn.row_factory = sqlite3.Row
    ensure_reschedule_dispatch_schema(conn)
    create_initial_version(conn, version_id="old", order_id=4, owner_user_id=OWNER,
                           payload={"rows": []}, confirmed_at=NOW)
    conn.commit()
    read = lambda: confirmed_reschedule_menu_context(
        conn, order_id=4, owner_user_id=OWNER, today=date(2026, 9, 30))
    assert read() is None
    payload = {"authority": "pair_reschedule_snapshot/v1", "order_id": 4,
               "owner_user_id": OWNER, "rows": [
                   {"service_date": OLD, "columns": _row(OLD, "", "")},
                   {"service_date": NEW, "columns": _row(NEW, "午餐A", "晚餐B")},
               ]}
    reserve_reschedule(
        conn, operation_id="op", request_id="req", old_version_id="old",
        new_version_id="new", order_id=4, owner_user_id=OWNER,
        requested_by=OWNER, approved_by="admin", source_date=OLD,
        target_date=NEW, new_payload=payload, claim_token="claim", now=NOW,
    )
    assert read()["source_dates"] == []
    mark_sheet_unknown(conn, operation_id="op", claim_token="claim",
                       reason="readback unavailable", now=NOW)
    assert read()["source_dates"] == []
    confirm_sheet_readback(conn, operation_id="op", claim_token="claim",
                           observed_payload=payload, now=NOW)
    result = read()
    assert [item["date"] for item in result["source_dates"]] == [NEW]
    assert result["source_dates"][0]["meals"] == [
        {"slot": "午餐", "name": "午餐A"}, {"slot": "晚餐", "name": "晚餐B"}]
    assert result["occupied_dates"] == [NEW]
    assert confirmed_reschedule_menu_context(
        conn, order_id=4, owner_user_id="other", today=date(2026, 9, 30)
    )["source_dates"] == []


def test_rejected_operation_keeps_exportable_initial_menu_visible(tmp_path):
    conn = sqlite3.connect(tmp_path / "rejected.db")
    conn.row_factory = sqlite3.Row
    ensure_reschedule_dispatch_schema(conn)
    initial_payload = {"authority": "pair_reschedule_snapshot/v1", "order_id": 4,
                       "owner_user_id": OWNER, "rows": [
                           {"service_date": OLD, "columns": _row(OLD, "原午餐", "原晚餐")},
                       ]}
    create_initial_version(conn, version_id="initial", order_id=4,
                           owner_user_id=OWNER, payload=initial_payload, confirmed_at=NOW)
    conn.commit()
    reserve_reschedule(
        conn, operation_id="rejected-op", request_id="rejected-req",
        old_version_id="initial", new_version_id="candidate", order_id=4,
        owner_user_id=OWNER, requested_by=OWNER, approved_by="admin",
        source_date=OLD, target_date=NEW, new_payload={"rows": []},
        claim_token="claim", now=NOW,
    )
    # Model the persisted terminal failure: rejected has no confirmation or
    # effective supersession, so the version module exports the initial row.
    conn.execute("UPDATE reschedule_dispatch_operations SET status='rejected' WHERE operation_id='rejected-op'")
    conn.commit()
    assert [row["version_id"] for row in exportable_versions(conn, order_id=4)] == ["initial"]
    context = confirmed_reschedule_menu_context(
        conn, order_id=4, owner_user_id=OWNER, today=date(2026, 9, 30))
    assert [item["date"] for item in context["source_dates"]] == [OLD]
    assert context["occupied_dates"] == [OLD]
