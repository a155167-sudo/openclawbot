from collections import Counter
import json
import sqlite3
from types import SimpleNamespace

import server
from customer_navigation import build_customer_home_contents


class _ReadOnlyWorksheet:
    def __init__(self, title, counts, values, records=None):
        self.title = title
        self._counts = counts
        self._values = values
        self._records = records or []

    def get_all_values(self):
        self._counts[f"{self.title}.get_all_values"] += 1
        return self._values

    def get_all_records(self):
        self._counts[f"{self.title}.get_all_records"] += 1
        return self._records

    def __getattr__(self, name):
        if name in {"update", "batch_update", "append_row", "append_rows", "clear", "delete_rows"}:
            raise AssertionError(f"home must not write Sheet via {name}")
        raise AttributeError(name)


class _Book:
    def __init__(self, counts, sheets):
        self._counts = counts
        self._sheets = {sheet.title: sheet for sheet in sheets}

    def worksheets(self):
        self._counts["book.worksheets"] += 1
        return list(self._sheets.values())

    def worksheet(self, title):
        self._counts[f"book.worksheet:{title}"] += 1
        if title not in self._sheets:
            raise KeyError(title)
        return self._sheets[title]

    def add_worksheet(self, **_kwargs):
        raise AssertionError("home must not create a worksheet")


class _GC:
    def __init__(self, counts, book):
        self._counts = counts
        self._book = book

    def open_by_key(self, _key):
        self._counts["gc.open_by_key"] += 1
        return self._book

    def open_by_url(self, _url):
        raise AssertionError("home must not enter the Sheet rebuild writer")


class _Line:
    def __init__(self, counts):
        self.counts = counts
        self.messages = []

    def reply_message(self, _token, message):
        self.counts["line.reply_message"] += 1
        self.messages.append(message)


def _event(uid, event_id):
    return SimpleNamespace(
        message=SimpleNamespace(id=event_id, text="首頁"),
        source=SimpleNamespace(user_id=uid),
        reply_token=f"reply-{event_id}",
    )


def _install_fixture(tmp_path, monkeypatch, *, users=("U-HOME-A",)):
    counts = Counter()
    db = tmp_path / "home.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    today_iso = server.tw_today().isoformat()
    today_slash = server.tw_today().strftime("%Y/%m/%d")
    with sqlite3.connect(db) as conn:
        for index, uid in enumerate(users):
            conn.execute(
                "INSERT INTO health_profile(user_id,name,tdee,protein,today_date,sheet_name,status,training_group) VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"會員{index + 1}", 1800 + index * 100, 100 + index * 10, today_iso, f"sheet-{index + 1}", "A", "1"),
            )
            conn.execute(
                "INSERT INTO usage(user_id,remaining_meals,status,expiry_date) VALUES (?,?,?,?)",
                (uid, 10, "vip", "2099-12-31"),
            )
        conn.commit()

    sheets = []
    for index, uid in enumerate(users):
        sheets.append(_ReadOnlyWorksheet(
            f"sheet-{index + 1}", counts,
            [["【VIP 客戶檔案】", f"姓名: 會員{index + 1}", f"User_ID: {uid}"],
             ["實際日期", "午餐安排", "晚餐安排", "運動", "午餐熱量", "午餐蛋白", "晚餐熱量", "晚餐蛋白"],
             [today_slash, f"{uid}-午餐", f"{uid}-晚餐", "跑步", "500", "40", "600", "45"]],
        ))
    sheets.extend([
        _ReadOnlyWorksheet("Master_API_View", counts, [], [
            {"Date": today_slash, "User_ID": uid, "Lunch_Item": f"{uid}-午餐", "Dinner_Item": f"{uid}-晚餐", "Plan_Week": "跑步"}
            for uid in users
        ]),
        _ReadOnlyWorksheet("training_assignments", counts, [], []),
    ])
    book = _Book(counts, sheets)
    monkeypatch.setattr(server, "gc", _GC(counts, book))
    line = _Line(counts)
    monkeypatch.setattr(server, "line_bot_api", line)
    server.processed_messages.clear()
    return db, counts, line


def _payload(message):
    return message.as_json_dict()


def test_registered_home_handler_uses_one_readonly_personal_snapshot_only(tmp_path, monkeypatch):
    db, counts, line = _install_fixture(tmp_path, monkeypatch)
    before = db.read_bytes()

    server.handle_message(_event("U-HOME-A", "HOME-CALLS-1"))

    assert counts["line.reply_message"] == 1
    assert counts["gc.open_by_key"] == 1
    assert counts["sheet-1.get_all_values"] == 1
    assert counts["Master_API_View.get_all_records"] == 0
    assert counts["training_assignments.get_all_records"] == 0
    assert counts["book.worksheets"] == 0
    assert before == db.read_bytes()
    text = json.dumps(_payload(line.messages[0]), ensure_ascii=False)
    assert "U-HOME-A-午餐" in text and "U-HOME-A-晚餐" in text
    assert "預留 500" in text
    assert "預留 600" in text


def test_home_next_request_reads_fresh_canonical_food_logs_without_ttl(tmp_path, monkeypatch):
    db, counts, line = _install_fixture(tmp_path, monkeypatch)

    server.handle_message(_event("U-HOME-A", "HOME-FRESH-1"))
    first = json.dumps(_payload(line.messages[-1]), ensure_ascii=False)
    assert "今天還沒有紀錄" not in first  # two reserved subscription rows are visible
    assert "已吃 0" in first

    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U-HOME-A", product_name="剛記錄的豆漿", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 123, "protein_g": 9, "fat_g": 4},
            source_type="manual", operation_key="fresh-home-log", publish_catalog=False,
        )
        conn.commit()

    server.handle_message(_event("U-HOME-A", "HOME-FRESH-2"))
    second = json.dumps(_payload(line.messages[-1]), ensure_ascii=False)
    assert "剛記錄的豆漿" in second
    assert "已吃 123" in second
    assert "蛋白質餘額" in second and "6 g" in second
    assert "剛記錄的豆漿" in second
    assert counts["gc.open_by_key"] == 2
    assert counts["sheet-1.get_all_values"] == 2


def test_home_requests_are_owner_bound_and_do_not_share_snapshots(tmp_path, monkeypatch):
    _db, counts, line = _install_fixture(tmp_path, monkeypatch, users=("U-HOME-A", "U-HOME-B"))

    server.handle_message(_event("U-HOME-A", "HOME-OWNER-A"))
    server.handle_message(_event("U-HOME-B", "HOME-OWNER-B"))
    payload_a = json.dumps(_payload(line.messages[0]), ensure_ascii=False)
    payload_b = json.dumps(_payload(line.messages[1]), ensure_ascii=False)

    assert "U-HOME-A-午餐" in payload_a and "U-HOME-B-午餐" not in payload_a
    assert "U-HOME-B-午餐" in payload_b and "U-HOME-A-午餐" not in payload_b
    assert counts["sheet-1.get_all_values"] == 1
    assert counts["sheet-2.get_all_values"] == 1


def test_non_vip_is_denied_before_any_sheet_access(tmp_path, monkeypatch):
    db, counts, line = _install_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE usage SET status='expired' WHERE user_id='U-HOME-A'")
        conn.commit()

    server.handle_message(_event("U-HOME-A", "HOME-NO-VIP"))

    assert counts["gc.open_by_key"] == 0
    assert counts["line.reply_message"] == 0


def test_unknown_user_does_not_touch_sheet_or_create_profile(tmp_path, monkeypatch):
    db, counts, line = _install_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage(user_id,remaining_meals,status,expiry_date) VALUES (?,?,?,?)",
            ("U-UNKNOWN", 1, "vip", "2099-12-31"),
        )
        conn.commit()

    server.handle_message(_event("U-UNKNOWN", "HOME-UNKNOWN"))

    assert counts["gc.open_by_key"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM health_profile WHERE user_id='U-UNKNOWN'").fetchone()[0] == 0


def test_home_scope_visible_output_matches_full_dashboard_fixture(tmp_path, monkeypatch):
    _db, counts, _line = _install_fixture(tmp_path, monkeypatch)

    full = server.get_dashboard_data("U-HOME-A")
    home = server.get_dashboard_data("U-HOME-A", scope="home")

    assert build_customer_home_contents(home) == build_customer_home_contents(full)
    assert full["future_days"] is not None
    assert counts["training_assignments.get_all_records"] == 5


def test_home_misbound_current_sheet_is_rejected_and_recovers_only_same_full_uid_from_master(tmp_path, monkeypatch):
    db, counts, _line = _install_fixture(tmp_path, monkeypatch, users=("U-ALICE-1234", "U-BOB-9999"))
    today = server.tw_today().strftime("%Y/%m/%d")
    book = server.gc._book
    book._sheets["sheet-1"] = _ReadOnlyWorksheet(
        "sheet-1", counts,
        [["【VIP 客戶檔案】", "姓名: Bob", "User_ID: U-BOB-9999"],
         ["實際日期", "午餐安排", "晚餐安排"],
         [today, "BOB-LUNCH", "BOB-DINNER"]],
    )
    master = book._sheets["Master_API_View"]
    master._records = [
        {"Date": today, "User_ID": "U-ALICE-1234", "Lunch_Item": "OWNER-LUNCH", "Dinner_Item": "OWNER-DINNER"},
        {"Date": today, "User_ID": "U-BOB-9999", "Lunch_Item": "BOB-LUNCH", "Dinner_Item": "BOB-DINNER"},
    ]

    data = server.get_dashboard_data("U-ALICE-1234", scope="home")

    assert data["today_lunch"] == "OWNER-LUNCH"
    assert data["today_dinner"] == "OWNER-DINNER"
    assert counts["sheet-1.get_all_values"] == 1
    assert counts["Master_API_View.get_all_records"] == 1
    assert db.exists()


def test_home_stale_mapping_does_not_authorize_same_suffix_or_same_name_candidate(tmp_path, monkeypatch):
    db, counts, _line = _install_fixture(tmp_path, monkeypatch, users=("U-ALICE-1234",))
    today = server.tw_today().strftime("%Y/%m/%d")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE health_profile SET name='Alice', sheet_name='deleted-old-sheet' WHERE user_id='U-ALICE-1234'")
        conn.commit()
    book = server.gc._book
    del book._sheets["sheet-1"]
    book._sheets["Alice_1234_20260901"] = _ReadOnlyWorksheet(
        "Alice_1234_20260901", counts,
        [["【VIP 客戶檔案】", "姓名: Alice", "User_ID: U-OTHER-1234"],
         ["實際日期", "午餐安排", "晚餐安排"], [today, "FOREIGN-LUNCH", "FOREIGN-DINNER"]],
    )
    book._sheets["Bob_1234_20260930"] = _ReadOnlyWorksheet(
        "Bob_1234_20260930", counts,
        [["【VIP 客戶檔案】", "姓名: Bob", "User_ID: U-BOB-1234"],
         ["實際日期", "午餐安排", "晚餐安排"], [today, "BOB-LUNCH", "BOB-DINNER"]],
    )
    book._sheets["Master_API_View"]._records = [
        {"Date": today, "User_ID": "U-ALICE-1234", "Lunch_Item": "OWNER-LUNCH", "Dinner_Item": "OWNER-DINNER"},
    ]

    before = db.read_bytes()
    data = server.get_dashboard_data("U-ALICE-1234", scope="home")

    assert data["today_lunch"] == "OWNER-LUNCH"
    assert "FOREIGN" not in json.dumps(data, ensure_ascii=False)
    assert counts["Master_API_View.get_all_records"] == 1
    assert before == db.read_bytes()


def test_home_missing_personal_sheet_recovers_today_from_readonly_full_uid_master_snapshot(tmp_path, monkeypatch):
    db, counts, _line = _install_fixture(tmp_path, monkeypatch, users=("U-HOME-A",))
    today = server.tw_today().strftime("%Y/%m/%d")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE health_profile SET sheet_name='' WHERE user_id='U-HOME-A'")
        conn.commit()
    book = server.gc._book
    del book._sheets["sheet-1"]
    book._sheets["Master_API_View"]._records = [
        {"Date": "2099/01/01", "User_ID": "U-HOME-A", "Lunch_Item": "FUTURE", "Dinner_Item": "FUTURE"},
        {"Date": today, "User_ID": "U-HOME-B", "Lunch_Item": "FOREIGN", "Dinner_Item": "FOREIGN"},
        {"Date": today, "User_ID": "U-HOME-A", "Lunch_Item": "OWNER-LUNCH", "Dinner_Item": "OWNER-DINNER", "Plan_Week": "REST"},
    ]

    before = db.read_bytes()
    data = server.get_dashboard_data("U-HOME-A", scope="home")

    assert data["today_lunch"] == "OWNER-LUNCH"
    assert data["today_dinner"] == "OWNER-DINNER"
    assert data["today_workout"] == "REST"
    assert counts["gc.open_by_key"] == 1
    assert counts["Master_API_View.get_all_records"] == 1
    assert counts["training_assignments.get_all_records"] == 0
    assert before == db.read_bytes()


def test_home_master_fallback_preserves_explicit_zero_and_unknown_nutrition(tmp_path, monkeypatch):
    db, counts, _line = _install_fixture(tmp_path, monkeypatch, users=("U-HOME-A",))
    today = server.tw_today().strftime("%Y/%m/%d")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE health_profile SET sheet_name='' WHERE user_id='U-HOME-A'")
        conn.commit()
    book = server.gc._book
    book._sheets["Master_API_View"]._records = [{
        "Date": today, "User_ID": "U-HOME-A", "Lunch_Item": "未知自訂餐", "Dinner_Item": "另一未知餐",
        "Lunch_Calories": "0", "Lunch_Protein": "unknown", "Dinner_Calories": "oops", "Dinner_Protein": 0,
    }]

    data = server.get_dashboard_data("U-HOME-A", scope="home")

    assert data["lunch_cal"] == 0
    assert data["lunch_pro"] is None
    assert data["dinner_cal"] is None
    assert data["dinner_pro"] == 0
    assert data["planned_cal"] is None
    assert data["planned_pro"] is None


def test_home_master_read_failure_fails_closed_without_sheet_or_database_write(tmp_path, monkeypatch):
    db, counts, _line = _install_fixture(tmp_path, monkeypatch, users=("U-HOME-A",))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE health_profile SET sheet_name='' WHERE user_id='U-HOME-A'")
        conn.commit()
    book = server.gc._book
    master = book._sheets["Master_API_View"]

    def fail_read():
        counts["Master_API_View.get_all_records"] += 1
        raise PermissionError("offline fixture: denied")

    master.get_all_records = fail_read
    before = db.read_bytes()

    data = server.get_dashboard_data("U-HOME-A", scope="home")

    assert data["today_lunch"] == "尚未安排"
    assert data["today_dinner"] == "尚未安排"
    assert data["lunch_cal"] is None and data["dinner_cal"] is None
    assert counts["Master_API_View.get_all_records"] == 1
    assert before == db.read_bytes()
