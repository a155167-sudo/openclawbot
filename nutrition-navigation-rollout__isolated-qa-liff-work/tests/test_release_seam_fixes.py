import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import server
from meal_mutation_ledger import MealMutationBinding, reserve_meal_mutation
from subscription_dispatch_contract import (
    DispatchConflict,
    ORIGINAL_HEADERS,
    publish_schedule,
    stage_schedule,
)


class MutationSheet:
    def __init__(self, rows):
        self.rows = rows
        self.id = 42
        self.title = "customer"
        self.updates = []

    def get_all_values(self):
        return self.rows


class MutationBook:
    def __init__(self, sheet):
        self.sheet = sheet
        self.batch_calls = []

    def worksheet(self, _name):
        return self.sheet

    def batch_update(self, body):
        self.batch_calls.append(body)
        raise AssertionError("published meals must be rejected before every Sheet write")


class MutationGC:
    def __init__(self, book):
        self.book = book

    def open_by_url(self, _url):
        return self.book


class DispatchSheet:
    def __init__(self):
        self.id = 42
        self.title = "customer"
        self.rows = []

    def replace_schedule(self, rows):
        self.rows = [list(row) for row in rows]
        return self.rows

    def read_schedule(self):
        return [list(row) for row in self.rows]


def _menu_row(day, lunch="餐A", dinner="餐B"):
    row = [""] * 14
    row[1] = day
    row[2] = lunch
    row[5] = dinner
    return row


def _dispatch_row(day, lunch="餐A", dinner="餐B"):
    row = [""] * 14
    row[0] = day
    row[2] = lunch
    for column in (3, 4, 6, 7, 8, 9):
        row[column] = 0
    row[5] = dinner
    row[13] = "待列印"
    return row


def _setup(tmp_path, monkeypatch, *, published_days):
    db_path = tmp_path / "release-seam.db"
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    uid = "U" + "p" * 32
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,sheet_name,summary_text) VALUES(?,?,?,?)",
            (uid, "測試會員", "customer", "before"),
        )
        conn.execute(
            "INSERT INTO subscription_orders(user_id,status) VALUES(?,'activated')", (uid,)
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if published_days:
            rows = [ORIGINAL_HEADERS] + [_dispatch_row(day) for day in published_days]
            tagged = stage_schedule(
                conn,
                order_id=order_id,
                snapshot_uid=uid,
                workbook_id=server.SPREADSHEET_ID,
                worksheet_title="customer",
                schedule_rows=rows,
                now=datetime(2099, 9, 1, tzinfo=timezone.utc),
                id_factory=iter([f"dispatch-{i}" for i in range(len(published_days))]).__next__,
            )
            publish_schedule(
                conn,
                order_id=order_id,
                sheet=DispatchSheet(),
                tagged_rows=tagged,
                now=datetime(2099, 9, 1, tzinfo=timezone.utc),
            )
        conn.commit()
    sheet = MutationSheet([
        _menu_row("2099/10/01（週四）", "餐A", "餐B"),
        _menu_row("2099/10/02（週五）", "", "餐D"),
    ])
    book = MutationBook(sheet)
    monkeypatch.setattr(server, "gc", MutationGC(book))
    monkeypatch.setattr(server, "get_subscription_menu_access", lambda _uid: ("active", "menu", 10, "2099-10-31"))
    return uid, db_path, sheet, book


def _db_evidence(db_path):
    with sqlite3.connect(db_path) as conn:
        receipt = conn.execute(
            "SELECT source_payload_json,receipt_hash FROM subscription_dispatch_publication_receipts ORDER BY receipt_id"
        ).fetchall()
        food_logs = conn.execute("SELECT count(*) FROM food_logs").fetchone()[0]
        operations = conn.execute("SELECT count(*) FROM meal_mutation_operations").fetchone()[0]
        summary = conn.execute("SELECT summary_text FROM health_profile").fetchone()[0]
    return receipt, food_logs, operations, summary


def test_display_name_provider_failure_log_is_fixed_and_contains_no_owned_pii(monkeypatch, capsys):
    uid = "U-REVIEW-OWNED-PII-SENTINEL"
    secret = "REVIEW-OWNED-TOKEN-SENTINEL"
    address = "REVIEW-OWNED-ADDRESS-SENTINEL"
    monkeypatch.setattr(
        server.line_bot_api,
        "get_profile",
        lambda _uid: (_ for _ in ()).throw(RuntimeError(f"{secret} {address}")),
    )

    assert server.get_line_display_name_safe(uid) == ""

    output = capsys.readouterr().out
    assert output == "⚠️ line_display_name_lookup status=failed\n"
    assert uid not in output
    assert uid[:8] not in output
    assert secret not in output
    assert address not in output


def test_swap_rejects_when_source_is_published_before_any_sheet_or_business_write(tmp_path, monkeypatch):
    uid, db_path, sheet, book = _setup(tmp_path, monkeypatch, published_days=["2099/10/01"])
    before = _db_evidence(db_path)

    result = server.execute_meal_swap(
        uid, "2099/10/01", "午餐", "2099/10/02", "晚餐",
        operation_id=f"line-ai:{uid}:published-source",
    )

    assert str(result) == "⚠️ 餐點已發布出單，請聯絡店家。"
    assert book.batch_calls == []
    assert sheet.updates == []
    assert _db_evidence(db_path) == before


def test_defer_rejects_when_target_is_published_without_changing_request_foodlog_or_receipt(tmp_path, monkeypatch):
    uid, db_path, sheet, book = _setup(tmp_path, monkeypatch, published_days=["2099/10/02"])
    request_id = server.create_deferred_meal_request(
        uid, "測試會員", "2099/10/01", "午餐", "2099/10/02", "午餐", False, False
    )
    before = _db_evidence(db_path)
    with sqlite3.connect(db_path) as conn:
        request_before = conn.execute("SELECT * FROM deferred_meals WHERE id=?", (request_id,)).fetchone()

    ok, message = server.execute_deferred_meal_move(
        uid, "2099/10/01", "午餐", "2099/10/02", "午餐",
        request_id=str(request_id), admin_uid="U-ADMIN",
    )

    assert not ok
    assert str(message) == "⚠️ 餐點已發布出單，請聯絡店家。"
    assert book.batch_calls == []
    assert sheet.updates == []
    assert _db_evidence(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT * FROM deferred_meals WHERE id=?", (request_id,)).fetchone() == request_before


def test_unpublished_swap_remains_available(tmp_path, monkeypatch):
    uid, db_path, sheet, book = _setup(tmp_path, monkeypatch, published_days=[])
    book.batch_update = lambda body: book.batch_calls.append(body)
    monkeypatch.setattr(server, "complete_meal_mutation", lambda *_a, **_k: SimpleNamespace(kind="newly_completed"))

    result = server.execute_meal_swap(
        uid, "2099/10/01", "午餐", "2099/10/02", "晚餐",
        operation_id=f"line-ai:{uid}:unpublished",
    )

    assert "成功" in result
    assert len(book.batch_calls) == 1


def test_publication_refuses_active_meal_mutation_before_sheet_write(tmp_path, monkeypatch):
    uid, db_path, _sheet, _book = _setup(tmp_path, monkeypatch, published_days=[])
    with sqlite3.connect(db_path) as conn:
        order_id = conn.execute(
            "SELECT id FROM subscription_orders WHERE user_id=?", (uid,)
        ).fetchone()[0]
        rows = [ORIGINAL_HEADERS, _dispatch_row("2099/10/01")]
        tagged = stage_schedule(
            conn,
            order_id=order_id,
            snapshot_uid=uid,
            workbook_id=server.SPREADSHEET_ID,
            worksheet_title="customer",
            schedule_rows=rows,
            now=datetime(2099, 9, 1, tzinfo=timezone.utc),
            id_factory=lambda: "dispatch-race",
        )
        conn.commit()
    binding = MealMutationBinding(
        operation_id=f"line-ai:{uid}:race",
        event_id="race",
        owner_user_id=uid,
        purpose="swap",
        request_id="",
        payload={"d1": "2099/10/01", "m1": "午餐", "d2": "2099/10/02", "m2": "晚餐"},
        spreadsheet_id=server.SPREADSHEET_ID,
        worksheet_id=42,
        worksheet_name="customer",
        cells=({"row_idx": 1, "col_idx": 3}, {"row_idx": 2, "col_idx": 6}),
        before=("餐A", "餐D"),
        after=("餐D", "餐A"),
    )
    assert reserve_meal_mutation(db_path, binding).kind == "claimed"
    dispatch_sheet = DispatchSheet()

    with sqlite3.connect(db_path) as conn, pytest.raises(DispatchConflict, match="meal mutation"):
        publish_schedule(
            conn,
            order_id=order_id,
            sheet=dispatch_sheet,
            tagged_rows=tagged,
            now=datetime(2099, 9, 1, tzinfo=timezone.utc),
        )

    assert dispatch_sheet.rows == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM subscription_dispatch_publication_receipts"
        ).fetchone()[0] == 0


def test_publication_transaction_serializes_racing_mutation_until_receipt_commit(tmp_path, monkeypatch):
    uid, db_path, _sheet, _book = _setup(tmp_path, monkeypatch, published_days=[])
    with sqlite3.connect(db_path) as conn:
        order_id = conn.execute(
            "SELECT id FROM subscription_orders WHERE user_id=?", (uid,)
        ).fetchone()[0]
        tagged = stage_schedule(
            conn,
            order_id=order_id,
            snapshot_uid=uid,
            workbook_id=server.SPREADSHEET_ID,
            worksheet_title="customer",
            schedule_rows=[ORIGINAL_HEADERS, _dispatch_row("2099/10/01")],
            now=datetime(2099, 9, 1, tzinfo=timezone.utc),
            id_factory=lambda: "dispatch-concurrent",
        )
        conn.commit()
    binding = MealMutationBinding(
        operation_id=f"line-ai:{uid}:concurrent",
        event_id="concurrent",
        owner_user_id=uid,
        purpose="swap",
        request_id="",
        payload={"d1": "2099/10/01", "m1": "午餐", "d2": "2099/10/02", "m2": "晚餐"},
        spreadsheet_id=server.SPREADSHEET_ID,
        worksheet_id=42,
        worksheet_name="customer",
        cells=({"row_idx": 1, "col_idx": 3}, {"row_idx": 2, "col_idx": 6}),
        before=("餐A", "餐D"),
        after=("餐D", "餐A"),
    )
    started = threading.Event()
    results = []

    def reserve_racer():
        started.set()
        results.append(reserve_meal_mutation(
            db_path,
            binding,
            publication_slots=(("2099-10-01", "午餐"), ("2099-10-02", "晚餐")),
        ))

    class RacingDispatchSheet(DispatchSheet):
        def replace_schedule(self, rows):
            self.thread = threading.Thread(target=reserve_racer)
            self.thread.start()
            assert started.wait(1)
            time.sleep(0.05)
            assert self.thread.is_alive(), "mutation reservation must wait behind publication"
            return super().replace_schedule(rows)

    dispatch_sheet = RacingDispatchSheet()
    conn = sqlite3.connect(db_path)
    try:
        publish_schedule(
            conn,
            order_id=order_id,
            sheet=dispatch_sheet,
            tagged_rows=tagged,
            now=datetime(2099, 9, 1, tzinfo=timezone.utc),
        )
        assert dispatch_sheet.thread.is_alive()
        conn.commit()
        dispatch_sheet.thread.join(2)
    finally:
        conn.close()

    assert not dispatch_sheet.thread.is_alive()
    assert [result.kind for result in results] == ["published_blocked"]
    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT count(*) FROM meal_mutation_operations").fetchone()[0] == 0
        assert check.execute(
            "SELECT count(*) FROM subscription_dispatch_publication_receipts"
        ).fetchone()[0] == 1
