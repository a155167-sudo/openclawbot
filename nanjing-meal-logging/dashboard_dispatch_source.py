"""唯讀讀取南京出單機個人 worksheet 的單日預留餐點。

本模組不持有 Google 憑證、不查 Master_API_View，也不以 SQLite 菜名回填；
``book`` 必須由呼叫端注入。SQLite 僅用於無 User_ID marker 頁面的正式發布綁定驗證。
"""
from __future__ import annotations

from datetime import date
import math
import re
import sqlite3
import unicodedata
from typing import Any
from urllib.parse import quote


_SOURCE = "printer_personal_worksheet"
_BASE_HEADER = (
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
)
_DISPATCH_TAIL = ("Dispatch_Row_ID", "Order_ID", "Menu_Version")
_VALID_HEADERS = frozenset({_BASE_HEADER, _BASE_HEADER + _DISPATCH_TAIL})
_ABSENT_MEALS = frozenset({"", "無", "無餐", "不供餐", "不出餐", "N/A", "NA", "—", "-"})
_UID_MARKER = re.compile(r"^User_ID:\s*(\S(?:.*\S)?)$")
_DATE = re.compile(r"^\s*(\d{4})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*$")


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str("" if value is None else value)).strip()


def _result(status: str, reason: str, meals: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "source": _SOURCE, "meals": meals or []}


def _date_text(value: object) -> str | None:
    match = _DATE.fullmatch(_text(value))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def _nutrition(value: object) -> float | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _marker_ownership(values: list[list[object]], user_id: str) -> tuple[bool | None, str]:
    markers: list[str] = []
    for row in values:
        for cell in row:
            match = _UID_MARKER.fullmatch(_text(cell))
            if match:
                markers.append(_text(match.group(1)))
    if not markers:
        return None, ""
    if len(markers) != 1 or markers[0] != user_id:
        return False, "worksheet owner marker is ambiguous"
    return True, ""


def _db_ownership(db_path: str, user_id: str, workbook_id: str,
                  worksheet_id: str, worksheet_title: str) -> tuple[bool, str]:
    """Validate a producer-receipted binding without initializing or changing schema."""
    if not db_path:
        return False, "dispatch ownership database unavailable"
    try:
        uri = "file:" + quote(str(db_path), safe="/")
        conn = sqlite3.connect(uri + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error, ValueError):
        return False, "dispatch ownership database unavailable"
    try:
        conn.execute("PRAGMA query_only=ON")
        required = {"subscription_dispatch_rows", "subscription_dispatch_publication_receipts",
                    "subscription_orders"}
        names = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if not required.issubset(names):
            return False, "trusted dispatch ownership tables unavailable"

        from subscription_dispatch_contract import _trusted_publication_rows
        where = ("r.workbook_id=? AND CAST(r.worksheet_id AS TEXT)=? "
                 "AND r.worksheet_title=?")
        parameters = (workbook_id, worksheet_id, worksheet_title)
        raw_count = int(conn.execute(
            "SELECT count(*) FROM subscription_dispatch_publication_receipts r "
            "WHERE " + where, parameters
        ).fetchone()[0])
        trusted = _trusted_publication_rows(conn, where, parameters)
        if raw_count < 1 or len(trusted) != raw_count:
            return False, "trusted dispatch ownership binding unavailable"
        if any(_text(item.get("customer_uid")) != user_id for item in trusted):
            return False, "trusted dispatch ownership conflicts with user"
        return True, ""
    except (ImportError, KeyError, TypeError, ValueError, sqlite3.Error):
        return False, "trusted dispatch ownership binding unavailable"
    finally:
        conn.close()


def read_dispatch_day(*, book: Any, db_path: str, user_id: str, sheet_name: str,
                      workbook_id: str, service_date: str) -> dict[str, Any]:
    """Read one day from one exactly-bound personal worksheet, failing closed."""
    uid = _text(user_id)
    expected_book = _text(workbook_id)
    title = _text(sheet_name)
    if not uid or not expected_book or not title:
        return _result("unavailable", "required dispatch identity is empty")
    if _date_text(service_date) != _text(service_date):
        return _result("unavailable", "service_date must be YYYY-MM-DD")
    if _text(getattr(book, "id", "")) != expected_book:
        return _result("ambiguous", "workbook identity mismatch")

    try:
        worksheet = book.worksheet(title)
    except Exception:
        return _result("unavailable", "personal worksheet unavailable")
    if _text(getattr(worksheet, "title", "")) != title:
        return _result("ambiguous", "worksheet title mismatch")
    worksheet_id = _text(getattr(worksheet, "id", ""))
    if not worksheet_id:
        return _result("unavailable", "worksheet identity unavailable")
    try:
        raw_values = worksheet.get_all_values()
        values = [list(row) for row in raw_values]
    except Exception:
        return _result("unavailable", "personal worksheet read failed")

    marker_owner, reason = _marker_ownership(values, uid)
    if marker_owner is False:
        return _result("ambiguous", reason)
    if marker_owner is None:
        bound, reason = _db_ownership(str(db_path), uid, expected_book, worksheet_id, title)
        if not bound:
            return _result("unavailable", reason)

    headers: list[tuple[int, list[str]]] = []
    for index, raw in enumerate(values[:15]):
        normalized = [_text(cell) for cell in raw]
        while normalized and not normalized[-1]:
            normalized.pop()
        if tuple(normalized) in _VALID_HEADERS:
            headers.append((index, normalized))
    if not headers:
        return _result("unavailable", "exact 14/17-column dispatch header unavailable")
    if len(headers) != 1:
        return _result("ambiguous", "multiple dispatch headers")
    header_index, header = headers[0]
    dispatch_index = header.index("Dispatch_Row_ID") if header.count("Dispatch_Row_ID") == 1 else None

    matching: list[list[object]] = []
    for raw in values[header_index + 1:]:
        padded = list(raw) + [""] * max(0, 14 - len(raw))
        if _date_text(padded[0]) == service_date:
            matching.append(padded)
    if len(matching) > 1:
        return _result("ambiguous", "multiple rows for service_date")
    if not matching:
        return _result("ok", "")

    selected = matching[0]
    dispatch_id = ""
    if dispatch_index is not None and dispatch_index < len(selected):
        dispatch_id = _text(selected[dispatch_index])
    meals: list[dict[str, Any]] = []
    for slot, name_index, kcal_index, protein_index in (
        ("午餐", 2, 3, 4), ("晚餐", 5, 6, 7),
    ):
        name = _text(selected[name_index])
        if name.upper() in _ABSENT_MEALS:
            continue
        meals.append({
            "slot": slot,
            "name": name,
            "kcal": _nutrition(selected[kcal_index]),
            "protein": _nutrition(selected[protein_index]),
            "subscription_meal_id": f"{dispatch_id}:{slot}" if dispatch_id else None,
        })
    return _result("ok", "", meals)
