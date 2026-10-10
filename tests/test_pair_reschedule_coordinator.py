import copy
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dispatch_authority_bridge import (
    import_initial_published_version,
    resolve_trusted_export_rows,
)
from pair_reschedule_coordinator import (
    PairRescheduleConflict,
    execute_pair_reschedule,
    reconcile_pair_reschedule_readback,
    verify_admin_context,
)
from printer_dispatch_claims import (
    PrinterClaimConflict,
    activate_v2_capability,
    claim_current_dispatch,
    ensure_printer_claim_schema,
)
from reschedule_dispatch_versions import exportable_versions
from subscription_dispatch_contract import (
    RECEIPT_VERSION,
    TRUSTED_WRITER,
    _receipt_hash,
    _source_payload,
    ensure_dispatch_schema,
    create_dispatch_router,
)
from workbook_write_lease import (
    FULL_REQUIRED_WRITER_INVENTORY,
    acquire_workbook_lease,
    configure_workbook_writer_capability,
    ensure_workbook_lease_schema,
)

OWNER = "U" + "1" * 32
ADMIN = "U" + "9" * 32
NOW = datetime.fromisoformat("2026-09-26T07:59:00+08:00")
SOURCE = "2026-09-27"
TARGET = "2026-09-28"
THIRD = "2026-09-29"
STORE = "nanjing"
SCOPE = "printer:claim:nanjing"


def old_source_row():
    return [SOURCE, "第1週-日", "午餐A", "500", "30", "晚餐B", "600", "35",
            "1100", "65", "剩100/補5", "$220", "跑步", "待列印"]


class AtomicSheetFake:
    def __init__(self, rows, outcome="ok"):
        self.rows = {row[0].replace("/", "-"): list(row) for row in rows}
        self.outcome = outcome
        self.batch_calls = []
        self.update_cell_calls = []

    def plan_pair_reschedule(self, *, workbook_id, worksheet_id, owner_user_id,
                             source_date, target_date, schedule_rows,
                             expected_schedule_before, target_master_profile=None):
        assert workbook_id == "book-1" and worksheet_id == 17 and owner_user_id == OWNER
        assert {
            day: copy.deepcopy(self.rows.get(day)) for day in (source_date, target_date)
        } == expected_schedule_before
        master = {
            day: [day.replace("-", "/"), OWNER] + [""] * 19
            for day in (source_date, target_date)
        }
        return SimpleNamespace(
            source_date=source_date,
            target_date=target_date,
            schedule_rows=copy.deepcopy(schedule_rows),
            expected_readback={"schedule": copy.deepcopy(schedule_rows), "master": master},
        )

    def apply_pair_reschedule(self, plan):
        updates = [
            {"service_date": day, "values": plan.schedule_rows[day]}
            for day in (plan.source_date, plan.target_date)
        ]
        self.batch_calls.append(copy.deepcopy(updates))
        if self.outcome == "fail_before_apply":
            raise RuntimeError("write rejected")
        staged = copy.deepcopy(self.rows)
        for update in updates:
            assert len(update["values"]) == 17
            staged[update["service_date"]] = list(update["values"])
        self.rows = staged
        if self.outcome == "apply_then_timeout":
            raise TimeoutError("response lost")

    def read_pair_reschedule(self, plan):
        observed = {day: copy.deepcopy(self.rows.get(day)) for day in (plan.source_date, plan.target_date)}
        if observed != plan.expected_readback["schedule"]:
            raise RuntimeError("readback differs")
        return {
            "schedule": observed,
            "master": copy.deepcopy(plan.expected_readback["master"]),
        }

    def read_pair_rows(self, *, owner_user_id, source_date, target_date):
        assert owner_user_id == OWNER
        return {
            "schedule": {
                day: copy.deepcopy(self.rows.get(day))
                for day in (source_date, target_date)
            },
            "master": {
                day: [day.replace("-", "/"), OWNER] + [""] * 19
                for day in (source_date, target_date)
            },
        }

    def batch_update_rows(self, _updates):
        raise AssertionError("legacy schedule-only writer must not be used")

    def read_rows(self, service_dates):
        return {day: copy.deepcopy(self.rows.get(day)) for day in service_dates}

    def update_cell(self, *_args):
        self.update_cell_calls.append(_args)
        raise AssertionError("sequential writes forbidden")


def open_db(
    path,
    *,
    activate_capability=True,
    workbook_id="book-1",
    worksheet_id=17,
    worksheet_title="sheet-1",
    extra_published_rows=(),
):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
      CREATE TABLE subscription_orders(
        id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, meal_count INTEGER NOT NULL,
        status TEXT NOT NULL, formalized_at TEXT NOT NULL DEFAULT '',
        form_payload_json TEXT NOT NULL);
      CREATE TABLE usage(
        user_id TEXT PRIMARY KEY, remaining_meals INTEGER NOT NULL,
        last_date TEXT NOT NULL, expiry_date TEXT NOT NULL, status TEXT NOT NULL);
      CREATE TABLE admin_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE subscription_service_calendar(
        order_id INTEGER NOT NULL, service_date TEXT NOT NULL,
        is_service_day INTEGER NOT NULL, schedule_label TEXT NOT NULL,
        PRIMARY KEY(order_id,service_date));
    """)
    target_master = [TARGET, OWNER, "1800", "", "", "恢復跑", "0", "減脂", "run",
                     "間歇", "", "", "3", "60", "六", "5:00", "", "", "2", "", "0"]
    third_master = [THIRD, OWNER, "1800", "", "", "恢復跑", "0", "減脂", "run",
                    "間歇", "", "", "3", "60", "六", "5:00", "", "", "2", "", "0"]
    form_payload = json.dumps({"master_api_rows": [target_master, third_master]}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO subscription_orders VALUES(1,?,20,'activated',?,?)",
        (OWNER, NOW.isoformat(), form_payload),
    )
    conn.execute("INSERT INTO usage VALUES(?,8,'2026-09-01','2026-10-31','active')", (OWNER,))
    conn.execute("INSERT INTO admin_settings VALUES('admin_id',?)", (ADMIN,))
    conn.executemany(
        "INSERT INTO subscription_service_calendar VALUES(1,?,1,?)",
        [(SOURCE, "第1週-日"), (TARGET, "第2週-一"), (THIRD, "第2週-二")],
    )
    ensure_dispatch_schema(conn)
    row = old_source_row()
    payload_json, payload_hash = _source_payload(row)
    dispatch_id = "dispatch-old"
    conn.execute(
        """INSERT INTO subscription_dispatch_rows
           (dispatch_row_id,order_id,customer_uid,store_id,workbook_id,worksheet_id,
            worksheet_title,service_date,lunch,dinner,menu_version,formalized_at,
            publish_state,created_at,source_payload_json,source_payload_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (dispatch_id, 1, OWNER, STORE, workbook_id, worksheet_id, worksheet_title,
         SOURCE, row[2], row[5], "order-1-v1",
         NOW.isoformat(), "published", NOW.isoformat(), payload_json, payload_hash),
    )
    values = (RECEIPT_VERSION, TRUSTED_WRITER, dispatch_id, 1, OWNER, STORE, workbook_id,
              worksheet_id, worksheet_title, SOURCE, "order-1-v1", payload_json, payload_hash, NOW.isoformat())
    receipt_hash = _receipt_hash(values)
    conn.execute(
        """INSERT INTO subscription_dispatch_publication_receipts
           (receipt_id,receipt_version,trusted_writer,dispatch_row_id,order_id,customer_uid,
            store_id,workbook_id,worksheet_id,worksheet_title,service_date,menu_version,
            source_payload_json,source_payload_hash,published_at,receipt_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("receipt-" + receipt_hash, *values, receipt_hash),
    )
    for dispatch_id, row in extra_published_rows:
        payload_json, payload_hash = _source_payload(row)
        day = row[0].replace("/", "-")
        conn.execute(
            """INSERT INTO subscription_dispatch_rows
               (dispatch_row_id,order_id,customer_uid,store_id,workbook_id,worksheet_id,
                worksheet_title,service_date,lunch,dinner,menu_version,formalized_at,
                publish_state,created_at,source_payload_json,source_payload_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dispatch_id, 1, OWNER, STORE, workbook_id, worksheet_id, worksheet_title,
             day, row[2], row[5], "order-1-v1", NOW.isoformat(), "published",
             NOW.isoformat(), payload_json, payload_hash),
        )
        values = (RECEIPT_VERSION, TRUSTED_WRITER, dispatch_id, 1, OWNER, STORE,
                  workbook_id, worksheet_id, worksheet_title, day, "order-1-v1",
                  payload_json, payload_hash, NOW.isoformat())
        receipt_hash = _receipt_hash(values)
        conn.execute(
            """INSERT INTO subscription_dispatch_publication_receipts
               (receipt_id,receipt_version,trusted_writer,dispatch_row_id,order_id,customer_uid,
                store_id,workbook_id,worksheet_id,worksheet_title,service_date,menu_version,
                source_payload_json,source_payload_hash,published_at,receipt_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("receipt-" + receipt_hash, *values, receipt_hash),
        )
    conn.commit()
    imported = import_initial_published_version(
        conn, order_id=1, store_id=STORE, menu_version="order-1-v1", now=NOW.isoformat()
    )
    ensure_printer_claim_schema(conn)
    if activate_capability:
        activate_v2_capability(
            conn, operation_id="cutover", store_id=STORE, admin_scope=SCOPE,
            generation=1, cache_cleared=True, all_consumers_confirmed=True,
            evidence_digest="a" * 64, now=NOW.isoformat(),
        )
    ensure_workbook_lease_schema(conn)
    configure_workbook_writer_capability(
        conn, workbook_id=workbook_id, controlled_writers=FULL_REQUIRED_WRITER_INVENTORY,
        external_writer_state="controlled", cutover_evidence="test-only-complete-fixture-inventory",
    )
    conn.commit()
    return conn, imported


def call(conn, sheet, **overrides):
    admin_context = overrides.pop("admin_context", None)
    if admin_context is None:
        admin_context = verify_admin_context(conn, ADMIN)
    values = dict(
        order_id=1, source_date=SOURCE, target_date=TARGET, request_id="request-1",
        admin_context=admin_context, now=NOW,
    )
    values.update(overrides)
    return execute_pair_reschedule(conn, sheet, **values)


def test_missing_target_is_one_atomic_17_column_batch_and_confirmed_snapshot(tmp_path):
    conn, imported = open_db(tmp_path / "pair.sqlite3")
    before_receipt = conn.execute(
        "SELECT * FROM subscription_dispatch_publication_receipts"
    ).fetchone()
    before_counts = tuple(conn.execute(
        "SELECT meal_count,(SELECT remaining_meals FROM usage WHERE user_id=?) FROM subscription_orders WHERE id=1",
        (OWNER,),
    ).fetchone())
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])

    result = call(conn, sheet)

    assert result.status == "confirmed"
    assert result.request_id == "request-1"
    assert len(sheet.batch_calls) == 1
    assert len(sheet.batch_calls[0]) == 2
    assert sheet.update_cell_calls == []
    source = sheet.rows[SOURCE]
    target = sheet.rows[TARGET]
    assert len(source) == len(target) == 17
    assert source[2:12] == [""] * 10
    assert target[1] == "第2週-一"
    assert target[2:12] == old_source_row()[2:12]
    assert source[14] != "dispatch-old" and target[14] != "dispatch-old"
    assert source[15] == target[15] == "1"
    assert source[16] == target[16]
    assert tuple(conn.execute("SELECT * FROM subscription_dispatch_publication_receipts").fetchone()) == tuple(before_receipt)
    assert tuple(conn.execute(
        "SELECT meal_count,(SELECT remaining_meals FROM usage WHERE user_id=?) FROM subscription_orders WHERE id=1",
        (OWNER,),
    ).fetchone()) == before_counts
    current = exportable_versions(conn, order_id=1)
    assert len(current) == 1 and current[0]["parent_version_id"] == imported.version_id
    payload = json.loads(current[0]["payload_json"])
    assert payload["authority"] == "pair_reschedule_snapshot/v1"
    assert [row["service_date"] for row in payload["rows"]] == [SOURCE, TARGET]
    assert conn.execute("SELECT count(*) FROM pair_reschedule_snapshot_receipts").fetchone()[0] == 1


def test_apply_then_timeout_stays_fenced_and_reconcile_is_read_only(tmp_path):
    conn, _ = open_db(tmp_path / "unknown.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]], "apply_then_timeout")

    result = call(conn, sheet)
    assert result.status == "sheet_unknown"
    assert exportable_versions(conn, order_id=1) == []
    assert len(sheet.batch_calls) == 1

    replay = call(conn, sheet)
    assert replay.status == "sheet_unknown"
    assert len(sheet.batch_calls) == 1

    recovered = reconcile_pair_reschedule_readback(
        conn, sheet, order_id=1, request_id="request-1",
        admin_context=verify_admin_context(conn, ADMIN), now=NOW,
    )
    assert recovered.status == "confirmed"
    assert len(sheet.batch_calls) == 1
    assert len(exportable_versions(conn, order_id=1)) == 1
    assert tuple(conn.execute(
        "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id='book-1'"
    ).fetchone()) == ("released", "confirmed")


def test_write_failure_is_unknown_and_never_auto_retried(tmp_path):
    conn, _ = open_db(tmp_path / "write-fail.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]], "fail_before_apply")
    assert call(conn, sheet).status == "sheet_unknown"
    assert call(conn, sheet).status == "sheet_unknown"
    assert len(sheet.batch_calls) == 1
    assert exportable_versions(conn, order_id=1) == []
    assert conn.execute(
        "SELECT status FROM workbook_write_leases WHERE workbook_id='book-1'"
    ).fetchone()[0] == "active"


def test_workbook_lease_covers_read_plan_write_readback_and_releases_on_success(tmp_path):
    conn, _ = open_db(tmp_path / "lease-success.sqlite3")

    class LeaseAssertingSheet(AtomicSheetFake):
        def _active(self):
            assert conn.execute(
                "SELECT status FROM workbook_write_leases WHERE workbook_id='book-1'"
            ).fetchone()[0] == "active"

        def read_rows(self, service_dates):
            self._active()
            return super().read_rows(service_dates)

        def plan_pair_reschedule(self, **kwargs):
            self._active()
            return super().plan_pair_reschedule(**kwargs)

        def apply_pair_reschedule(self, plan):
            self._active()
            return super().apply_pair_reschedule(plan)

        def read_pair_reschedule(self, plan):
            self._active()
            return super().read_pair_reschedule(plan)

    sheet = LeaseAssertingSheet([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    assert call(conn, sheet).status == "confirmed"
    assert tuple(conn.execute(
        "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id='book-1'"
    ).fetchone()) == ("released", "confirmed")


def test_busy_workbook_lease_rejects_before_any_sheet_read_or_reservation(tmp_path):
    conn, _ = open_db(tmp_path / "lease-busy.sqlite3")
    acquire_workbook_lease(
        conn, workbook_id="book-1", writer_id="pair_reschedule",
        operation_id="other-operation", now=NOW, ttl_seconds=120,
    )
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])

    with pytest.raises(PairRescheduleConflict, match="already leased"):
        call(conn, sheet)

    assert sheet.batch_calls == []
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 0


def test_pair_only_inventory_direct_coordinator_rejects_before_sheet_read(tmp_path):
    conn, _ = open_db(tmp_path / "pair-only-inventory.sqlite3")
    conn.execute(
        "UPDATE workbook_writer_capabilities SET controlled_writers_json=?",
        ('["pair_reschedule"]',),
    )
    conn.commit()

    class NoSheetAccess:
        def __getattr__(self, name):
            raise AssertionError(f"Sheet access reached: {name}")

    with pytest.raises(PairRescheduleConflict, match="full writer inventory"):
        call(conn, NoSheetAccess())
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 0


def test_same_request_sibling_mismatch_is_rejected_without_second_write(tmp_path):
    conn, _ = open_db(tmp_path / "replay.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    assert call(conn, sheet).status == "confirmed"
    with pytest.raises(PairRescheduleConflict, match="request replay binding differs"):
        call(conn, sheet, target_date="2026-09-29")
    assert len(sheet.batch_calls) == 1


def test_legacy_capability_and_missing_calendar_fail_before_reservation_or_sheet(tmp_path):
    conn, _ = open_db(tmp_path / "gates.sqlite3", activate_capability=False)
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    with pytest.raises(PairRescheduleConflict, match="consumer capability"):
        call(conn, sheet)
    assert sheet.batch_calls == []
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 0

    activate_v2_capability(
        conn, operation_id="cutover-2", store_id=STORE, admin_scope=SCOPE,
        generation=2, cache_cleared=True, all_consumers_confirmed=True,
        evidence_digest="b" * 64, now=NOW.isoformat(),
    )
    conn.execute("DELETE FROM subscription_service_calendar WHERE service_date=?", (TARGET,))
    conn.commit()
    with pytest.raises(PairRescheduleConflict, match="service calendar configuration"):
        call(conn, sheet)
    assert sheet.batch_calls == []
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 0


def _resolved(conn, service_date):
    return resolve_trusted_export_rows(
        conn,
        where_sql="r.store_id=? AND r.workbook_id=? AND r.service_date=? ORDER BY r.dispatch_row_id",
        parameters=(STORE, "book-1", service_date),
    )


def _claim_successor(conn, version_id, *, dispatch_row_id, service_date,
                     operation_id="print-successor"):
    return claim_current_dispatch(
        conn, operation_id=operation_id, version_id=version_id, order_id=1,
        owner_user_id=OWNER, store_id=STORE, admin_scope=SCOPE,
        capability_generation=1, claim_token="server-side-claim",
        dispatch_row_id=dispatch_row_id, service_date=service_date,
        lease_expires_at="2026-09-26T08:05:00+08:00", now=NOW.isoformat(),
    )


def test_confirmed_snapshot_projects_new_rows_and_claim_binds_snapshot_payload(tmp_path):
    conn, imported = open_db(tmp_path / "projection.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    result = call(conn, sheet)
    assert result.status == "confirmed"

    source_rows = _resolved(conn, SOURCE)
    target_rows = _resolved(conn, TARGET)
    # The vacated source has no meal to dispatch.  The real acceptance condition is
    # that the newly occupied target is projected from the trusted successor snapshot.
    assert source_rows == []
    assert len(target_rows) == 1
    assert target_rows[0]["dispatch_row_id"] != "dispatch-old"
    assert target_rows[0]["source_columns"][2:12] == old_source_row()[2:12]
    assert target_rows[0]["authority"] == "pair_reschedule_snapshot/v1"

    with pytest.raises(PrinterClaimConflict, match="current version"):
        _claim_successor(
            conn, imported.version_id, dispatch_row_id="dispatch-old",
            service_date=SOURCE, operation_id="cached-old",
        )
    claim = _claim_successor(
        conn, result.new_version_id,
        dispatch_row_id=target_rows[0]["dispatch_row_id"], service_date=TARGET,
    )
    persisted_hash = conn.execute(
        "SELECT payload_hash FROM printer_dispatch_claims WHERE operation_id=?",
        (claim.operation_id,),
    ).fetchone()[0]
    assert persisted_hash == result.expected_payload_hash


def test_forged_snapshot_row_or_hash_is_not_exportable_or_claimable(tmp_path):
    conn, _ = open_db(tmp_path / "forged.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    result = call(conn, sheet)
    successor_payload = json.loads(conn.execute(
        "SELECT payload_json FROM reschedule_dispatch_versions WHERE version_id=?",
        (result.new_version_id,),
    ).fetchone()[0])
    target_scope = next(row for row in successor_payload["rows"] if row["service_date"] == TARGET)
    conn.execute("DROP TRIGGER pair_reschedule_rows_no_update")
    conn.execute(
        "UPDATE pair_reschedule_snapshot_rows SET row_json='[]' WHERE service_date=?",
        (TARGET,),
    )
    conn.commit()

    assert _resolved(conn, TARGET) == []
    with pytest.raises(PrinterClaimConflict, match="dispatch authority"):
        _claim_successor(
            conn, result.new_version_id,
            dispatch_row_id=target_scope["dispatch_row_id"], service_date=TARGET,
        )


def test_real_sqlite_fake_sheet_supports_legacy_to_v2_to_v3_and_rejects_tampered_middle(tmp_path):
    conn, imported = open_db(tmp_path / "multi-hop.sqlite3")
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])

    v2 = call(conn, sheet, request_id="request-v2")
    assert v2.status == "confirmed"
    v2_rows = _resolved(conn, TARGET)
    assert _resolved(conn, SOURCE) == []
    assert len(v2_rows) == 1
    moved_columns = list(v2_rows[0]["source_columns"])

    v3 = call(
        conn, sheet, source_date=TARGET, target_date=THIRD,
        request_id="request-v3",
    )
    assert v3.status == "confirmed"
    assert v3.old_version_id == v2.new_version_id
    assert len(sheet.batch_calls) == 2
    assert _resolved(conn, SOURCE) == []
    assert _resolved(conn, TARGET) == []
    v3_rows = _resolved(conn, THIRD)
    assert len(v3_rows) == 1
    assert v3_rows[0]["source_columns"][2:12] == moved_columns[2:12]
    assert json.loads(conn.execute(
        "SELECT payload_json FROM reschedule_dispatch_versions WHERE version_id=?",
        (v3.new_version_id,),
    ).fetchone()[0])["parent_version_id"] == v2.new_version_id
    assert conn.execute(
        "SELECT count(*) FROM reschedule_dispatch_versions WHERE version_id=?",
        (imported.version_id,),
    ).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_versions").fetchone()[0] == 3

    current_scope = v3_rows[0]["dispatch_row_id"]
    with pytest.raises(PrinterClaimConflict, match="current version"):
        _claim_successor(
            conn, v2.new_version_id,
            dispatch_row_id=v2_rows[0]["dispatch_row_id"], service_date=TARGET,
            operation_id="cached-v2",
        )

    conn.execute("DROP TRIGGER pair_reschedule_rows_no_update")
    conn.execute(
        "UPDATE pair_reschedule_snapshot_rows SET row_json='[]' "
        "WHERE operation_id=? AND service_date=?",
        (v2.operation_id, TARGET),
    )
    conn.commit()
    assert _resolved(conn, THIRD) == []
    with pytest.raises(PrinterClaimConflict, match="dispatch authority"):
        _claim_successor(
            conn, v3.new_version_id, dispatch_row_id=current_scope,
            service_date=THIRD, operation_id="tampered-middle",
        )


def test_two_original_sources_keep_their_publication_versions_across_successors(tmp_path):
    fourth = "2026-09-30"
    fifth, sixth, seventh = "2026-10-01", "2026-10-02", "2026-10-03"
    third_row = old_source_row()
    third_row[0], third_row[1], third_row[2], third_row[5] = (
        THIRD, "第2週-二", "午餐C", "晚餐D")
    fifth_row = old_source_row()
    fifth_row[0], fifth_row[1], fifth_row[2], fifth_row[5] = (
        fifth, "第2週-四", "午餐E", "晚餐F")
    conn, _ = open_db(
        tmp_path / "two-sources.sqlite3",
        extra_published_rows=[("dispatch-third", third_row), ("dispatch-fifth", fifth_row)],
    )
    conn.executemany("INSERT INTO subscription_service_calendar VALUES(1,?,1,?)",
                     [(fourth, "第2週-三"), (fifth, "第2週-四"),
                      (sixth, "第2週-五"), (seventh, "第2週-六")])
    profile = json.loads(conn.execute("SELECT form_payload_json FROM subscription_orders WHERE id=1").fetchone()[0])
    for day in (fourth, sixth, seventh):
        new_master = list(profile["master_api_rows"][-1])
        new_master[0] = day
        profile["master_api_rows"].append(new_master)
    conn.execute("UPDATE subscription_orders SET form_payload_json=? WHERE id=1",
                 (json.dumps(profile, ensure_ascii=False),))
    conn.commit()
    sheet = AtomicSheetFake([
        old_source_row() + ["dispatch-old", "1", "order-1-v1"],
        third_row + ["dispatch-third", "1", "order-1-v1"],
        fifth_row + ["dispatch-fifth", "1", "order-1-v1"],
    ])

    first = call(conn, sheet, request_id="first-source")
    assert first.status == "confirmed"
    assert sheet.rows[THIRD] == third_row + ["dispatch-third", "1", "order-1-v1"]
    second = call(conn, sheet, source_date=THIRD, target_date=fourth,
                  request_id="second-source")
    assert second.status == "confirmed"
    assert len(sheet.batch_calls) == 2
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM reschedule_dispatch_versions WHERE version_id=?",
        (second.new_version_id,),
    ).fetchone()[0])
    assert {row["service_date"] for row in payload["rows"]} == {SOURCE, TARGET, THIRD, fourth, fifth}
    assert next(row["columns"] for row in payload["rows"] if row["service_date"] == TARGET) == sheet.rows[TARGET]
    assert next(row["columns"] for row in payload["rows"] if row["service_date"] == fifth) == sheet.rows[fifth]
    assert sheet.rows[fifth][16] == "order-1-v1"

    moved_again = call(conn, sheet, source_date=TARGET, target_date=sixth,
                       request_id="moved-again")
    assert moved_again.status == "confirmed"
    untouched_third = call(conn, sheet, source_date=fifth, target_date=seventh,
                           request_id="untouched-third")
    assert untouched_third.status == "confirmed"
    assert len(sheet.batch_calls) == 4


@pytest.mark.parametrize("column", range(17))
def test_unchanged_source_exact_seventeen_column_fence_after_prior_move(tmp_path, column):
    fourth = "2026-09-30"
    third_row = old_source_row()
    third_row[0], third_row[1], third_row[2], third_row[5] = (
        THIRD, "第2週-二", "午餐C", "晚餐D")
    conn, _ = open_db(tmp_path / "fence.sqlite3",
                      extra_published_rows=[("dispatch-third", third_row)])
    conn.execute("INSERT INTO subscription_service_calendar VALUES(1,?,1,?)", (fourth, "第2週-三"))
    profile = json.loads(conn.execute("SELECT form_payload_json FROM subscription_orders WHERE id=1").fetchone()[0])
    new_master = list(profile["master_api_rows"][-1])
    new_master[0] = fourth
    profile["master_api_rows"].append(new_master)
    conn.execute("UPDATE subscription_orders SET form_payload_json=? WHERE id=1",
                 (json.dumps(profile, ensure_ascii=False),))
    conn.commit()
    sheet = AtomicSheetFake([
        old_source_row() + ["dispatch-old", "1", "order-1-v1"],
        third_row + ["dispatch-third", "1", "order-1-v1"],
    ])
    assert call(conn, sheet, request_id="prior-move").status == "confirmed"
    sheet.rows[THIRD][column] = "tampered"

    with pytest.raises(PairRescheduleConflict, match="Sheet source differs from trusted publication"):
        call(conn, sheet, source_date=THIRD, target_date=fourth,
             request_id=f"tamper-{column}")
    assert len(sheet.batch_calls) == 1
    assert conn.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 1


HTTP_TOKEN = "printer-http-" + "p" * 32


def _http_client(path, *, enabled):
    def read_connection():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def write_connection():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    app = FastAPI()
    app.include_router(create_dispatch_router(
        connection_factory=read_connection,
        claim_connection_factory=write_connection,
        export_token=HTTP_TOKEN,
        workbook_id="book-1",
        now_factory=lambda: NOW,
        allowed_date_window_days=4,
        versioned_dispatch_enabled=enabled,
    ))
    return TestClient(app, base_url="https://printer.test")


def test_registered_http_feature_off_has_no_claim_result_routes_or_writes(tmp_path):
    path = tmp_path / "http-off.sqlite3"
    conn, _ = open_db(path)
    before = conn.execute("SELECT count(*) FROM printer_dispatch_claims").fetchone()[0]
    conn.close()
    client = _http_client(path, enabled=False)
    headers = {"Authorization": f"Bearer {HTTP_TOKEN}"}

    assert client.post("/internal/printer/v1/dispatch-claims", headers=headers, json={
        "operation_id": "print-http-1", "dispatch_row_id": "dispatch-old",
        "service_date": SOURCE,
    }).status_code == 404
    assert client.post("/internal/printer/v1/dispatch-results", headers=headers, json={
        "operation_id": "print-http-1", "send_permit": "not-real",
        "outcome": "outcome_unknown", "ack_id": "", "evidence_digest": "b" * 64,
    }).status_code == 404
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT count(*) FROM printer_dispatch_claims").fetchone()[0] == before


def test_registered_http_projects_rescheduled_date_and_claims_once_before_fake_send(tmp_path):
    path = tmp_path / "http-on.sqlite3"
    conn, _ = open_db(path)
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    completed = call(conn, sheet)
    assert completed.status == "confirmed"
    conn.close()
    client = _http_client(path, enabled=True)
    headers = {"Authorization": f"Bearer {HTTP_TOKEN}"}

    source = client.get("/internal/printer/v1/dispatch-contract", params={
        "store_id": STORE, "service_date": SOURCE,
    }, headers=headers)
    target = client.get("/internal/printer/v1/dispatch-contract", params={
        "store_id": STORE, "service_date": TARGET,
    }, headers=headers)
    assert source.status_code == target.status_code == 200
    assert source.json()["rows"] == []
    assert len(target.json()["rows"]) == 1
    exported = target.json()["rows"][0]
    assert exported["service_date"] == TARGET
    assert exported["dispatch_row_id"] != "dispatch-old"

    request = {
        "operation_id": "print-http-1",
        "dispatch_row_id": exported["dispatch_row_id"],
        "service_date": TARGET,
    }
    first = client.post("/internal/printer/v1/dispatch-claims", headers=headers, json=request)
    assert first.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["status"] == "send_permitted"
    assert first.json()["send_permit"]
    fake_transport_sends = 1

    replay = client.post("/internal/printer/v1/dispatch-claims", headers=headers, json=request)
    assert replay.status_code == 409
    assert replay.json() == {"detail": "already claimed"}
    assert "send_permit" not in replay.text
    assert fake_transport_sends == 1

    accepted = client.post("/internal/printer/v1/dispatch-results", headers=headers, json={
        "operation_id": request["operation_id"],
        "send_permit": first.json()["send_permit"],
        "outcome": "transport_accepted",
        "ack_id": "fake-transport-ack",
        "evidence_digest": "b" * 64,
    })
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "transport_accepted"
    assert accepted.json()["printed"] is False
    assert client.get("/internal/printer/v1/dispatch-contract", params={
        "store_id": STORE, "service_date": TARGET,
    }, headers=headers).json()["rows"] == []


def test_registered_http_claim_rejects_client_authority_and_requires_https_auth(tmp_path):
    path = tmp_path / "http-boundary.sqlite3"
    conn, _ = open_db(path)
    conn.close()
    client = _http_client(path, enabled=True)
    body = {"operation_id": "print-http-2", "dispatch_row_id": "dispatch-old", "service_date": SOURCE}

    missing = client.post("/internal/printer/v1/dispatch-claims", json=body)
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    injected = client.post(
        "/internal/printer/v1/dispatch-claims",
        headers={"Authorization": f"Bearer {HTTP_TOKEN}"},
        json={**body, "owner_user_id": OWNER, "admin_scope": SCOPE, "version_id": "caller"},
    )
    assert injected.status_code == 422
    assert OWNER not in injected.text
    insecure = TestClient(client.app, base_url="http://printer.test").post(
        "/internal/printer/v1/dispatch-claims",
        headers={"Authorization": f"Bearer {HTTP_TOKEN}"}, json=body,
    )
    assert insecure.status_code == 400


def test_target_occupied_entitlement_admin_and_today_cutoff_fail_closed(tmp_path):
    conn, _ = open_db(tmp_path / "policy.sqlite3")
    occupied = [TARGET, "第2週-一", "已有午餐", "1", "1", "", "", "", "", "", "", "", "", "待列印",
                "other", "1", "other-version"]
    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"], occupied])
    with pytest.raises(PairRescheduleConflict, match="target meal cells are not empty"):
        call(conn, sheet)
    assert sheet.batch_calls == []

    conn.execute("UPDATE usage SET remaining_meals=0 WHERE user_id=?", (OWNER,))
    conn.commit()
    empty_sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    with pytest.raises(PairRescheduleConflict, match="active entitlement"):
        call(conn, empty_sheet)

    conn.execute("UPDATE usage SET remaining_meals=8 WHERE user_id=?", (OWNER,))
    trusted_admin = verify_admin_context(conn, ADMIN)
    conn.execute("UPDATE admin_settings SET value='other' WHERE key='admin_id'")
    conn.commit()
    with pytest.raises(PairRescheduleConflict, match="admin context"):
        call(conn, empty_sheet, admin_context=trusted_admin)

    conn.execute("UPDATE admin_settings SET value=? WHERE key='admin_id'", (ADMIN,))
    conn.execute("INSERT OR REPLACE INTO subscription_service_calendar VALUES(1,'2026-09-26',1,'今日')")
    conn.commit()
    with pytest.raises(PairRescheduleConflict, match="cutoff"):
        call(conn, empty_sheet, source_date="2026-09-26", now=datetime.fromisoformat("2026-09-26T08:00:00+08:00"))
    assert empty_sheet.batch_calls == []
