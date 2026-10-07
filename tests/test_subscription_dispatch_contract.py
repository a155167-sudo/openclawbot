import sqlite3
import json
import socket
from datetime import date, datetime, timezone, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from subscription_dispatch_contract import (
    DISPATCH_HEADERS,
    ORIGINAL_HEADERS,
    DispatchConflict,
    DispatchNotEligible,
    create_dispatch_router,
    ensure_dispatch_schema,
    merge_tagged_schedule_rows,
    publish_schedule,
    reconcile_staged_schedule,
    schedule_is_already_published,
    stage_schedule,
)

UID = "U" + "1" * 32
TOKEN = "device-test-token-" + "x" * 32
NOW = datetime(2026, 9, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def order_db(status="activated", uid=UID):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, formalized_at TEXT DEFAULT '')")
    conn.execute("INSERT INTO subscription_orders(id,user_id,status) VALUES(1,?,?)", (uid, status))
    ensure_dispatch_schema(conn)
    conn.commit()
    return conn


def schedule_rows():
    return [ORIGINAL_HEADERS, ["2026/09/16", "第1週-三", "午餐 A ($100)", 500, 30, "晚餐 B ($120)", 600, 35, 700, 65, "剩 100kcal / 補 5g", "$220", "", "待列印"]]


class FakeSheet:
    def __init__(self, *, fail_write=False, write_error=None, fail_read=False, mutate_read=False, rows=None):
        self.id = 123456789
        self.title = "王小明_1111_20260915"
        self.fail_write = fail_write
        self.write_error = write_error
        self.fail_read = fail_read
        self.mutate_read = mutate_read
        self.rows = [list(row) for row in (rows or [])]
        self.writes = 0

    def replace_schedule(self, rows):
        self.writes += 1
        if self.fail_write or self.write_error:
            raise self.write_error or RuntimeError("fake write failed")
        self.rows = merge_tagged_schedule_rows(self.rows, rows)
        self._last_ids = [str(row[14]) for row in rows[1:]]
        return [list(rows[0])] + [
            list(next(row for row in self.rows if len(row) > 14 and str(row[14]) == dispatch_id))
            for dispatch_id in self._last_ids
        ]

    def read_schedule(self):
        if self.fail_read:
            raise RuntimeError("fake read failed")
        wanted = getattr(self, "_last_ids", [])
        rows = [ORIGINAL_HEADERS + DISPATCH_HEADERS] + [
            list(next(row for row in self.rows if len(row) > 14 and str(row[14]) == dispatch_id))
            for dispatch_id in wanted
        ]
        if self.mutate_read and len(rows) > 1:
            rows[1][2] = "被改餐"
        return rows


class ReadOnlyExistingSheet:
    """Recovery adapter: reading is allowed; every Sheet mutation is forbidden."""
    id = 123456789
    title = "王小明_1111_20260915"

    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.reads = 0
        self.writes = 0
        self.workbook_id = "runtime-sheet-id"

    def read_existing_schedule(self, tagged_rows):
        self.reads += 1
        return [list(row) for row in self.rows]

    def replace_schedule(self, _rows):
        self.writes += 1
        raise AssertionError("bounded reconcile must never mutate Sheet")


def stage(conn, **kwargs):
    return stage_schedule(
        conn,
        order_id=1,
        snapshot_uid=UID,
        workbook_id="runtime-sheet-id",
        worksheet_title="王小明_1111_20260915",
        schedule_rows=schedule_rows(),
        now=NOW,
        id_factory=kwargs.get("id_factory", iter(["dispatch-1"]).__next__),
    )


def test_stage_requires_paid_activated_order_and_matching_uid():
    with pytest.raises(DispatchNotEligible):
        stage(order_db("approved"))
    with pytest.raises(DispatchNotEligible):
        stage_schedule(order_db(), order_id=1, snapshot_uid="U" + "2" * 32, workbook_id="runtime-sheet-id", worksheet_title="x", schedule_rows=schedule_rows(), now=NOW, id_factory=lambda: "never")


def test_stage_preserves_original_14_columns_and_appends_stable_identity():
    conn = order_db()
    rows = stage(conn)
    assert rows[0] == ORIGINAL_HEADERS + DISPATCH_HEADERS
    assert rows[1][:14] == schedule_rows()[1]
    assert rows[1][14:] == ["dispatch-1", "1", "order-1-v1"]


def test_retry_reuses_same_dispatch_id_and_rejects_changed_payload():
    conn = order_db()
    first = stage(conn)
    second = stage(conn, id_factory=lambda: "must-not-be-used")
    assert second == first
    changed = schedule_rows()
    changed[1][2] = "不同午餐"
    with pytest.raises(DispatchConflict):
        stage_schedule(conn, order_id=1, snapshot_uid=UID, workbook_id="runtime-sheet-id", worksheet_title="王小明_1111_20260915", schedule_rows=changed, now=NOW, id_factory=lambda: "new-id")
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_rows").fetchone()[0] == 1


@pytest.mark.parametrize("column", range(14))
def test_retry_rejects_every_changed_original_payload_column(column):
    conn = order_db()
    stage(conn)
    changed = schedule_rows()
    changed[1][column] = "2026-09-17" if column == 0 else f"tampered-{column}"

    with pytest.raises(DispatchConflict):
        stage_schedule(
            conn,
            order_id=1,
            snapshot_uid=UID,
            workbook_id="runtime-sheet-id",
            worksheet_title="王小明_1111_20260915",
            schedule_rows=changed,
            now=NOW,
            id_factory=lambda: "new-id",
        )


@pytest.mark.parametrize("sheet", [FakeSheet(fail_write=True), FakeSheet(fail_read=True), FakeSheet(mutate_read=True)])
def test_sheet_failure_or_untrusted_readback_never_publishes_or_formalizes(sheet):
    conn = order_db()
    tagged = stage(conn)
    with pytest.raises(RuntimeError):
        publish_schedule(conn, order_id=1, sheet=sheet, tagged_rows=tagged, now=NOW)
    row = conn.execute("SELECT publish_state, worksheet_id, formalized_at FROM subscription_dispatch_rows").fetchone()
    assert tuple(row) == ("staged", None, "")
    assert conn.execute("SELECT formalized_at FROM subscription_orders WHERE id=1").fetchone()[0] == ""


def test_successful_verified_publish_sets_identity_and_formalized_timestamp():
    conn = order_db()
    tagged = stage(conn)
    publish_schedule(conn, order_id=1, sheet=FakeSheet(), tagged_rows=tagged, now=NOW)
    row = conn.execute("SELECT publish_state, worksheet_id, formalized_at FROM subscription_dispatch_rows").fetchone()
    assert tuple(row) == ("published", 123456789, NOW.isoformat())
    assert conn.execute("SELECT formalized_at FROM subscription_orders WHERE id=1").fetchone()[0] == NOW.isoformat()


def test_formatted_numeric_readback_is_exact_decimal_semantic_only_for_six_columns():
    conn = order_db()
    rows = schedule_rows()
    for column in (3, 4, 6, 7, 8, 9):
        rows[1][column] = float(rows[1][column])
    tagged = stage_schedule(
        conn, order_id=1, snapshot_uid=UID, workbook_id="runtime-sheet-id",
        worksheet_title="王小明_1111_20260915", schedule_rows=rows, now=NOW,
        id_factory=lambda: "dispatch-1",
    )

    class FormattedNumericSheet(FakeSheet):
        def read_schedule(self):
            rows = super().read_schedule()
            for column in (3, 4, 6, 7, 8, 9):
                rows[1][column] = str(int(rows[1][column]))
            return rows

    publish_schedule(conn, order_id=1, sheet=FormattedNumericSheet(), tagged_rows=tagged, now=NOW)

    receipt = conn.execute(
        "SELECT source_payload_json,source_payload_hash FROM subscription_dispatch_publication_receipts"
    ).fetchone()
    assert json.loads(receipt[0]) == [str(value) for value in rows[1]]
    assert len(receipt[1]) == 64


@pytest.mark.parametrize("bad_value", [
    "", True, "NaN", "Infinity", "-Infinity", "500.0000000001",
    "0E+999999999", "1E+31", "1E-31", "1" * 65,
])
def test_numeric_readback_rejects_blank_bool_nonfinite_and_any_value_change(bad_value):
    conn = order_db()
    tagged = stage(conn)

    class BadNumericSheet(FakeSheet):
        def read_schedule(self):
            rows = super().read_schedule()
            rows[1][3] = bad_value
            return rows

    with pytest.raises(RuntimeError, match="readback mismatch"):
        publish_schedule(conn, order_id=1, sheet=BadNumericSheet(), tagged_rows=tagged, now=NOW)
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_publication_receipts").fetchone()[0] == 0


def test_zero_is_not_semantically_equal_to_blank():
    conn = order_db()
    rows = schedule_rows()
    rows[1][3] = 0
    tagged = stage_schedule(
        conn, order_id=1, snapshot_uid=UID, workbook_id="runtime-sheet-id",
        worksheet_title="王小明_1111_20260915", schedule_rows=rows, now=NOW,
        id_factory=lambda: "dispatch-1",
    )

    class BlankSheet(FakeSheet):
        def read_schedule(self):
            result = super().read_schedule()
            result[1][3] = ""
            return result

    with pytest.raises(RuntimeError, match="readback mismatch"):
        publish_schedule(conn, order_id=1, sheet=BlankSheet(), tagged_rows=tagged, now=NOW)


def test_bounded_reconcile_read_only_finalizes_exact_existing_rows_and_is_idempotent():
    conn = order_db()
    tagged = stage(conn)
    formatted = [list(tagged[0]), list(tagged[1])]
    for column in (3, 4, 6, 7, 8, 9):
        formatted[1][column] = str(int(formatted[1][column]))
    sheet = ReadOnlyExistingSheet(formatted)

    assert reconcile_staged_schedule(
        conn, order_id=1, sheet=sheet, tagged_rows=tagged, now=NOW,
        expected_row_count=1,
    ) is True
    first_dump = "\n".join(conn.iterdump())
    assert reconcile_staged_schedule(
        conn, order_id=1, sheet=sheet, tagged_rows=tagged, now=NOW,
        expected_row_count=1,
    ) is True

    assert sheet.reads == 1
    assert sheet.writes == 0
    assert "\n".join(conn.iterdump()) == first_dump
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_publication_receipts").fetchone()[0] == 1


@pytest.mark.parametrize("corruption", ["wrong_order", "wrong_width", "wrong_owner", "wrong_workbook"])
def test_bounded_reconcile_rejects_incomplete_tagged_identity_before_sheet_read(corruption):
    conn = order_db()
    tagged = stage(conn)
    changed = [list(tagged[0]), list(tagged[1])]
    if corruption == "wrong_order":
        changed[1][15] = "2"
    elif corruption == "wrong_width":
        changed[1].pop()
    else:
        column = {"wrong_owner": "customer_uid", "wrong_workbook": "workbook_id"}[corruption]
        conn.execute(f"UPDATE subscription_dispatch_rows SET {column}='foreign' WHERE order_id=1")
    sheet = ReadOnlyExistingSheet(changed)
    before = "\n".join(conn.iterdump())

    with pytest.raises(DispatchConflict):
        reconcile_staged_schedule(
            conn, order_id=1, sheet=sheet, tagged_rows=changed, now=NOW,
            expected_row_count=1,
        )

    assert sheet.reads == 0
    assert sheet.writes == 0
    assert "\n".join(conn.iterdump()) == before


def test_bounded_reconcile_honors_active_mutation_lock_before_sheet_read(monkeypatch):
    import meal_mutation_ledger

    conn = order_db()
    tagged = stage(conn)
    sheet = ReadOnlyExistingSheet(tagged)
    monkeypatch.setattr(meal_mutation_ledger, "meal_mutation_blocks_publication", lambda *_a, **_k: True)
    before = "\n".join(conn.iterdump())

    with pytest.raises(DispatchConflict, match="active meal mutation"):
        reconcile_staged_schedule(
            conn, order_id=1, sheet=sheet, tagged_rows=tagged, now=NOW,
            expected_row_count=1,
        )

    assert sheet.reads == 0
    assert sheet.writes == 0
    assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize("corruption", ["mismatched_id", "duplicate", "extra_unknown", "wrong_date", "wrong_menu"])
def test_bounded_reconcile_rejects_ambiguous_or_mismatched_sheet_without_writes(corruption):
    conn = order_db()
    tagged = stage(conn)
    observed = [list(tagged[0]), list(tagged[1])]
    if corruption == "mismatched_id":
        observed[1][14] = "foreign-id"
    elif corruption == "duplicate":
        observed.append(list(observed[1]))
    elif corruption == "extra_unknown":
        extra = list(observed[1]); extra[14] = "unknown-id"; observed.append(extra)
    elif corruption == "wrong_date":
        observed[1][0] = "2026/09/17"
    else:
        observed[1][16] = "order-1-v2"
    sheet = ReadOnlyExistingSheet(observed)
    before = "\n".join(conn.iterdump())

    with pytest.raises((DispatchConflict, RuntimeError)):
        reconcile_staged_schedule(
            conn, order_id=1, sheet=sheet, tagged_rows=tagged, now=NOW,
            expected_row_count=1,
        )

    assert sheet.writes == 0
    assert "\n".join(conn.iterdump()) == before


def test_incremental_sheet_merge_preserves_unknown_rows_and_printed_status():
    tagged = [ORIGINAL_HEADERS + DISPATCH_HEADERS, schedule_rows()[1] + ["dispatch-1", "1", "order-1-v1"]]
    old_dispatch = list(tagged[1])
    old_dispatch[2] = "舊午餐"
    old_dispatch[13] = "已列印"
    unknown = ["歷史人工備註", "不可刪"]

    merged = merge_tagged_schedule_rows(
        [["既有客戶資料"], ORIGINAL_HEADERS + DISPATCH_HEADERS, old_dispatch, unknown],
        tagged,
    )

    assert ["既有客戶資料"] in merged
    assert unknown in merged
    matching = [row for row in merged if len(row) > 14 and row[14] == "dispatch-1"]
    assert len(matching) == 1
    assert matching[0][2] == schedule_rows()[1][2]
    assert matching[0][13] == "已列印"


@pytest.mark.parametrize("write_error", [RuntimeError("fake failure"), TimeoutError("fake timeout")])
def test_sheet_write_failure_preserves_preexisting_snapshot(write_error):
    old_rows = [["既有客戶資料"], ["歷史人工備註", "不可刪"]]
    sheet = FakeSheet(write_error=write_error, rows=old_rows)
    conn = order_db()
    tagged = stage(conn)

    with pytest.raises(RuntimeError, match="publish/readback failed"):
        publish_schedule(conn, order_id=1, sheet=sheet, tagged_rows=tagged, now=NOW)

    assert sheet.rows == old_rows


def test_completed_publish_retry_performs_zero_sheet_writes():
    conn = order_db()
    tagged = stage(conn)
    first_sheet = FakeSheet()
    publish_schedule(conn, order_id=1, sheet=first_sheet, tagged_rows=tagged, now=NOW)
    assert first_sheet.writes == 1
    retry_sheet = FakeSheet(fail_write=True)

    publish_schedule(conn, order_id=1, sheet=retry_sheet, tagged_rows=tagged, now=NOW)

    assert retry_sheet.writes == 0
    assert conn.execute("SELECT publish_state FROM subscription_dispatch_rows").fetchone()[0] == "published"


def test_published_schedule_preflight_matches_exact_stable_ids_and_target():
    conn = order_db()
    tagged = stage(conn)
    publish_schedule(conn, order_id=1, sheet=FakeSheet(), tagged_rows=tagged, now=NOW)

    assert schedule_is_already_published(
        conn,
        order_id=1,
        workbook_id="runtime-sheet-id",
        worksheet_title="王小明_1111_20260915",
        tagged_rows=tagged,
    ) is True
    changed = [list(tagged[0]), list(tagged[1])]
    changed[1][14] = "foreign-id"
    assert schedule_is_already_published(
        conn,
        order_id=1,
        workbook_id="runtime-sheet-id",
        worksheet_title="王小明_1111_20260915",
        tagged_rows=changed,
    ) is False


def client_for(conn, token=TOKEN):
    app = FastAPI()
    def connection_factory():
        snapshot = sqlite3.connect(":memory:", check_same_thread=False)
        conn.backup(snapshot)
        return snapshot
    app.include_router(create_dispatch_router(connection_factory=connection_factory, export_token=token, workbook_id="runtime-sheet-id", now_factory=lambda: NOW, allowed_date_window_days=1))
    return TestClient(app)


def publish_one(conn):
    tagged = stage(conn)
    publish_schedule(conn, order_id=1, sheet=FakeSheet(), tagged_rows=tagged, now=NOW)
    conn.commit()


def test_export_auth_is_required_and_wrong_device_credential_is_forbidden():
    conn = order_db(); publish_one(conn)
    client = client_for(conn)
    missing = client.get("/internal/printer/v1/dispatch-contract", params={"store_id": "nanjing", "service_date": "2026-09-16"})
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.headers["cache-control"] == "no-store"
    response = client.get("/internal/printer/v1/dispatch-contract", params={"store_id": "nanjing", "service_date": "2026-09-16"}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert UID not in response.text and TOKEN not in response.text


def test_export_only_returns_exact_store_date_published_currently_activated_rows():
    conn = order_db(); publish_one(conn)
    client = client_for(conn)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = client.get("/internal/printer/v1/dispatch-contract", params={"store_id": "nanjing", "service_date": "2026-09-16"}, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    payload = response.json()
    assert payload["workbook_id"] == "runtime-sheet-id"
    assert [row["dispatch_row_id"] for row in payload["rows"]] == ["dispatch-1"]
    assert payload["rows"][0]["source_columns"] == [str(value) for value in schedule_rows()[1]]
    assert payload["rows"][0]["publication_print_status"] == "待列印"
    assert payload["rows"][0]["print_status_policy"] == "external_mutable_not_publication_identity"
    assert payload["rows"][0]["receipt_version"] == 1
    conn.execute("UPDATE subscription_orders SET status='approved' WHERE id=1")
    conn.commit()
    assert client.get("/internal/printer/v1/dispatch-contract", params={"store_id": "nanjing", "service_date": "2026-09-16"}, headers=headers).json()["rows"] == []


def test_export_rejects_uid_order_enumeration_unknown_store_and_unbounded_date():
    conn = order_db(); publish_one(conn)
    client = client_for(conn)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/internal/printer/v1/dispatch-contract", params={"store_id": "other", "service_date": "2026-09-16"}, headers=headers).status_code == 404
    assert client.get("/internal/printer/v1/dispatch-contract", params={"store_id": "nanjing", "service_date": "2026-10-01", "user_id": UID}, headers=headers).status_code == 422


def exported_rows(conn):
    response = client_for(conn).get(
        "/internal/printer/v1/dispatch-contract",
        params={"store_id": "nanjing", "service_date": "2026-09-16"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    return response.json()["rows"]


def test_forged_or_legacy_published_row_without_trusted_receipt_is_not_exported():
    conn = order_db()
    conn.execute(
        """INSERT INTO subscription_dispatch_rows
           (dispatch_row_id,order_id,customer_uid,store_id,workbook_id,worksheet_id,
            worksheet_title,service_date,lunch,dinner,menu_version,formalized_at,
            publish_state,created_at,source_payload_json,source_payload_hash)
           VALUES('forged',1,?,'nanjing','runtime-sheet-id',123456789,
                  '王小明_1111_20260915','2026-09-16','fake lunch','fake dinner',
                  'sha256:invented',?,'published',?,'[]','invented')""",
        (UID, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()

    assert exported_rows(conn) == []


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE subscription_dispatch_rows SET customer_uid=?", ("U" + "9" * 32,)),
        ("UPDATE subscription_dispatch_rows SET order_id=99", ()),
        ("UPDATE subscription_dispatch_rows SET workbook_id='foreign-book'", ()),
        ("UPDATE subscription_dispatch_rows SET worksheet_id=987", ()),
        ("UPDATE subscription_dispatch_rows SET worksheet_title='foreign-sheet'", ()),
        ("UPDATE subscription_dispatch_rows SET service_date='2026-09-17'", ()),
        ("UPDATE subscription_dispatch_rows SET menu_version='sha256:invented'", ()),
        ("UPDATE subscription_dispatch_rows SET publish_state='staged'", ()),
        ("UPDATE subscription_dispatch_rows SET source_payload_json='[]'", ()),
        ("UPDATE subscription_dispatch_rows SET source_payload_hash='invented'", ()),
        ("UPDATE subscription_orders SET user_id=?", ("U" + "9" * 32,)),
        ("UPDATE subscription_orders SET status='approved'", ()),
        ("UPDATE subscription_dispatch_publication_receipts SET source_payload_json='[]'", ()),
        ("UPDATE subscription_dispatch_publication_receipts SET receipt_hash='invented'", ()),
        ("UPDATE subscription_dispatch_publication_receipts SET receipt_version=999", ()),
        ("UPDATE subscription_dispatch_publication_receipts SET trusted_writer='legacy-import'", ()),
    ],
)
def test_export_fails_closed_when_any_current_or_receipt_binding_is_tampered(statement, parameters):
    conn = order_db()
    publish_one(conn)
    assert len(exported_rows(conn)) == 1

    if "publication_receipts" in statement:
        # Model offline corruption after removing the normal immutable-write guard.
        conn.execute("DROP TRIGGER subscription_dispatch_receipts_no_update")
    try:
        conn.execute(statement, parameters)
    except sqlite3.IntegrityError:
        # Canonical v2 constraints may reject the corruption before the read gate.
        assert "publication_receipts" in statement
        return
    conn.commit()

    assert exported_rows(conn) == []


def test_receipt_captures_complete_original_payload_and_is_created_only_after_publish():
    conn = order_db()
    tagged = stage(conn)
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_publication_receipts").fetchone()[0] == 0

    publish_schedule(conn, order_id=1, sheet=FakeSheet(), tagged_rows=tagged, now=NOW)

    receipt = conn.execute(
        """SELECT receipt_version,trusted_writer,source_payload_json,receipt_hash
             FROM subscription_dispatch_publication_receipts"""
    ).fetchone()
    assert receipt[0] == 1
    assert receipt[1] == "subscription_dispatch_contract.publish_schedule/v1"
    assert json.loads(receipt[2]) == [str(value) for value in schedule_rows()[1]]
    assert len(receipt[3]) == 64


def test_fresh_export_uses_read_only_sqlite_and_no_sheet_or_network(tmp_path, monkeypatch):
    source = order_db()
    publish_one(source)
    db_path = tmp_path / "dispatch.db"
    with sqlite3.connect(db_path) as target:
        source.backup(target)
    statements = []

    def read_only_connection():
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    app = FastAPI()
    app.include_router(create_dispatch_router(
        connection_factory=read_only_connection,
        export_token=TOKEN,
        workbook_id="runtime-sheet-id",
        now_factory=lambda: NOW,
    ))

    response = TestClient(app).get(
        "/internal/printer/v1/dispatch-contract",
        params={"store_id": "nanjing", "service_date": "2026-09-16"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200
    assert len(response.json()["rows"]) == 1
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_schema_rejects_weakened_v1_without_marking_current_or_mutating():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE subscription_dispatch_schema_versions(version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO subscription_dispatch_schema_versions VALUES(1,'old');
        CREATE TABLE subscription_dispatch_rows(
          dispatch_row_id TEXT, order_id INTEGER, customer_uid TEXT, store_id TEXT,
          workbook_id TEXT, worksheet_id INTEGER, worksheet_title TEXT, service_date TEXT,
          lunch TEXT, dinner TEXT, menu_version TEXT, formalized_at TEXT DEFAULT '',
          publish_state TEXT, created_at TEXT);
        INSERT INTO subscription_dispatch_rows VALUES('legacy',1,'owner','other','book',NULL,'sheet','bad','l','d','v','','evil','old');
    """)
    before = "\n".join(conn.iterdump())
    with pytest.raises(DispatchConflict, match="weakened"):
        ensure_dispatch_schema(conn)
    assert "\n".join(conn.iterdump()) == before
    assert conn.execute("SELECT version FROM subscription_dispatch_schema_versions").fetchall() == [(1,)]


def test_schema_rejects_weak_v1_with_constraint_tokens_only_in_comments_atomically():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE caller_sentinel(value TEXT)")
    conn.execute("INSERT INTO caller_sentinel VALUES('before')")
    conn.executescript("""
        CREATE TABLE subscription_dispatch_schema_versions(version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO subscription_dispatch_schema_versions VALUES(1,'old');
        CREATE TABLE subscription_dispatch_rows(
          dispatch_row_id TEXT, order_id INTEGER, customer_uid TEXT, store_id TEXT,
          workbook_id TEXT, worksheet_id INTEGER, worksheet_title TEXT, service_date TEXT,
          lunch TEXT, dinner TEXT, menu_version TEXT, formalized_at TEXT,
          publish_state TEXT, created_at TEXT
          /* primary key not null
             check(store_id = 'nanjing')
             check(publish_state in ('staged','published','superseded'))
             unique(order_id, menu_version, service_date) */
        );
        INSERT INTO subscription_dispatch_rows VALUES(
          'weak',1,'owner','other','book',NULL,'sheet','bad','l','d','v','','evil','old');
    """)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_sentinel VALUES('during')")
    before = "\n".join(conn.iterdump())

    with pytest.raises(DispatchConflict, match="predecessor"):
        ensure_dispatch_schema(conn)

    assert conn.in_transaction
    assert "\n".join(conn.iterdump()) == before
    assert conn.execute("SELECT version FROM subscription_dispatch_schema_versions").fetchall() == [(1,)]


def test_schema_install_preserves_caller_transaction_and_rolls_back_with_it():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE subscription_orders(id INTEGER PRIMARY KEY, user_id TEXT, status TEXT)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO subscription_orders VALUES(7,'sentinel','pending')")
    ensure_dispatch_schema(conn)
    assert conn.in_transaction
    assert conn.execute("SELECT version FROM subscription_dispatch_schema_versions").fetchone() == (2,)
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM subscription_orders").fetchone() == (0,)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='subscription_dispatch_rows'").fetchone() is None


def test_printer_framework_errors_are_private_and_preserve_method_auth_metadata():
    conn = order_db()
    client = client_for(conn)
    path = "/internal/printer/v1/dispatch-contract"
    invalid = client.get(path, params={"store_id": "nanjing", "service_date": "not-a-date"},
                         headers={"Authorization": f"Bearer {TOKEN}"})
    method = client.post(path)
    auth = client.get(path)
    for response in (invalid, method, auth):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert UID not in response.text and TOKEN not in response.text
    assert invalid.status_code == 422
    assert method.status_code == 405 and method.headers["allow"] == "GET"
    assert auth.status_code == 401 and auth.headers["www-authenticate"] == "Bearer"

    failing_app = FastAPI()
    failing_app.include_router(create_dispatch_router(
        connection_factory=lambda: (_ for _ in ()).throw(RuntimeError("secret-db")),
        export_token=TOKEN, workbook_id="runtime-sheet-id",
        now_factory=lambda: (_ for _ in ()).throw(RuntimeError("secret-clock")),
    ))
    failed = TestClient(failing_app).get(
        path, params={"store_id": "nanjing", "service_date": "2026-09-16"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert failed.status_code == 500
    assert failed.headers["cache-control"] == "no-store"
    assert failed.headers["x-content-type-options"] == "nosniff"
    assert "secret" not in failed.text


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND"])
def test_printer_nonstandard_methods_are_scoped_private_405(method):
    conn = order_db()
    client = client_for(conn)

    response = client.request(method, "/internal/printer/v1/dispatch-contract")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_printer_head_and_options_keep_explicit_private_405_contract(method):
    response = client_for(order_db()).request(
        method, "/internal/printer/v1/dispatch-contract"
    )
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["cache-control"] == "no-store"


def test_exact_v1_migrates_without_inventing_receipts_and_repeated_init_is_stable():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE subscription_orders(id INTEGER PRIMARY KEY, user_id TEXT, status TEXT)")
    conn.executescript("""
      CREATE TABLE subscription_dispatch_schema_versions(version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL);
      INSERT INTO subscription_dispatch_schema_versions VALUES(1,'old');
      CREATE TABLE subscription_dispatch_rows(
        dispatch_row_id TEXT PRIMARY KEY NOT NULL, order_id INTEGER NOT NULL,
        customer_uid TEXT NOT NULL, store_id TEXT NOT NULL CHECK(store_id = 'nanjing'),
        workbook_id TEXT NOT NULL, worksheet_id INTEGER, worksheet_title TEXT NOT NULL,
        service_date TEXT NOT NULL, lunch TEXT NOT NULL, dinner TEXT NOT NULL,
        menu_version TEXT NOT NULL, formalized_at TEXT NOT NULL DEFAULT '',
        publish_state TEXT NOT NULL CHECK(publish_state IN ('staged','published','superseded')),
        created_at TEXT NOT NULL, UNIQUE(order_id, menu_version, service_date));
      INSERT INTO subscription_dispatch_rows VALUES(
        'legacy',1,'owner','nanjing','book',9,'sheet','2026-09-16','l','d','v','old','published','old');
    """)
    ensure_dispatch_schema(conn)
    first = "\n".join(conn.iterdump())
    ensure_dispatch_schema(conn)
    assert "\n".join(conn.iterdump()) == first
    assert conn.execute("SELECT version FROM subscription_dispatch_schema_versions").fetchall() == [(2,)]
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_rows").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_publication_receipts").fetchone() == (0,)
