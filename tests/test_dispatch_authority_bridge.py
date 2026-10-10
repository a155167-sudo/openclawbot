import hashlib
import json
import sqlite3

import pytest

from dispatch_authority_bridge import (
    DispatchAuthorityConflict,
    ensure_dispatch_authority_bridge_schema,
    import_initial_published_version,
    reserve_published_reschedule,
    resolve_trusted_export_rows,
)
from printer_dispatch_claims import (
    PrinterClaimConflict,
    activate_v2_capability,
    claim_current_dispatch,
    ensure_printer_claim_schema,
)
from reschedule_dispatch_versions import confirm_sheet_readback, mark_sheet_unknown
from subscription_dispatch_contract import (
    ORIGINAL_HEADERS,
    RECEIPT_VERSION,
    TRUSTED_WRITER,
    _canonical_json,
    _receipt_hash,
    _source_payload,
    _trusted_publication_rows,
    ensure_dispatch_schema,
)

OWNER = "U" + "1" * 32
NOW = "2026-09-26T10:00:00+08:00"
STORE = "nanjing"
SCOPE = "printer:claim:nanjing"


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""CREATE TABLE subscription_orders(
        id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
        formalized_at TEXT NOT NULL DEFAULT '')""")
    conn.execute("INSERT INTO subscription_orders(id,user_id,status) VALUES(1,?,'activated')", (OWNER,))
    ensure_dispatch_schema(conn)
    conn.commit()
    return conn


def source_row(service_date, lunch, dinner):
    return [service_date, "第1週", lunch, "500", "30", dinner, "600", "35",
            "1100", "65", "", "$220", "", "待列印"]


def insert_published_generation(conn, *, menu_version, dispatch_id, row=None,
                                owner=OWNER, store=STORE, payload_hash=None,
                                with_receipt=True):
    row = row or source_row("2026-09-27", f"午餐-{menu_version}", f"晚餐-{menu_version}")
    payload_json, actual_hash = _source_payload(row)
    stored_hash = actual_hash if payload_hash is None else payload_hash
    published_at = NOW
    conn.execute(
        """INSERT INTO subscription_dispatch_rows
           (dispatch_row_id,order_id,customer_uid,store_id,workbook_id,worksheet_id,
            worksheet_title,service_date,lunch,dinner,menu_version,formalized_at,
            publish_state,created_at,source_payload_json,source_payload_hash)
           VALUES(?,1,?,?, 'book-1',17,'sheet-1',?,?,?,?,?,'published',?,?,?)""",
        (dispatch_id, owner, store, row[0].replace("/", "-"), row[2], row[5],
         menu_version, published_at, published_at, payload_json, stored_hash),
    )
    if with_receipt:
        values = (
            RECEIPT_VERSION, TRUSTED_WRITER, dispatch_id, 1, owner, store, "book-1",
            17, "sheet-1", row[0].replace("/", "-"), menu_version, payload_json,
            stored_hash, published_at,
        )
        receipt_hash = _receipt_hash(values)
        conn.execute(
            """INSERT INTO subscription_dispatch_publication_receipts
               (receipt_id,receipt_version,trusted_writer,dispatch_row_id,order_id,
                customer_uid,store_id,workbook_id,worksheet_id,worksheet_title,
                service_date,menu_version,source_payload_json,source_payload_hash,
                published_at,receipt_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("receipt-" + receipt_hash, *values, receipt_hash),
        )
    conn.commit()


def authority_rows(conn, service_date="2026-09-27"):
    return _trusted_publication_rows(
        conn,
        "r.store_id=? AND r.workbook_id=? AND r.service_date=? ORDER BY r.dispatch_row_id",
        (STORE, "book-1", service_date),
    )


def resolved(conn, service_date="2026-09-27"):
    return resolve_trusted_export_rows(
        conn,
        where_sql="r.store_id=? AND r.workbook_id=? AND r.service_date=? ORDER BY r.dispatch_row_id",
        parameters=(STORE, "book-1", service_date),
    )


def bootstrap(conn, menu="order-1-v1"):
    return import_initial_published_version(
        conn, order_id=1, store_id=STORE, menu_version=menu, now=NOW,
    )


def reserve_new(conn):
    return reserve_published_reschedule(
        conn,
        operation_id="reschedule-1", request_id="request-1",
        old_version_id=bootstrap_version_id(conn),
        order_id=1, requested_by=OWNER, approved_by="persisted-admin",
        source_date="2026-09-27", target_date="2026-09-28",
        store_id=STORE, menu_version="order-1-v2",
        claim_token="reschedule-secret", now=NOW,
    )


def bootstrap_version_id(conn):
    return str(conn.execute(
        "SELECT version_id FROM dispatch_authority_version_bindings WHERE order_id=1 AND menu_version='order-1-v1'"
    ).fetchone()[0])


def cutover(conn):
    ensure_printer_claim_schema(conn)
    activate_v2_capability(
        conn, operation_id="cutover-1", store_id=STORE, admin_scope=SCOPE,
        generation=1, cache_cleared=True, all_consumers_confirmed=True,
        evidence_digest="a" * 64, now=NOW,
    )


def claim(conn, version_id, *, dispatch_row_id, service_date, operation_id="print-1"):
    return claim_current_dispatch(
        conn, operation_id=operation_id, version_id=version_id, order_id=1,
        owner_user_id=OWNER, store_id=STORE, admin_scope=SCOPE,
        capability_generation=1, claim_token="print-secret",
        dispatch_row_id=dispatch_row_id, service_date=service_date,
        lease_expires_at="2026-09-26T10:05:00+08:00", now=NOW,
    )


def test_import_derives_version_only_from_real_persisted_receipt_and_is_immutable(tmp_path):
    conn = open_db(tmp_path / "bridge.sqlite3")
    insert_published_generation(conn, menu_version="order-1-v1", dispatch_id="dispatch-old")

    imported = bootstrap(conn)

    version = conn.execute(
        "SELECT version_id,order_id,owner_user_id,payload_json,payload_hash FROM reschedule_dispatch_versions"
    ).fetchone()
    payload = json.loads(version[3])
    assert imported.version_id == version[0]
    assert tuple(version[1:3]) == (1, OWNER)
    assert payload["store_id"] == STORE
    assert payload["menu_version"] == "order-1-v1"
    assert payload["rows"][0]["dispatch_row_id"] == "dispatch-old"
    assert hashlib.sha256(version[3].encode()).hexdigest() == version[4]
    with pytest.raises(DispatchAuthorityConflict, match="already imported"):
        bootstrap(conn)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE subscription_dispatch_publication_receipts SET published_at='changed'")


@pytest.mark.parametrize("corruption", ["owner", "store", "menu", "forged_hash", "partial_rows"])
def test_import_rejects_mismatch_forgery_partial_rows_without_creating_version(tmp_path, corruption):
    conn = open_db(tmp_path / f"{corruption}.sqlite3")
    if corruption == "partial_rows":
        insert_published_generation(conn, menu_version="order-1-v1", dispatch_id="good")
        insert_published_generation(
            conn, menu_version="order-1-v1", dispatch_id="missing-receipt",
            row=source_row("2026-09-28", "午餐-b", "晚餐-b"), with_receipt=False,
        )
    else:
        insert_published_generation(
            conn, menu_version="order-1-v1", dispatch_id="dispatch-old",
            owner="foreign" if corruption == "owner" else OWNER,
            payload_hash="f" * 64 if corruption == "forged_hash" else None,
        )
    kwargs = {"store_id": "other"} if corruption == "store" else {}
    menu = "missing-menu" if corruption == "menu" else "order-1-v1"
    with pytest.raises(DispatchAuthorityConflict):
        import_initial_published_version(conn, order_id=1, store_id=kwargs.get("store_id", STORE),
                                         menu_version=menu, now=NOW)
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='reschedule_dispatch_versions'"
    ).fetchone()[0] in (0, 1)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='reschedule_dispatch_versions'").fetchone():
        assert conn.execute("SELECT count(*) FROM reschedule_dispatch_versions").fetchone()[0] == 0


def test_legacy_export_is_equivalent_when_bridge_absent_or_only_schema_installed(tmp_path):
    conn = open_db(tmp_path / "legacy.sqlite3")
    insert_published_generation(conn, menu_version="order-1-v1", dispatch_id="dispatch-old")
    before = authority_rows(conn)
    assert [dict(row) for row in resolved(conn)] == [dict(row) for row in before]

    ensure_dispatch_authority_bridge_schema(conn)
    conn.commit()
    assert [dict(row) for row in resolved(conn)] == [dict(row) for row in before]


def test_pending_unknown_and_printer_claims_fence_export(tmp_path):
    conn = open_db(tmp_path / "fences.sqlite3")
    insert_published_generation(conn, menu_version="order-1-v1", dispatch_id="dispatch-old")
    bootstrap(conn)
    insert_published_generation(
        conn, menu_version="order-1-v2", dispatch_id="dispatch-new",
        row=source_row("2026-09-28", "新午餐", "新晚餐"),
    )
    reservation = reserve_new(conn)
    assert resolved(conn) == []
    mark_sheet_unknown(conn, operation_id=reservation.operation_id,
                       claim_token="reschedule-secret", reason="readback_timeout", now=NOW)
    assert resolved(conn) == []

    confirm_sheet_readback(
        conn, operation_id=reservation.operation_id, claim_token="reschedule-secret",
        observed_payload=json.loads(conn.execute(
            "SELECT payload_json FROM reschedule_dispatch_versions WHERE version_id=?",
            (reservation.new_version_id,),
        ).fetchone()[0]), now=NOW,
    )
    cutover(conn)
    assert [row["dispatch_row_id"] for row in resolved(conn, "2026-09-28")] == ["dispatch-new"]
    claim(
        conn, reservation.new_version_id,
        dispatch_row_id="dispatch-new", service_date="2026-09-28",
    )
    assert resolved(conn, "2026-09-28") == []


def test_confirmed_supersession_rejects_old_cached_id_and_requires_cutover(tmp_path):
    conn = open_db(tmp_path / "stale.sqlite3")
    insert_published_generation(conn, menu_version="order-1-v1", dispatch_id="dispatch-old")
    old = bootstrap(conn)
    insert_published_generation(
        conn, menu_version="order-1-v2", dispatch_id="dispatch-new",
        row=source_row("2026-09-28", "新午餐", "新晚餐"),
    )
    reservation = reserve_new(conn)
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM reschedule_dispatch_versions WHERE version_id=?",
        (reservation.new_version_id,),
    ).fetchone()[0])
    confirm_sheet_readback(
        conn, operation_id=reservation.operation_id, claim_token="reschedule-secret",
        observed_payload=payload, now=NOW,
    )

    assert resolved(conn, "2026-09-27") == []
    assert resolved(conn, "2026-09-28") == []
    cutover(conn)
    assert [row["dispatch_row_id"] for row in resolved(conn, "2026-09-28")] == ["dispatch-new"]
    with pytest.raises(PrinterClaimConflict, match="current version"):
        claim(
            conn, old.version_id, dispatch_row_id="dispatch-old",
            service_date="2026-09-27", operation_id="cached-old-id",
        )
    assert claim(
        conn, reservation.new_version_id, dispatch_row_id="dispatch-new",
        service_date="2026-09-28", operation_id="current-id",
    ).version_id == reservation.new_version_id


def test_unbound_synthetic_version_cannot_be_claimed_when_bridge_is_present(tmp_path):
    conn = open_db(tmp_path / "unbound.sqlite3")
    insert_published_generation(conn, menu_version="order-1-v1", dispatch_id="dispatch-old")
    imported = bootstrap(conn)
    cutover(conn)
    # Model offline corruption after bypassing the normal immutable guards.
    conn.execute("DROP TRIGGER dispatch_authority_rows_no_delete")
    conn.execute("DROP TRIGGER dispatch_authority_bindings_no_delete")
    conn.execute("DELETE FROM dispatch_authority_version_rows")
    conn.execute("DELETE FROM dispatch_authority_version_bindings")
    conn.commit()

    with pytest.raises(PrinterClaimConflict, match="dispatch authority"):
        claim(
            conn, imported.version_id, dispatch_row_id="dispatch-old",
            service_date="2026-09-27",
        )
    assert conn.execute("SELECT count(*) FROM printer_dispatch_claims").fetchone()[0] == 0
