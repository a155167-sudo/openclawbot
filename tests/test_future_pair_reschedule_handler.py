from __future__ import annotations

import ast
import copy
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import server
from meal_mutation_ledger import ensure_meal_mutation_schema
from minimal_pair_reschedule import (
    MASTER_HEADERS,
    PERSONAL_HEADERS,
    PairMoveConflict,
    execute_future_pair_move,
)

UID = "U-FUTURE-PAIR"
ADMIN = "U-BOUND-ADMIN"
TZ = ZoneInfo("Asia/Taipei")


def personal(day, lunch="無", dinner="無", *, status="待列印", row_id="target-row"):
    has_lunch = lunch not in {"", "無", "尚未安排"}
    has_dinner = dinner not in {"", "無", "尚未安排"}
    return [
        day, "第4週-一", lunch, 500 if has_lunch else 0, 30 if has_lunch else 0,
        dinner, 600 if has_dinner else 0, 35 if has_dinner else 0,
        (500 if has_lunch else 0) + (600 if has_dinner else 0),
        (30 if has_lunch else 0) + (35 if has_dinner else 0),
        "", "$200", "休息", status, row_id, "7", "order-7-v1",
    ]


def master(day, lunch="無", dinner="無"):
    return [
        day, UID, 1800, lunch, dinner, "休息", "1", "subscription", "run", "第4週-一",
        "athlete", "secret-ref", "4", "19:00", "日", "5:30", "", "", "一般",
        "2099-12-31", "0",
    ]


class Sheet:
    def __init__(self, title, sheet_id, rows):
        self.title, self.id, self.rows = title, sheet_id, copy.deepcopy(rows)
        self.reads = 0

    def get_all_values(self):
        self.reads += 1
        return copy.deepcopy(self.rows)


class Book:
    def __init__(self, personal_sheet, master_sheet, *, outcome="accept"):
        self.id = "formal-book"
        self.personal, self.master = personal_sheet, master_sheet
        self.calls, self.outcome = [], outcome

    def worksheet(self, title):
        if title == self.personal.title:
            return self.personal
        if title == self.master.title:
            return self.master
        raise KeyError(title)

    def batch_update(self, body):
        self.calls.append(copy.deepcopy(body))
        staged = {self.personal.id: copy.deepcopy(self.personal.rows), self.master.id: copy.deepcopy(self.master.rows)}
        for request in body["requests"]:
            op = request.get("updateCells") or request.get("appendCells")
            sid = op.get("range", {}).get("sheetId", op.get("sheetId"))
            values = []
            for cell in op["rows"][0]["values"]:
                entered = cell["userEnteredValue"]
                values.append(next(iter(entered.values())))
            if "updateCells" in request:
                staged[sid][op["range"]["startRowIndex"]] = values
            else:
                staged[sid].append(values)
        self.personal.rows, self.master.rows = staged[self.personal.id], staged[self.master.id]
        if self.outcome == "timeout":
            raise TimeoutError("response lost after apply")


def install_db(path: Path):
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
        CREATE TABLE deferred_meals(
          id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, customer_name TEXT DEFAULT '',
          original_date TEXT NOT NULL, original_meal_type TEXT NOT NULL,
          target_date TEXT NOT NULL, target_meal_type TEXT NOT NULL,
          status TEXT NOT NULL, note TEXT DEFAULT '', approved_at TEXT DEFAULT '', approved_by TEXT DEFAULT '');
        CREATE TABLE health_profile(user_id TEXT PRIMARY KEY, summary_text TEXT NOT NULL, sheet_name TEXT);
        CREATE TABLE admin_settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE subscription_orders(
          id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, customer_name TEXT,
          status TEXT NOT NULL, form_payload_json TEXT NOT NULL, formalized_at TEXT NOT NULL);
        CREATE TABLE subscription_menu_entitlements(
          user_id TEXT PRIMARY KEY, order_id INTEGER NOT NULL, status TEXT NOT NULL,
          starts_on TEXT NOT NULL, expires_on TEXT NOT NULL);
        INSERT INTO health_profile VALUES('U-FUTURE-PAIR','before','王小明');
        INSERT INTO admin_settings VALUES('admin_id','U-BOUND-ADMIN');
        INSERT INTO subscription_orders VALUES(
          7,'U-FUTURE-PAIR','王小明','activated',
          '{"user_id":"U-FUTURE-PAIR","name":"王小明","start_date":"2099-10-04","tdee":1800,"goal":"subscription","safe_name":"王小明"}',
          '2099-10-01 10:00:00');
        INSERT INTO subscription_menu_entitlements VALUES(
          'U-FUTURE-PAIR',7,'active','2099-10-01','2099-10-31');
        INSERT INTO deferred_meals VALUES(
          1,'U-FUTURE-PAIR','王小明','2099-10-23','午餐+晚餐','2099-10-26','午餐+晚餐','pending','','','');
        """)
        ensure_meal_mutation_schema(conn)


def fixture(tmp_path, *, outcome="accept", source_status="待列印"):
    db = tmp_path / "handler.sqlite"
    install_db(db)
    p = Sheet("王小明", 10, [PERSONAL_HEADERS, personal("2099-10-23", "午餐A", "晚餐B", status=source_status, row_id="source-row")])
    m = Sheet("Master_API_View", 20, [MASTER_HEADERS, master("2099-10-23", "午餐A", "晚餐B")])
    return db, Book(p, m, outcome=outcome), p, m


def derive_target(_request, _source_personal, _source_master, _schedule_rows, _master_rows):
    return personal("2099-10-26", row_id="derived-target-row"), master("2099-10-26")


def run(db, book, p, m, *, now=None):
    fixed = now or datetime(2099, 10, 20, 12, 0, tzinfo=TZ)
    return execute_future_pair_move(
        db_path=db, request_id=1, owner_user_id=UID, admin_uid=ADMIN,
        event_id="deferred-meal-request:1", now_provider=lambda: fixed,
        book=book, schedule_sheet=p, master_sheet=m,
        expected_workbook_id="formal-book", expected_worksheet_id=10,
        target_row_deriver=derive_target,
    )


def test_full_year_pair_parser_is_additive_and_legacy_parser_is_unchanged():
    assert server.parse_defer_command("#延餐 5/31 午餐 -> 6/2 晚餐") == {
        "original_date": "5/31", "original_meal_type": "午餐",
        "target_date": "6/2", "target_meal_type": "晚餐",
    }
    assert server.parse_defer_command(
        "#延餐 2099-10-23 午餐+晚餐 -> 2099-10-26 午餐+晚餐"
    ) == {
        "original_date": "2099-10-23", "original_meal_type": "午餐+晚餐",
        "target_date": "2099-10-26", "target_meal_type": "午餐+晚餐",
    }


def test_true_handler_appends_future_pair_once_keeps_old_row_numbers_and_no_blank_source_ticket(tmp_path):
    db, book, p, m = fixture(tmp_path)
    original_source_row_number = 2

    result = run(db, book, p, m)

    assert result.kind == "newly_completed"
    assert len(book.calls) == 1 and len(book.calls[0]["requests"]) == 4
    assert len(p.rows) == len(m.rows) == 3
    assert original_source_row_number == 2
    assert p.rows[1][0] == "2099-10-23" and p.rows[1][2:8] == ["無", 0, 0, "無", 0, 0]
    assert p.rows[1][13] == ""  # actual printer cannot emit an all-empty source ticket
    assert p.rows[2][0] == "2099-10-26" and p.rows[2][2:8] == ["午餐A", 500, 30, "晚餐B", 600, 35]
    assert p.rows[2][13] == "待列印"
    assert result.meal_count_before == result.meal_count_after == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=1").fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM meal_mutation_operations").fetchone()[0] == "completed"


def test_existing_admin_approval_entry_creates_both_missing_target_rows(tmp_path, monkeypatch):
    db, book, p, m = fixture(tmp_path)
    # The real subscription generator persists slash dates in both sheets.
    p.rows[1][0] = "2099/10/23"
    m.rows[1][0] = "2099/10/23"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", "formal-book")
    monkeypatch.setattr(server, "gc", type("GC", (), {"open_by_url": lambda _self, _url: book})())
    pushes = []
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda uid, msg: pushes.append((uid, msg.text)))

    reply = server.approve_deferred_meal_request(1, ADMIN)

    assert "已核准延餐申請 #1" in reply
    assert len(book.calls) == 1
    assert p.rows[1][13] == "" and p.rows[2][13] == "待列印"
    assert p.rows[2] == [
        "2099/10/26", "第4週-週一", "午餐A", 500, 30, "晚餐B", 600, 35,
        1100, 65, "", "$200", "", "待列印", "source-row", "7", "order-7-v1",
    ]
    assert m.rows[2] == [
        "2099/10/26", UID, 1800, "午餐A", "晚餐B", "", "1",
        "subscription", "run", "", "athlete", "secret-ref", "4", "19:00",
        "日", "5:30", "", "", "一般", "2099-12-31", "0",
    ]
    assert p.rows[1][14] == ""  # dispatch identity moved; never invented or duplicated
    assert pushes and pushes[0][0] == UID
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT order_id,status,starts_on,expires_on FROM subscription_menu_entitlements WHERE user_id=?",
            (UID,),
        ).fetchone() == (7, "active", "2099-10-01", "2099-10-31")
        assert conn.execute(
            "SELECT status,formalized_at FROM subscription_orders WHERE id=7"
        ).fetchone() == ("activated", "2099-10-01 10:00:00")


def test_formal_approval_rejects_missing_target_outside_service_calendar_without_write(tmp_path, monkeypatch):
    db, book, p, m = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE deferred_meals SET target_date='2099-11-01' WHERE id=1")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", "formal-book")
    monkeypatch.setattr(server, "gc", type("GC", (), {"open_by_url": lambda _self, _url: book})())

    reply = server.approve_deferred_meal_request(1, ADMIN)

    assert "outside the active entitlement service calendar" in reply
    assert book.calls == []
    assert len(p.rows) == len(m.rows) == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=1").fetchone()[0] == "pending"
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 0


@pytest.mark.parametrize("target_exists", [False, True], ids=["missing-target", "existing-target"])
def test_formal_approval_rejects_source_outside_persisted_plan_before_reserve_or_write(
    tmp_path, monkeypatch, target_exists
):
    """The persisted-plan gate applies before either target-row derivation branch."""
    db, book, p, m = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE deferred_meals SET original_date='2099-09-30' WHERE id=1")
    p.rows[1][0] = "2099/09/30"
    m.rows[1][0] = "2099/09/30"
    if target_exists:
        p.rows.append(personal("2099/10/26", row_id="existing-target-row"))
        m.rows.append(master("2099/10/26"))
    before_p, before_m = copy.deepcopy(p.rows), copy.deepcopy(m.rows)
    with sqlite3.connect(db) as conn:
        metadata_before = (
            conn.execute(
                "SELECT order_id,status,starts_on,expires_on FROM subscription_menu_entitlements WHERE user_id=?",
                (UID,),
            ).fetchone(),
            conn.execute(
                "SELECT status,form_payload_json,formalized_at FROM subscription_orders WHERE id=7"
            ).fetchone(),
        )
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", "formal-book")
    monkeypatch.setattr(server, "gc", type("GC", (), {"open_by_url": lambda _self, _url: book})())

    reply = server.approve_deferred_meal_request(1, ADMIN)

    assert "source date is outside the active entitlement service calendar" in reply
    assert book.calls == []
    assert p.rows == before_p and m.rows == before_m
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=1").fetchone()[0] == "pending"
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 0
        metadata_after = (
            conn.execute(
                "SELECT order_id,status,starts_on,expires_on FROM subscription_menu_entitlements WHERE user_id=?",
                (UID,),
            ).fetchone(),
            conn.execute(
                "SELECT status,form_payload_json,formalized_at FROM subscription_orders WHERE id=7"
            ).fetchone(),
        )
    assert metadata_after == metadata_before


@pytest.mark.parametrize(
    "source,entitlement_start,expected_error",
    [
        ("2099-10-04", "2099-10-10", "source date is outside the active entitlement service calendar"),
        ("2099-10-02", "2099-10-01", "source date is outside the activated four-week plan calendar"),
    ],
)
def test_source_must_be_inside_entitlement_and_four_week_plan_independently(
    tmp_path, monkeypatch, source, entitlement_start, expected_error
):
    db, book, p, m = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE deferred_meals SET original_date=? WHERE id=1", (source,))
        conn.execute(
            "UPDATE subscription_menu_entitlements SET starts_on=? WHERE user_id=?",
            (entitlement_start, UID),
        )
    p.rows[1][0] = source.replace("-", "/")
    m.rows[1][0] = source.replace("-", "/")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", "formal-book")
    monkeypatch.setattr(server, "gc", type("GC", (), {"open_by_url": lambda _self, _url: book})())

    reply = server.approve_deferred_meal_request(1, ADMIN)

    assert expected_error in reply
    assert book.calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=1").fetchone()[0] == "pending"
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 0


@pytest.mark.parametrize("source", ["2099-10-04", "2099-10-31"], ids=["plan-first-day", "plan-last-day"])
def test_source_on_four_week_plan_boundaries_remains_approved(tmp_path, monkeypatch, source):
    db, book, p, m = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE deferred_meals SET original_date=? WHERE id=1", (source,))
        metadata_before = conn.execute(
            "SELECT form_payload_json FROM subscription_orders WHERE id=7"
        ).fetchone()[0]
    p.rows[1][0] = source.replace("-", "/")
    m.rows[1][0] = source.replace("-", "/")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", "formal-book")
    monkeypatch.setattr(server, "gc", type("GC", (), {"open_by_url": lambda _self, _url: book})())
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda *_args, **_kwargs: None)

    reply = server.approve_deferred_meal_request(1, ADMIN)

    assert "已核准延餐申請 #1" in reply
    assert len(book.calls) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=1").fetchone()[0] == "completed"
        assert conn.execute("SELECT form_payload_json FROM subscription_orders WHERE id=7").fetchone()[0] == metadata_before


def test_replay_confirmed_does_zero_sheet_io_and_never_moves_twice(tmp_path):
    db, book, p, m = fixture(tmp_path)
    first = run(db, book, p, m)
    calls, reads = len(book.calls), (p.reads, m.reads)

    second = run(db, book, p, m)

    assert first.kind == "newly_completed" and second.kind == "replay_completed"
    assert len(book.calls) == calls == 1
    assert (p.reads, m.reads) == reads


def test_timeout_is_durable_unknown_and_repeat_has_zero_external_io(tmp_path):
    db, book, p, m = fixture(tmp_path, outcome="timeout")
    first = run(db, book, p, m)
    calls, reads = len(book.calls), (p.reads, m.reads)
    second = run(db, book, p, m)

    assert first.kind == second.kind == "outcome_unknown"
    assert len(book.calls) == calls == 1
    assert (p.reads, m.reads) == reads
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM meal_mutation_operations").fetchone()[0] == "outcome_unknown"
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=1").fetchone()[0] == "pending"


@pytest.mark.parametrize("source,target", [("2099-10-20", "2099-10-26"), ("2099-10-23", "2099-10-20")])
def test_today_or_past_endpoint_is_rejected_before_sheet_io(tmp_path, source, target):
    db, book, p, m = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE deferred_meals SET original_date=?,target_date=? WHERE id=1", (source, target))
    with pytest.raises(PairMoveConflict, match="strictly future"):
        run(db, book, p, m, now=datetime(2099, 10, 20, 12, 0, tzinfo=TZ))
    assert book.calls == [] and (p.reads, m.reads) == (0, 0)


def test_printed_source_is_rejected_without_write(tmp_path):
    db, book, p, m = fixture(tmp_path, source_status="已送印")
    with pytest.raises(PairMoveConflict, match="printed"):
        run(db, book, p, m)
    assert book.calls == []


def test_unknown_source_print_state_is_rejected_without_write(tmp_path):
    db, book, p, m = fixture(tmp_path, source_status="列印狀態不明")
    with pytest.raises(PairMoveConflict, match="print status"):
        run(db, book, p, m)
    assert book.calls == []


def test_actual_1030_reader_ignores_future_but_reads_moved_row_on_target_day(tmp_path):
    source = (Path(__file__).resolve().parent / "fixtures" / "legacy_nanjing_pending_reader.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants = {
        "PENDING_STATUS", "PRINTED_STATUSES", "ABSENT_MEALS", "COL_DATE",
        "COL_LUNCH", "COL_DINNER", "COL_PRINT_STATUS", "EXPECTED_HEADER_PREFIX",
        "EXPECTED_HEADER_STATUS",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_norm", "_norm_date", "_is_absent", "get_pending_orders"
        }:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants for target in node.targets
        ):
            selected.append(node)
    ns = {"datetime": None}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "lf5167-ast", "exec"), ns)

    class Clock:
        day = "2099-10-23"
        @classmethod
        def now(cls, _tz):
            return datetime.fromisoformat(cls.day + "T10:30:00+08:00")

    p = Sheet("王小明_owner", 10, [PERSONAL_HEADERS, personal("2099-10-23", status=""), personal("2099-10-26", "午餐A", "晚餐B")])
    class W:
        def worksheets(self): return [p]
    ns["datetime"] = Clock
    ns["TAIPEI"] = TZ
    ns["_connect_workbook"] = lambda: W()

    assert ns["get_pending_orders"]()["orders"] == []
    Clock.day = "2099-10-26"
    orders = ns["get_pending_orders"]()["orders"]
    assert len(orders) == 1 and orders[0]["rowNumber"] == 3
