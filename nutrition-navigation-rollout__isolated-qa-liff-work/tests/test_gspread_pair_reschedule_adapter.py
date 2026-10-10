import copy

import pytest

from gspread_pair_reschedule_adapter import (
    MASTER_API_HEADERS,
    GspreadPairRescheduleAdapter,
    PersistedMasterProfile,
    PairSheetConflict,
)

BOOK = "book-1"
OWNER = "U" + "1" * 32
SOURCE = "2026-09-27"
TARGET = "2026-09-28"


def schedule_row(day, label, lunch="", dinner="", row_id="", menu=""):
    row = [day, label, lunch, "500" if lunch else "", "30" if lunch else "",
           dinner, "600" if dinner else "", "35" if dinner else "",
           "1100" if lunch else "", "65" if lunch else "", "", "$220" if lunch else "",
           "跑步", "待列印", row_id, "1" if row_id else "", menu]
    return row


def master_row(day, owner=OWNER, lunch="無", dinner="無", workout="休息"):
    return [day.replace("-", "/"), owner, "1800", lunch, dinner, "", "0", "減脂",
            "run", workout, "", "", "3", "60", "六", "5:00", "", "", "2", "", "0"]


class Worksheet:
    def __init__(self, title, sheet_id, rows, workbook_id=BOOK):
        self.title = title
        self.id = sheet_id
        self.rows = copy.deepcopy(rows)
        self.spreadsheet = None
        self.workbook_id = workbook_id

    def get_all_values(self):
        return copy.deepcopy(self.rows)


class Spreadsheet:
    def __init__(self, book_id, worksheets, outcome="ok"):
        self.id = book_id
        self.worksheets = {ws.id: ws for ws in worksheets}
        self.batch_calls = []
        self.outcome = outcome
        for ws in worksheets:
            ws.spreadsheet = self

    def batch_update(self, body):
        self.batch_calls.append(copy.deepcopy(body))
        if self.outcome == "fail_before_apply":
            raise RuntimeError("rejected")
        staged = {key: copy.deepcopy(ws.rows) for key, ws in self.worksheets.items()}
        for request in body["requests"]:
            if "appendCells" in request:
                append = request["appendCells"]
                values = []
                for cell in append["rows"][0]["values"]:
                    typed = cell["userEnteredValue"]
                    values.append(next(iter(typed.values())))
                staged[append["sheetId"]].append(values)
                continue
            update = request["updateCells"]
            target = update["range"]
            assert update["fields"] == "userEnteredValue"
            values = []
            for cell in update["rows"][0]["values"]:
                typed = cell["userEnteredValue"]
                values.append(next(iter(typed.values())))
            staged[target["sheetId"]][target["startRowIndex"]] = values
        for key, rows in staged.items():
            self.worksheets[key].rows = rows
        if self.outcome == "apply_then_timeout":
            raise TimeoutError("response lost")


def make_adapter(*, master_rows=None, outcome="ok", master_book=BOOK):
    schedule = Worksheet("customer", 101, [
        ["profile"],
        schedule_row(SOURCE, "第1週-日", "午餐A ($100)", "晚餐B ($120)", "old", "old-menu"),
        schedule_row(TARGET, "第2週-一"),
        ["tracking"],
    ])
    master = Worksheet("Master_API_View", 202, [MASTER_API_HEADERS] + (master_rows or [
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B", workout="長跑"),
        master_row(TARGET, workout="間歇"),
        master_row("2026-09-29", owner="another-user", lunch="別人的午餐", dinner="別人的晚餐"),
    ]), workbook_id=master_book)
    book = Spreadsheet(BOOK, [schedule, master], outcome=outcome)
    return GspreadPairRescheduleAdapter(book, schedule, master, workbook_id=BOOK), book, schedule, master


def after_rows():
    source = schedule_row(SOURCE, "第1週-日", row_id="new-source", menu="new-menu")
    source[12] = "跑步"
    target = schedule_row(TARGET, "第2週-一", "午餐A ($100)", "晚餐B ($120)", "new-target", "new-menu")
    target[12] = "跑步"
    return {SOURCE: source, TARGET: target}


def test_plan_then_one_real_batch_updates_both_views_and_preserves_other_customers():
    adapter, book, _schedule, master = make_adapter()
    before_other = copy.deepcopy(master.rows[3])

    plan = adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
    )
    assert book.batch_calls == []
    assert len(plan.batch_body["requests"]) == 4
    assert [r["updateCells"]["range"]["endColumnIndex"] for r in plan.batch_body["requests"]] == [17, 17, 21, 21]

    adapter.apply_pair_reschedule(plan)
    assert len(book.batch_calls) == 1
    observed = adapter.read_pair_reschedule(plan)
    assert observed == plan.expected_readback
    assert master.rows[1][3:5] == ["無", "無"]
    assert master.rows[2][3:5] == ["午餐A", "晚餐B"]
    assert master.rows[1][9] == "長跑" and master.rows[2][9] == "間歇"
    assert master.rows[3] == before_other


@pytest.mark.parametrize("problem", ["duplicate", "missing", "wrong_header", "different_workbook", "schedule_drift"])
def test_planner_fails_before_write_on_untrusted_before_image(problem):
    rows = [
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B"),
        master_row(TARGET),
    ]
    if problem == "duplicate":
        rows.append(master_row(TARGET))
    elif problem == "missing":
        rows.pop()
    if problem == "different_workbook":
        with pytest.raises(PairSheetConflict):
            make_adapter(master_rows=rows, master_book="other-book")
        return
    adapter, book, schedule, master = make_adapter(master_rows=rows)
    if problem == "wrong_header":
        master.rows[0][3] = "Invented_Lunch"
    if problem == "schedule_drift":
        schedule.rows[1][2] = "已被別處修改"

    with pytest.raises(PairSheetConflict):
        adapter.plan_pair_reschedule(
            workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
            source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
            expected_schedule_before={
                SOURCE: schedule_row(SOURCE, "第1週-日", "午餐A ($100)", "晚餐B ($120)", "old", "old-menu"),
                TARGET: schedule_row(TARGET, "第2週-一"),
            },
        )
    assert book.batch_calls == []


def test_plan_is_bound_to_exact_before_images_and_cannot_apply_after_drift():
    adapter, book, schedule, _master = make_adapter()
    plan = adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
    )
    schedule.rows[1][2] = "concurrent-change"
    with pytest.raises(PairSheetConflict, match="changed after planning"):
        adapter.apply_pair_reschedule(plan)
    assert book.batch_calls == []


def persisted_profile(*, plan_week="間歇", tdee="1800", carb_enabled="0"):
    return PersistedMasterProfile(
        owner_user_id=OWNER, tdee=tdee, tomorrow_training="恢復跑",
        is_coaching_enabled="0", plan_type="減脂", sport_type="run",
        plan_week=plan_week, intervals_id="", intervals_api_key="",
        training_freq="3", normal_train_time="60", long_train_day="六",
        run_pace="5:00", bike_ftp="", swim_pace="", user_level="2",
        race_date="", is_carb_cycling_enabled=carb_enabled,
    )


def test_missing_master_target_is_generated_only_from_explicit_persisted_profile():
    adapter, book, _schedule, master = make_adapter(master_rows=[
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B", workout="長跑"),
    ])
    plan = adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
        target_master_profile=persisted_profile(),
    )
    target = plan.expected_readback["master"][TARGET]
    assert target == [TARGET, OWNER, "1800", "午餐A", "晚餐B", "恢復跑", "0",
                      "減脂", "run", "間歇", "", "", "3", "60", "六", "5:00",
                      "", "", "2", "", "0"]
    assert "appendCells" in plan.batch_body["requests"][-1]
    adapter.apply_pair_reschedule(plan)
    assert adapter.read_pair_reschedule(plan) == plan.expected_readback
    assert len(book.batch_calls) == 1 and master.rows[-1] == target


def test_missing_master_target_without_persisted_profile_fails_before_write():
    adapter, book, _schedule, _master = make_adapter(master_rows=[
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B"),
    ])
    with pytest.raises(PairSheetConflict, match="persisted target profile"):
        adapter.plan_pair_reschedule(
            workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
            source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
        )
    assert book.batch_calls == []


def test_missing_target_rejects_source_placeholder_meals_before_write():
    adapter, book, _schedule, _master = make_adapter(master_rows=[master_row(SOURCE)])
    with pytest.raises(PairSheetConflict, match="source meals"):
        adapter.plan_pair_reschedule(
            workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
            source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
            target_master_profile=persisted_profile(),
        )
    assert book.batch_calls == []


@pytest.mark.parametrize("change", ["owner", "tdee", "weekday", "carb"])
def test_missing_target_rejects_untrusted_or_unprovable_metadata_before_io(change):
    adapter, book, _schedule, _master = make_adapter(master_rows=[
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B", workout="長跑"),
    ])
    profile = persisted_profile(
        plan_week="" if change == "weekday" else "間歇",
        tdee="" if change == "tdee" else "1800",
        carb_enabled="" if change == "carb" else "0",
    )
    if change == "owner":
        profile = PersistedMasterProfile(**{**profile.__dict__, "owner_user_id": "other"})
    with pytest.raises(PairSheetConflict):
        adapter.plan_pair_reschedule(
            workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
            source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
            target_master_profile=profile,
        )
    assert book.batch_calls == []
