from datetime import date, datetime
from zoneinfo import ZoneInfo

import server


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []
        self.id = 42

    def get_all_values(self):
        return self.rows

    def update_cell(self, row, col, value):
        self.updates.append((row, col, value))


class FakeBook:
    def __init__(self, sheet):
        self.sheet = sheet

    def worksheet(self, _name):
        return self.sheet

    def batch_update(self, body):
        staged = [list(row) for row in self.sheet.rows]
        for request in body["requests"]:
            update = request["updateCells"]
            target = update["range"]
            value = update["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
            row = target["startRowIndex"]
            col = target["startColumnIndex"]
            staged[row][col] = value
            self.sheet.updates.append((row + 1, col + 1, value))
        self.sheet.rows[:] = staged
        return {"replies": [{} for _ in body["requests"]]}


class FakeSheetsClient:
    def __init__(self, sheet):
        self.sheet = sheet

    def open_by_url(self, _url):
        return FakeBook(self.sheet)


def _row(date_text, lunch="午餐菜", dinner="晚餐菜", printed=""):
    values = [""] * 14
    values[1] = date_text
    values[2] = lunch
    values[5] = dinner
    values[13] = printed
    return values


def _setup_deferred_move(tmp_path, monkeypatch, rows, *, db_name="deferred-meal.db"):
    user_id = "U" + "e" * 32
    db_path = tmp_path / db_name
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    with server.sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile (user_id, sheet_name, summary_text) VALUES (?, ?, ?)",
            (user_id, "customer", "existing menu"),
        )
    sheet = FakeSheet(rows)
    monkeypatch.setattr(server, "gc", FakeSheetsClient(sheet))
    monkeypatch.setattr(
        server,
        "get_subscription_menu_access",
        lambda _uid: ("active", "menu", 10, "2026-10-31"),
    )
    return user_id, db_path, sheet


def _summary_text(db_path, user_id):
    with server.sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id=?", (user_id,)
        ).fetchone()[0]


def _execute_deferred(user_id, original_date, original_meal, target_date, target_meal):
    request_id = server.create_deferred_meal_request(
        user_id, "測試會員", original_date, original_meal, target_date, target_meal,
        False, False,
    )
    return server.execute_deferred_meal_move(
        user_id, original_date, original_meal, target_date, target_meal,
        request_id=str(request_id), admin_uid="U-TEST-ADMIN",
    )


def test_meal_slot_uses_exact_calendar_date_and_refuses_weekday_ambiguity():
    sheet = FakeSheet([
        _row("2026/10/10（週六）"),
        _row("2026/10/01（週四）"),
        _row("2026/10/05（週一）"),
        _row("2026/10/12（週一）"),
    ])

    slot = server.find_meal_slot_in_user_sheet(sheet, "10/1", "午餐")

    assert slot is not None
    assert slot["row_idx"] == 1
    assert slot["date_text"] == "2026/10/01（週四）"
    assert server.find_meal_slot_in_user_sheet(sheet, "週一", "午餐") is None


def test_meal_swap_fails_closed_at_same_day_cutoff(tmp_path, monkeypatch):
    db_path = tmp_path / "meal-swap.db"
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    with server.sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile (user_id, sheet_name, summary_text) VALUES (?, ?, ?)",
            ("U" + "b" * 32, "customer", "existing menu"),
        )
    sheet = FakeSheet([_row("2026/10/01（週四）"), _row("2026/10/02（週五）")])
    monkeypatch.setattr(server, "gc", FakeSheetsClient(sheet))
    monkeypatch.setattr(server, "get_subscription_menu_access", lambda _uid: ("active", "menu", 10, "2026-10-31"))
    monkeypatch.setattr(server, "tw_today", lambda: date(2026, 10, 1))
    monkeypatch.setattr(server, "tw_now", lambda: datetime(2026, 10, 1, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")))

    result = server.execute_meal_swap(
        "U" + "b" * 32,
        "10/1",
        "午餐",
        "10/2",
        "晚餐",
        operation_id=f"line-ai:{'U' + 'b' * 32}:CUTOFF",
    )

    assert "修改期限" in result
    assert sheet.updates == []


def test_meal_swap_denies_user_without_active_subscription_before_database_or_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "never-open.db"))
    monkeypatch.setattr(server, "get_subscription_menu_access", lambda _uid: ("expired", None, 0, ""))
    monkeypatch.setattr(server.sqlite3, "connect", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("database must not be read")))
    monkeypatch.setattr(server, "gc", None)

    result = server.execute_meal_swap(
        "U" + "c" * 32,
        "10/1",
        "午餐",
        "10/2",
        "晚餐",
        operation_id=f"line-ai:{'U' + 'c' * 32}:EXPIRED",
    )

    assert "有效包月方案" in result


def test_deferred_meal_approval_rechecks_active_plan_before_sheet_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "expired-deferred.db"))
    server.init_db()
    monkeypatch.setattr(server, "get_subscription_menu_access", lambda _uid: ("expired", None, 0, ""))
    monkeypatch.setattr(server, "gc", None)

    ok, message = _execute_deferred(
        "U" + "d" * 32, "10/1", "午餐", "10/2", "午餐"
    )

    assert not ok
    assert "有效包月方案" in message


def test_deferred_meal_move_rejects_past_source_or_target_without_business_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "tw_today", lambda: date(2026, 10, 2))
    monkeypatch.setattr(
        server,
        "tw_now",
        lambda: datetime(2026, 10, 2, 7, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    for case_index, (original_date, target_date) in enumerate(
        (("10/1", "10/3"), ("10/3", "10/1")), start=1
    ):
        user_id, db_path, sheet = _setup_deferred_move(
            tmp_path,
            monkeypatch,
            [
                _row("2026/10/01（週四）", lunch=""),
                _row("2026/10/03（週六）", lunch=""),
            ],
            db_name=f"deferred-meal-{case_index}.db",
        )
        source = sheet.rows[0] if original_date == "10/1" else sheet.rows[1]
        source[2] = "原餐"

        ok, message = _execute_deferred(
            user_id, original_date, "午餐", target_date, "午餐"
        )

        assert not ok
        assert "日期已過" in message
        assert sheet.updates == []
        assert _summary_text(db_path, user_id) == "existing menu"


def test_deferred_meal_move_rejects_same_day_lunch_or_dinner_at_cutoff_without_business_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "tw_today", lambda: date(2026, 10, 2))
    cases = (
        ("10/2", "午餐", "10/3", "午餐", 8),
        ("10/3", "午餐", "10/2", "午餐", 8),
        ("10/2", "晚餐", "10/3", "晚餐", 14),
        ("10/3", "晚餐", "10/2", "晚餐", 14),
    )

    for case_index, (original_date, original_meal, target_date, target_meal, hour) in enumerate(
        cases, start=1
    ):
        monkeypatch.setattr(
            server,
            "tw_now",
            lambda hour=hour: datetime(
                2026, 10, 2, hour, 0, tzinfo=ZoneInfo("Asia/Taipei")
            ),
        )
        user_id, db_path, sheet = _setup_deferred_move(
            tmp_path,
            monkeypatch,
            [
                _row("2026/10/02（週五）", lunch="", dinner=""),
                _row("2026/10/03（週六）", lunch="", dinner=""),
            ],
            db_name=f"deferred-cutoff-{case_index}.db",
        )
        source = sheet.rows[0] if original_date == "10/2" else sheet.rows[1]
        source[2 if original_meal == "午餐" else 5] = "原餐"

        ok, message = _execute_deferred(
            user_id, original_date, original_meal, target_date, target_meal
        )

        assert not ok
        assert "修改期限" in message
        assert sheet.updates == []
        assert _summary_text(db_path, user_id) == "existing menu"


def test_deferred_meal_move_allows_lunch_or_dinner_before_same_day_cutoff(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "tw_today", lambda: date(2026, 10, 2))
    for case_index, (meal_type, hour, minute) in enumerate(
        (("午餐", 7, 59), ("晚餐", 13, 59)), start=1
    ):
        monkeypatch.setattr(
            server,
            "tw_now",
            lambda hour=hour, minute=minute: datetime(
                2026, 10, 2, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")
            ),
        )
        user_id, db_path, sheet = _setup_deferred_move(
            tmp_path,
            monkeypatch,
            [
                _row("2026/10/02（週五）", lunch="", dinner=""),
                _row("2026/10/03（週六）", lunch="", dinner=""),
            ],
            db_name=f"deferred-before-cutoff-{case_index}.db",
        )
        meal_col = 2 if meal_type == "午餐" else 5
        sheet.rows[0][meal_col] = "原餐"

        ok, message = _execute_deferred(
            user_id, "10/2", meal_type, "10/3", meal_type
        )

        assert ok
        assert "已將" in message
        assert sheet.updates == [(1, meal_col + 1, "無"), (2, meal_col + 1, "原餐")]
        assert "延至" in _summary_text(db_path, user_id)


def test_deferred_meal_move_still_rejects_printed_source_or_target_without_business_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "tw_today", lambda: date(2026, 10, 2))
    monkeypatch.setattr(
        server,
        "tw_now",
        lambda: datetime(2026, 10, 2, 7, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )
    for case_index, printed_row in enumerate((0, 1), start=1):
        rows = [
            _row("2026/10/03（週六）", lunch="原餐"),
            _row("2026/10/04（週日）", lunch=""),
        ]
        rows[printed_row][13] = "已列印"
        user_id, db_path, sheet = _setup_deferred_move(
            tmp_path,
            monkeypatch,
            rows,
            db_name=f"deferred-printed-{case_index}.db",
        )

        ok, message = _execute_deferred(
            user_id, "10/3", "午餐", "10/4", "午餐"
        )

        assert not ok
        assert "出單列印" in message
        assert sheet.updates == []
        assert _summary_text(db_path, user_id) == "existing menu"
