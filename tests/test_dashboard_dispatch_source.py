from __future__ import annotations

import copy
import json
import sqlite3

import pytest

import dashboard_dispatch_source as source
from dashboard_dispatch_source import read_dispatch_day
from subscription_dispatch_contract import TRUSTED_WRITER, _canonical_json, _receipt_hash, _sha256_text


BASE_HEADERS = [
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
]
HEADERS = BASE_HEADERS + ["Dispatch_Row_ID", "Order_ID", "Menu_Version"]


class Worksheet:
    def __init__(self, title="會員專頁", sheet_id=101, values=None):
        self.title = title
        self.id = sheet_id
        self._values = values or []
        self.reads = 0

    def get_all_values(self):
        self.reads += 1
        return copy.deepcopy(self._values)


class Book:
    def __init__(self, worksheet, book_id="book-1"):
        self.id = book_id
        self._worksheet = worksheet
        self.lookups = []

    def worksheet(self, title):
        self.lookups.append(title)
        if title != self._worksheet.title:
            raise LookupError(title)
        return self._worksheet


def row(date="2026/10/26", lunch="無骨雞", lunch_kcal="650.5", lunch_protein="42",
        dinner="鮭魚", dinner_kcal="741", dinner_protein="45", status="已送印",
        dispatch_id="dispatch-26", order_id="order-7", menu_version="menu-v1"):
    return [date, "第4週-一", lunch, lunch_kcal, lunch_protein, dinner, dinner_kcal,
            dinner_protein, "1391.5", "87", "", "", "", status, dispatch_id,
            order_id, menu_version]


def call(book, db_path, **overrides):
    args = dict(book=book, db_path=str(db_path), user_id="U-OWNER", sheet_name="會員專頁",
                workbook_id="book-1", service_date="2026-10-26")
    args.update(overrides)
    return read_dispatch_day(**args)


def test_exact_owner_marker_reads_live_sheet_once_and_keeps_printed_meals(tmp_path):
    ws = Worksheet(values=[
        ["【VIP 客戶檔案】", "姓名: 測試", "User_ID: U-OWNER"],
        HEADERS,
        row(),
    ])
    result = call(Book(ws), tmp_path / "missing.db")

    assert result == {
        "status": "ok",
        "reason": "",
        "source": "printer_personal_worksheet",
        "meals": [
            {"slot": "午餐", "name": "無骨雞", "kcal": 650.5, "protein": 42.0,
             "subscription_meal_id": "dispatch-26:午餐"},
            {"slot": "晚餐", "name": "鮭魚", "kcal": 741.0, "protein": 45.0,
             "subscription_meal_id": "dispatch-26:晚餐"},
        ],
    }
    assert ws.reads == 1


@pytest.mark.parametrize("headers, data_row", [
    (BASE_HEADERS, row()[:14]),
    (HEADERS, row()),
    (HEADERS + ["", ""], row() + ["", ""]),
])
def test_exact_14_or_17_column_schema_accepts_google_trailing_empty_padding(
        tmp_path, headers, data_row):
    values = [["User_ID: U-OWNER"], headers, data_row]

    result = call(db_book(values), tmp_path / "none.db")

    assert result["status"] == "ok"
    assert [meal["name"] for meal in result["meals"]] == ["無骨雞", "鮭魚"]


@pytest.mark.parametrize("headers", [
    BASE_HEADERS[:3] + ["錯誤午餐熱量"] + BASE_HEADERS[4:],
    BASE_HEADERS + ["Dispatch_Row_ID", "Order_ID", "錯誤尾欄"],
    BASE_HEADERS + ["Dispatch_Row_ID"],
])
def test_malformed_middle_tail_or_15_column_schema_fails_closed(tmp_path, headers):
    values = [["User_ID: U-OWNER"], headers, row()]

    result = call(db_book(values), tmp_path / "none.db")

    assert result["status"] == "unavailable"
    assert result["meals"] == []


def seed_binding(path, *, user_id="U-OWNER", workbook_id="book-1", worksheet_id=101,
                 worksheet_title="會員專頁"):
    schedule = row()[:14]
    payload = _canonical_json(schedule)
    payload_hash = _sha256_text(payload)
    published_at = "2026-10-01T12:00:00+08:00"
    dispatch_id = "dispatch-26"
    receipt_values = (
        1, TRUSTED_WRITER, dispatch_id, 7, user_id, "nanjing", workbook_id,
        worksheet_id, worksheet_title, "2026-10-26", "menu-v1", payload,
        payload_hash, published_at,
    )
    receipt_hash = _receipt_hash(receipt_values)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE subscription_orders(id INTEGER PRIMARY KEY,user_id TEXT,status TEXT);
            CREATE TABLE subscription_dispatch_rows(
                dispatch_row_id TEXT PRIMARY KEY,order_id INTEGER,customer_uid TEXT,store_id TEXT,
                workbook_id TEXT,worksheet_id INTEGER,worksheet_title TEXT,service_date TEXT,
                lunch TEXT,dinner TEXT,menu_version TEXT,formalized_at TEXT,publish_state TEXT,
                created_at TEXT,source_payload_json TEXT,source_payload_hash TEXT);
            CREATE TABLE subscription_dispatch_publication_receipts(
                receipt_id TEXT,receipt_version INTEGER,trusted_writer TEXT,dispatch_row_id TEXT,
                order_id INTEGER,customer_uid TEXT,store_id TEXT,workbook_id TEXT,
                worksheet_id INTEGER,worksheet_title TEXT,service_date TEXT,menu_version TEXT,
                source_payload_json TEXT,source_payload_hash TEXT,published_at TEXT,receipt_hash TEXT);
        """)
        conn.execute("INSERT INTO subscription_orders VALUES (7,?, 'activated')", (user_id,))
        conn.execute("INSERT INTO subscription_dispatch_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            dispatch_id, 7, user_id, "nanjing", workbook_id, worksheet_id, worksheet_title,
            "2026-10-26", "無骨雞", "鮭魚", "menu-v1", published_at, "published",
            published_at, payload, payload_hash,
        ))
        conn.execute("INSERT INTO subscription_dispatch_publication_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "receipt-" + receipt_hash, *receipt_values, receipt_hash,
        ))


def db_book(values=None, **binding):
    ws = Worksheet(title=binding.get("sheet_title", "會員專頁"),
                   sheet_id=binding.get("sheet_id", 101),
                   values=values or [HEADERS, row()])
    return Book(ws, binding.get("book_id", "book-1"))


def test_formal_receipt_binding_owns_markerless_staging_page_but_sheet_supplies_meals(tmp_path):
    db = tmp_path / "dispatch.db"
    seed_binding(db)
    ws = Worksheet(values=[HEADERS, row(lunch="現場新餐", dinner="無")])

    result = call(Book(ws), db)

    assert result["status"] == "ok"
    assert [meal["name"] for meal in result["meals"]] == ["現場新餐"]
    assert ws.reads == 1


@pytest.mark.parametrize("changed", ["user_id", "workbook_id", "worksheet_id", "worksheet_title"])
def test_db_binding_requires_exact_complete_identity(tmp_path, changed):
    db = tmp_path / "dispatch.db"
    values = {
        "user_id": "U-OWNER", "workbook_id": "book-1", "worksheet_id": 101,
        "worksheet_title": "會員專頁",
    }
    values[changed] = {"user_id": "U-OTHER", "workbook_id": "other-book",
                       "worksheet_id": 999, "worksheet_title": "別頁"}[changed]
    seed_binding(db, **values)

    result = call(db_book(), db)

    assert result["status"] == "unavailable"
    assert result["meals"] == []


def test_foreign_marker_refuses_even_when_database_binding_matches(tmp_path):
    db = tmp_path / "dispatch.db"
    seed_binding(db)
    values = [["User_ID: U-FOREIGN"], HEADERS, row()]

    result = call(db_book(values), db)

    assert result["status"] == "ambiguous"
    assert result["meals"] == []


def test_missing_dispatch_tables_fail_closed_without_creation(tmp_path):
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")

    result = call(db_book(), db)

    assert result["status"] == "unavailable"
    with sqlite3.connect(db) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["unrelated"]


@pytest.mark.parametrize("values, expected_reason", [
    ([["User_ID: U-OWNER"], HEADERS, HEADERS, row()], "multiple dispatch headers"),
    ([["User_ID: U-OWNER"], HEADERS, row(), row(lunch="另一餐")], "multiple rows for service_date"),
])
def test_duplicate_header_or_date_is_ambiguous(tmp_path, values, expected_reason):
    result = call(db_book(values), tmp_path / "none.db")
    assert result["status"] == "ambiguous"
    assert result["reason"] == expected_reason


def test_absent_tokens_do_not_hide_real_wugu_chicken_and_bad_nutrition_becomes_none(tmp_path):
    values = [["User_ID: U-OWNER"], HEADERS,
              row(lunch="無骨雞", lunch_kcal="NaN", lunch_protein="-1",
                  dinner="N/A", dinner_kcal="600", dinner_protein="40", dispatch_id="")]

    result = call(db_book(values), tmp_path / "none.db")

    assert result["meals"] == [{
        "slot": "午餐", "name": "無骨雞", "kcal": None, "protein": None,
        "subscription_meal_id": None,
    }]


def test_zero_nutrition_is_real_zero_and_decimal_is_not_truncated(tmp_path):
    values = [["User_ID: U-OWNER"], HEADERS,
              row(lunch_kcal="0", lunch_protein="0.25", dinner="-")]
    meal = call(db_book(values), tmp_path / "none.db")["meals"][0]
    assert meal["kcal"] == 0.0
    assert meal["protein"] == 0.25


def test_moved_meal_is_not_filled_from_old_date_snapshot(tmp_path):
    db = tmp_path / "dispatch.db"
    seed_binding(db)
    values = [HEADERS, row(date="2026/10/26", lunch="移後現場餐", dinner="無")]
    book = db_book(values)

    old = call(book, db, service_date="2026-10-07")
    target = call(book, db, service_date="2026-10-26")

    assert old == {"status": "ok", "reason": "", "source": "printer_personal_worksheet", "meals": []}
    assert target["meals"][0]["name"] == "移後現場餐"


def test_db_connection_is_uri_read_only_query_only_and_issues_no_write(monkeypatch, tmp_path):
    db = tmp_path / "dispatch.db"
    seed_binding(db)
    real_connect = sqlite3.connect
    seen = {}
    write_actions = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_TRANSACTION,
    }

    def guarded_connect(database, *args, **kwargs):
        seen["database"] = database
        seen["uri"] = kwargs.get("uri")
        conn = real_connect(database, *args, **kwargs)
        seen["writes"] = []
        seen["sql"] = []
        conn.set_trace_callback(seen["sql"].append)

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action in write_actions:
                seen["writes"].append((action, arg1, arg2))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(source.sqlite3, "connect", guarded_connect)
    result = call(db_book(), db)

    assert result["status"] == "ok"
    assert seen["uri"] is True and seen["database"].endswith("?mode=ro")
    assert any(sql.upper() == "PRAGMA QUERY_ONLY=ON" for sql in seen["sql"])
    assert seen["writes"] == []


def test_empty_user_wrong_workbook_and_wrong_exact_sheet_are_rejected(tmp_path):
    book = db_book([["User_ID: U-OWNER"], HEADERS, row()])
    assert call(book, tmp_path / "none", user_id="")["status"] == "unavailable"
    assert call(book, tmp_path / "none", workbook_id="other")["status"] == "ambiguous"
    assert call(book, tmp_path / "none", sheet_name="會員")["status"] == "unavailable"
