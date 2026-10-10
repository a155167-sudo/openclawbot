import json
import os
import socket
import sqlite3
from datetime import datetime, timezone

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server
from subscription_dispatch_contract import (
    DISPATCH_HEADERS,
    ORIGINAL_HEADERS,
    ensure_dispatch_schema,
    publish_schedule,
    stage_schedule,
)


UID = "U" + "a" * 32


class _Worksheet:
    id = 123
    title = "customer-sheet"

    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.clear_calls = 0

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def clear(self):
        self.clear_calls += 1
        raise AssertionError("incremental adapter must not clear")

    def update(self, values, range_name=None):
        assert range_name is not None
        row_number = int(range_name.split(":", 1)[0][1:])
        self.rows[row_number - 1] = list(values[0])

    def append_rows(self, rows):
        self.rows.extend([list(row) for row in rows])

    def delete_rows(self, _row_number):
        raise AssertionError("incremental upsert must not delete old rows")


class _AtomicSpreadsheet:
    """Fake the documented all-or-nothing spreadsheets.batchUpdate boundary."""

    def __init__(self, worksheet, *, fail_before_apply=False, timeout_after_apply=False):
        self.worksheet = worksheet
        self.fail_before_apply = fail_before_apply
        self.timeout_after_apply = timeout_after_apply
        self.calls = []

    def batch_update(self, body):
        self.calls.append(body)
        if self.fail_before_apply:
            raise TimeoutError("atomic request rejected before apply")
        candidate = [list(row) for row in self.worksheet.rows]
        for request in body["requests"]:
            operation = request.get("updateCells") or request.get("appendCells")
            assert operation is not None
            decoded = []
            for row in operation["rows"]:
                decoded.append([
                    next(iter(cell["userEnteredValue"].values()), "")
                    if cell.get("userEnteredValue") else ""
                    for cell in row["values"]
                ])
            if "updateCells" in request:
                start = operation["range"]["startRowIndex"]
                for offset, row in enumerate(decoded):
                    candidate[start + offset] = row
            else:
                candidate.extend(decoded)
        self.worksheet.rows = candidate
        if self.timeout_after_apply:
            self.timeout_after_apply = False
            raise TimeoutError("ambiguous response after atomic apply")
        return {"replies": [{} for _ in body["requests"]]}


class _AtomicWorksheet(_Worksheet):
    def __init__(self, rows, **failure):
        super().__init__(rows)
        self.spreadsheet = _AtomicSpreadsheet(self, **failure)

    def update(self, values, range_name=None):
        raise AssertionError("production dispatch must not perform per-row values.update")

    def append_rows(self, rows):
        raise AssertionError("production dispatch must not append outside spreadsheets.batchUpdate")


def test_production_sheet_adapter_upserts_by_dispatch_id_without_clear_or_unknown_loss():
    header = ORIGINAL_HEADERS + DISPATCH_HEADERS
    old = ["2026/09/16", "週三", "舊午餐", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "已列印", "dispatch-1", "1", "order-1-v1"]
    unknown = ["人工歷史列", "保留"]
    worksheet = _AtomicWorksheet([["既有檔案"], header, old, unknown])
    incoming = [header, ["2026/09/16", "週三", "新午餐", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "待列印", "dispatch-1", "1", "order-1-v1"]]
    adapter = server._SubscriptionDispatchSheetAdapter(worksheet, [["新檔案"]], [["追蹤"]], 1)

    effective = adapter.replace_schedule(incoming)

    assert worksheet.clear_calls == 0
    assert ["既有檔案"] in worksheet.rows
    assert unknown in worksheet.rows
    assert effective[1][2] == "新午餐"
    assert effective[1][13] == "已列印"
    assert adapter.read_schedule() == effective


def test_production_adapter_atomic_batch_failure_preserves_all_preexisting_rows():
    header = ORIGINAL_HEADERS + DISPATCH_HEADERS
    old_one = ["2026/09/16", "週三", "舊一", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "已列印", "dispatch-1", "1", "order-1-v1"]
    old_two = ["2026/09/17", "週四", "舊二", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "待列印", "dispatch-2", "1", "order-1-v1"]
    original = [["既有檔案"], header, old_one, old_two, ["人工歷史列", "保留"]]
    worksheet = _AtomicWorksheet(original, fail_before_apply=True)
    incoming = [header, list(old_one), list(old_two)]
    incoming[1][2] = "新一"
    incoming[2][2] = "新二"
    adapter = server._SubscriptionDispatchSheetAdapter(worksheet, [], [], 2)

    with pytest.raises(TimeoutError):
        adapter.replace_schedule(incoming)

    assert worksheet.rows == original
    assert len(worksheet.spreadsheet.calls) == 1


def test_atomic_batch_timeout_after_apply_exact_retry_does_not_append_duplicates():
    header = ORIGINAL_HEADERS + DISPATCH_HEADERS
    old_one = ["2026/09/16", "週三", "舊一", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "", "dispatch-1", "1", "order-1-v1"]
    old_two = ["2026/09/17", "週四", "舊二", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "", "dispatch-2", "1", "order-1-v1"]
    worksheet = _AtomicWorksheet([["既有檔案"], header, old_one, old_two], timeout_after_apply=True)
    incoming = [header, list(old_one), list(old_two)]
    incoming[1][2] = "新一"
    incoming[2][2] = "新二"

    first = server._SubscriptionDispatchSheetAdapter(worksheet, [], [], 2)
    with pytest.raises(TimeoutError):
        first.replace_schedule(incoming)
    assert [row[2] for row in worksheet.rows if len(row) > 14 and str(row[14]).startswith("dispatch-")] == ["新一", "新二"]

    retry = server._SubscriptionDispatchSheetAdapter(worksheet, [], [], 2)
    expected = retry.replace_schedule(incoming)
    assert retry.read_schedule() == expected
    assert [row[14] for row in worksheet.rows if len(row) > 14 and str(row[14]).startswith("dispatch-")] == ["dispatch-1", "dispatch-2"]


def test_ambiguous_atomic_sheet_timeout_keeps_receipts_unpublished_until_exact_retry():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, formalized_at TEXT DEFAULT '')")
    conn.execute("INSERT INTO subscription_orders VALUES(1,?,'activated','')", (UID,))
    ensure_dispatch_schema(conn)
    schedule = [
        ORIGINAL_HEADERS,
        ["2026/09/16", "週三", "新一", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", ""],
        ["2026/09/17", "週四", "新二", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", ""],
    ]
    ids = iter(["dispatch-1", "dispatch-2"])
    tagged = stage_schedule(
        conn, order_id=1, snapshot_uid=UID, workbook_id="workbook",
        worksheet_title="customer-sheet", schedule_rows=schedule,
        now=datetime(2026, 9, 15, tzinfo=timezone.utc), id_factory=ids.__next__,
    )
    worksheet = _AtomicWorksheet(
        [["既有檔案"], tagged[0], list(tagged[1]), list(tagged[2])],
        timeout_after_apply=True,
    )

    with pytest.raises(RuntimeError, match="publish/readback failed"):
        publish_schedule(
            conn, order_id=1,
            sheet=server._SubscriptionDispatchSheetAdapter(worksheet, [], [], 2),
            tagged_rows=tagged, now=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
    assert conn.execute("SELECT DISTINCT publish_state FROM subscription_dispatch_rows").fetchall() == [("staged",)]
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_publication_receipts").fetchone() == (0,)

    publish_schedule(
        conn, order_id=1,
        sheet=server._SubscriptionDispatchSheetAdapter(worksheet, [], [], 2),
        tagged_rows=tagged, now=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    assert conn.execute("SELECT DISTINCT publish_state FROM subscription_dispatch_rows").fetchall() == [("published",)]
    assert conn.execute("SELECT count(*) FROM subscription_dispatch_publication_receipts").fetchone() == (2,)
    assert [row[14] for row in worksheet.rows if len(row) > 14 and str(row[14]).startswith("dispatch-")] == ["dispatch-1", "dispatch-2"]


def test_master_api_retry_upserts_by_user_and_date_without_delete():
    header = ["Date", "User_ID", "TDEE"]
    old = ["2026-09-16", UID, "1000"]
    unknown = ["2026-09-01", "someone-else", "900"]
    worksheet = _Worksheet([header, old, unknown])

    server._upsert_master_api_rows(worksheet, UID, [["2026-09-16", UID, "1200"]])

    assert worksheet.rows == [header, ["2026-09-16", UID, "1200"], unknown]


def _install_activation_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activation.db"
    snapshot = {"user_id": UID, "schedule_sheet_rows": []}
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER DEFAULT 0)")
        conn.execute("""CREATE TABLE subscription_orders (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, customer_name TEXT,
            meal_count INTEGER, quote_low_total INTEGER, quote_high_total INTEGER,
            status TEXT, form_payload_json TEXT, activated_at TEXT, vip_code TEXT,
            approved_at TEXT, approved_by TEXT, admin_note TEXT, formalized_at TEXT DEFAULT ''
        )""")
        conn.execute(
            "INSERT INTO subscription_orders VALUES(1,?,?,?,?,?,'approved',?,'','','','','','')",
            (UID, "測試客戶", 48, 1000, 1000, json.dumps(snapshot)),
        )
    pushes = []
    formalize_calls = []
    outcomes = iter([(False, "fake Sheet timeout"), (True, "recovered")])
    monkeypatch.setattr(server, "DB_PATH", str(db_path))

    def fake_formalize(oid, payload):
        formalize_calls.append((oid, payload))
        result = next(outcomes)
        if result[0]:
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE subscription_orders SET formalized_at='done' WHERE id=?", (oid,))
        return result

    monkeypatch.setattr(server, "formalize_subscription_snapshot", fake_formalize)
    monkeypatch.setattr(
        server.line_bot_api, "push_message",
        lambda uid, message, **kwargs: pushes.append((uid, message.text, kwargs.get("retry_key"))),
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden")))
    return db_path, pushes, formalize_calls


def test_formalize_failure_is_not_success_and_never_pushes_code(tmp_path, monkeypatch):
    db_path, pushes, formalize_calls = _install_activation_db(tmp_path, monkeypatch)

    first_ok, first_message = server.update_subscription_order_status(1, "activated", "admin")

    assert first_ok is False
    assert "尚未完成" in first_message
    with sqlite3.connect(db_path) as conn:
        order_code = conn.execute("SELECT vip_code FROM subscription_orders WHERE id=1").fetchone()[0]
        codes = conn.execute("SELECT code FROM vips").fetchall()
    assert codes == [(order_code,)]
    assert len(formalize_calls) == 1
    assert pushes == []
    assert order_code not in first_message


def test_existing_code_is_preserved_when_activation_reconcile_later_completes(tmp_path, monkeypatch):
    db_path, pushes, formalize_calls = _install_activation_db(tmp_path, monkeypatch)
    first_ok, _ = server.update_subscription_order_status(1, "activated", "admin")
    with sqlite3.connect(db_path) as conn:
        original_code = conn.execute("SELECT vip_code FROM subscription_orders WHERE id=1").fetchone()[0]

    second_ok, _ = server.update_subscription_order_status(1, "activated", "admin")

    assert first_ok is False
    assert second_ok is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT vip_code FROM subscription_orders WHERE id=1").fetchone()[0] == original_code
        assert conn.execute("SELECT count(*) FROM vips").fetchone()[0] == 1
    assert len(formalize_calls) == 2
    assert len(pushes) == 1
    assert pushes[0][0] == UID
    assert original_code in pushes[0][1]
    assert pushes[0][2]

    third_ok, _ = server.update_subscription_order_status(1, "activated", "admin")
    assert third_ok is True
    assert len(pushes) == 1


def test_activation_success_notification_unknown_outcome_stops_automatic_retry(tmp_path, monkeypatch):
    db_path, _pushes, _ = _install_activation_db(tmp_path, monkeypatch)
    calls = []

    def unknown_push(uid, message, **kwargs):
        calls.append((uid, message.text, kwargs.get("retry_key")))
        raise TimeoutError("unknown provider outcome")

    monkeypatch.setattr(server.line_bot_api, "push_message", unknown_push)
    assert server.update_subscription_order_status(1, "activated", "admin")[0] is False
    assert server.update_subscription_order_status(1, "activated", "admin")[0] is False
    assert server.update_subscription_order_status(1, "activated", "admin")[0] is False
    assert len(calls) == 1
    assert calls[0][0] == UID
    assert "專屬排餐與正式試算表已同步建立" in calls[0][1]
    assert calls[0][2]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM subscription_activation_notifications WHERE order_id=1"
        ).fetchone() == ("unknown",)


def test_formalize_published_retry_returns_before_any_google_or_profile_write(tmp_path, monkeypatch):
    db_path = tmp_path / "published-retry.db"
    title = "customer-sheet"
    schedule = [ORIGINAL_HEADERS, ["2026/09/16", "週三", "午餐", 1, 2, "晚餐", 3, 4, 5, 6, "", "$1", "", "待列印"]]
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, formalized_at TEXT DEFAULT '')")
        conn.execute("INSERT INTO subscription_orders VALUES(1,?,'activated','done')", (UID,))
        ensure_dispatch_schema(conn)
        tagged = stage_schedule(
            conn,
            order_id=1,
            snapshot_uid=UID,
            workbook_id="workbook",
            worksheet_title=title,
            schedule_rows=schedule,
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
            id_factory=lambda: "dispatch-1",
        )

        class ReceiptSheet:
            id = 123
            title = "customer-sheet"

            def replace_schedule(self, rows):
                self.rows = [list(row) for row in rows]
                return self.rows

            def read_schedule(self):
                return self.rows

        publish_schedule(
            conn,
            order_id=1,
            sheet=ReceiptSheet(),
            tagged_rows=tagged,
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
    class NoGoogle:
        def open_by_url(self, _url):
            raise AssertionError("published retry touched Google")
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "SPREADSHEET_ID", "workbook")
    monkeypatch.setattr(server, "gc", NoGoogle())

    ok, message = server.formalize_subscription_snapshot(1, {
        "user_id": UID,
        "safe_name": title,
        "schedule_sheet_rows": schedule,
    })

    assert ok is True
    assert "未重寫 Sheet" in message
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='health_profile'").fetchone() is None