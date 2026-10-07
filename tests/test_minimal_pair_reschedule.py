import copy
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from minimal_pair_reschedule import (
    MASTER_HEADERS,
    PairMoveConflict,
    PairMoveUnknown,
    apply_and_verify_pair_move,
    load_pending_pair_request,
    plan_pair_move,
)


UID = "U-PAIR-OWNER"
PERSONAL_HEADERS = [
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
    "Dispatch_Row_ID", "Order_ID", "Menu_Version",
]


def personal(day, lunch="無", dinner="無", *, row_id="row", week="第1週-一"):
    lunch_cal, lunch_pro = ((500, 30) if lunch not in ("", "無", "尚未安排") else (0, 0))
    dinner_cal, dinner_pro = ((600, 35) if dinner not in ("", "無", "尚未安排") else (0, 0))
    return [day, week, lunch, lunch_cal, lunch_pro, dinner, dinner_cal, dinner_pro,
            lunch_cal + dinner_cal, lunch_pro + dinner_pro, "", "$200", "休息", "待列印",
            row_id, "41", "order-41-v1"]


def master(day, lunch="無", dinner="無", *, uid=UID, week="第1週-一"):
    return [day, uid, 1800, lunch, dinner, "休息", "1", "subscription", "run", week,
            "athlete", "secret-ref", "4", "19:00", "日", "5:30", "", "", "一般",
            "2099-12-31", "0"]


def install_request(path, *, status="pending", owner=UID, source="2099-10-23", target="2099-10-26"):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE deferred_meals(
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, original_date TEXT NOT NULL,
            original_meal_type TEXT NOT NULL, target_date TEXT NOT NULL,
            target_meal_type TEXT NOT NULL, status TEXT NOT NULL)""")
        conn.execute("INSERT INTO deferred_meals VALUES(1,?,?,?,?,?,?)",
                     (owner, source, "午餐+晚餐", target, "午餐+晚餐", status))


class FakeWorksheet:
    def __init__(self, title, sheet_id, rows):
        self.title = title
        self.id = sheet_id
        self.rows = copy.deepcopy(rows)

    def get_all_values(self):
        return copy.deepcopy(self.rows)


class FakeBook:
    def __init__(self, schedule, master_sheet, *, fail=""):
        self.id = "formal-book"
        self.schedule = schedule
        self.master = master_sheet
        self.fail = fail
        self.calls = []

    def batch_update(self, body):
        self.calls.append(copy.deepcopy(body))
        staged = {self.schedule.id: copy.deepcopy(self.schedule.rows),
                  self.master.id: copy.deepcopy(self.master.rows)}
        for request in body["requests"]:
            op = request.get("updateCells") or request.get("appendCells")
            sheet_id = op.get("range", {}).get("sheetId", op.get("sheetId"))
            values = [cell["userEnteredValue"].get("stringValue", cell["userEnteredValue"].get("numberValue"))
                      for cell in op["rows"][0]["values"]]
            if "updateCells" in request:
                staged[sheet_id][op["range"]["startRowIndex"]] = values
            else:
                staged[sheet_id].append(values)
        if self.fail == "reject":
            raise ValueError("request rejected before apply")
        self.schedule.rows = staged[self.schedule.id]
        self.master.rows = staged[self.master.id]
        if self.fail == "timeout":
            raise TimeoutError("response lost after apply")


def make_plan(tmp_path, *, target_personal=None, target_master=None,
              now=datetime(2099, 9, 1, 7, 0, tzinfo=ZoneInfo("Asia/Taipei"))):
    db = tmp_path / "request.sqlite"
    install_request(db)
    request = load_pending_pair_request(db, request_id=1, owner_user_id=UID)
    source_p = personal("2099-10-23", "午餐A", "晚餐B", row_id="source-id", week="第4週-五")
    source_m = master("2099-10-23", "午餐A", "晚餐B", week="第4週-五")
    schedule_rows = [PERSONAL_HEADERS, source_p]
    master_rows = [MASTER_HEADERS, source_m]
    if target_personal is not None:
        schedule_rows.append(target_personal)
    if target_master is not None:
        master_rows.append(target_master)
    schedule = FakeWorksheet("王小明_owner_209910", 10, schedule_rows)
    master_sheet = FakeWorksheet("Master_API_View", 20, master_rows)
    book = FakeBook(schedule, master_sheet)
    plan = plan_pair_move(
        request=request, now=now, workbook_id="formal-book", expected_workbook_id="formal-book",
        worksheet_id=10, expected_worksheet_id=10, schedule_rows=schedule.get_all_values(),
        master_rows=master_sheet.get_all_values(),
        target_personal_template=personal("2099-10-26", row_id="target-id", week="第4週-一"),
        target_master_template=master("2099-10-26", week="第4週-一"),
    )
    return plan, book, schedule, master_sheet


def test_missing_target_moves_both_meals_in_one_batch_and_preserves_meal_count(tmp_path):
    plan, book, schedule, master_sheet = make_plan(tmp_path)
    before_count = plan.meal_count_before

    receipt = apply_and_verify_pair_move(book, schedule, master_sheet, plan)

    assert len(book.calls) == 1
    assert len(book.calls[0]["requests"]) == 4
    assert receipt.verified is True
    assert plan.meal_count_after == before_count == 2
    assert schedule.rows[1][2:8] == ["無", 0, 0, "無", 0, 0]
    assert schedule.rows[2][0:8] == ["2099-10-26", "第4週-一", "午餐A", 500, 30, "晚餐B", 600, 35]
    assert master_sheet.rows[1][3:5] == ["無", "無"]
    assert master_sheet.rows[2][0:5] == ["2099-10-26", UID, 1800, "午餐A", "晚餐B"]


def test_existing_occupied_target_is_rejected_without_write(tmp_path):
    occupied_p = personal("2099-10-26", "別人的午餐", "無", row_id="target-id")
    occupied_m = master("2099-10-26", "別人的午餐", "無")
    with pytest.raises(PairMoveConflict, match="target.*occupied"):
        make_plan(tmp_path, target_personal=occupied_p, target_master=occupied_m)


def test_request_owner_status_and_full_dates_are_revalidated_from_sqlite(tmp_path):
    db = tmp_path / "request.sqlite"
    install_request(db, owner="OTHER")
    with pytest.raises(PairMoveConflict, match="owner"):
        load_pending_pair_request(db, request_id=1, owner_user_id=UID)

    db2 = tmp_path / "done.sqlite"
    install_request(db2, status="completed")
    with pytest.raises(PairMoveConflict, match="pending"):
        load_pending_pair_request(db2, request_id=1, owner_user_id=UID)

    db3 = tmp_path / "short-date.sqlite"
    install_request(db3, source="10/23")
    with pytest.raises(PairMoveConflict, match="full calendar date"):
        load_pending_pair_request(db3, request_id=1, owner_user_id=UID)


def test_past_target_is_rejected_for_original_2026_request(tmp_path):
    db = tmp_path / "past.sqlite"
    install_request(db, source="2026-10-23", target="2026-09-26")
    request = load_pending_pair_request(db, request_id=1, owner_user_id=UID)
    with pytest.raises(PairMoveConflict, match="past"):
        plan_pair_move(
            request=request,
            now=datetime(2026, 9, 27, 7, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            workbook_id="formal-book", expected_workbook_id="formal-book",
            worksheet_id=10, expected_worksheet_id=10,
            schedule_rows=[PERSONAL_HEADERS, personal("2026-10-23", "午餐A", "晚餐B")],
            master_rows=[MASTER_HEADERS, master("2026-10-23", "午餐A", "晚餐B")],
            target_personal_template=personal("2026-09-26", row_id="target-id"),
            target_master_template=master("2026-09-26"),
        )


def test_same_day_pair_cutoff_rejects_before_any_batch(tmp_path):
    db = tmp_path / "cutoff.sqlite"
    install_request(db, source="2099-10-23", target="2099-10-26")
    request = load_pending_pair_request(db, request_id=1, owner_user_id=UID)
    with pytest.raises(PairMoveConflict, match="cutoff"):
        plan_pair_move(
            request=request,
            now=datetime(2099, 10, 23, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            workbook_id="formal-book", expected_workbook_id="formal-book",
            worksheet_id=10, expected_worksheet_id=10,
            schedule_rows=[PERSONAL_HEADERS, personal("2099-10-23", "午餐A", "晚餐B")],
            master_rows=[MASTER_HEADERS, master("2099-10-23", "午餐A", "晚餐B")],
            target_personal_template=personal("2099-10-26", row_id="target-id"),
            target_master_template=master("2099-10-26"),
        )


def test_cross_view_source_mismatch_is_rejected(tmp_path):
    db = tmp_path / "mismatch.sqlite"
    install_request(db)
    request = load_pending_pair_request(db, request_id=1, owner_user_id=UID)
    with pytest.raises(PairMoveConflict, match="personal/Master"):
        plan_pair_move(
            request=request,
            now=datetime(2099, 9, 1, 7, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            workbook_id="formal-book", expected_workbook_id="formal-book",
            worksheet_id=10, expected_worksheet_id=10,
            schedule_rows=[PERSONAL_HEADERS, personal("2099-10-23", "午餐A", "晚餐B")],
            master_rows=[MASTER_HEADERS, master("2099-10-23", "不同午餐", "晚餐B")],
            target_personal_template=personal("2099-10-26", row_id="target-id"),
            target_master_template=master("2099-10-26"),
        )


def test_timeout_is_unknown_and_never_rewrites_or_reports_verified_success(tmp_path):
    plan, book, schedule, master_sheet = make_plan(tmp_path)
    book.fail = "timeout"

    with pytest.raises(PairMoveUnknown, match="unknown"):
        apply_and_verify_pair_move(book, schedule, master_sheet, plan)

    assert len(book.calls) == 1
    assert not hasattr(plan, "success_message")
    assert schedule.rows[2][2:6] == ["午餐A", 500, 30, "晚餐B"]
