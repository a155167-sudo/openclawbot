from types import SimpleNamespace
import sqlite3

import pytest
from gspread.exceptions import APIError
from requests import Response

import server


class FakeWorksheet:
    def __init__(self, rows, sheet_id=42):
        self.rows = rows
        self.id = sheet_id
        self.update_cell_calls = []

    def get_all_values(self):
        return self.rows

    def update_cell(self, *args):
        self.update_cell_calls.append(args)
        raise AssertionError("sequential update_cell must never be used")


class FakeSpreadsheet:
    def __init__(self, worksheet, outcome="accept"):
        self.sheet = worksheet
        self.outcome = outcome
        self.batch_calls = []

    def worksheet(self, _name):
        return self.sheet

    def batch_update(self, body):
        self.batch_calls.append(body)
        if self.outcome == "reject":
            response = Response()
            response.status_code = 400
            response._content = b'{"error":{"code":400,"message":"invalid request","status":"INVALID_ARGUMENT"}}'
            raise APIError(response)

        staged = [list(row) for row in self.sheet.rows]
        for request in body["requests"]:
            update = request["updateCells"]
            target = update["range"]
            assert target["sheetId"] == self.sheet.id
            assert target["endRowIndex"] == target["startRowIndex"] + 1
            assert target["endColumnIndex"] == target["startColumnIndex"] + 1
            assert update["fields"] == "userEnteredValue"
            value = update["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
            staged[target["startRowIndex"]][target["startColumnIndex"]] = value
        self.sheet.rows[:] = staged
        if self.outcome == "apply_then_timeout":
            raise TimeoutError("response lost after server applied batch")
        return {"replies": [{}, {}]}


class FakeGC:
    def __init__(self, book):
        self.book = book

    def open_by_url(self, _url):
        return self.book

    def open_by_key(self, _key):
        raise RuntimeError("not used by this contract")


def _row(day, lunch):
    value = [""] * 14
    value[1] = day
    value[2] = lunch
    return value


def _setup(tmp_path, monkeypatch, *, outcome):
    db_path = tmp_path / "health.db"
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    uid = "U-ATOMIC-SHEET"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,sheet_name,summary_text,tdee,protein,today_date) VALUES (?,?,?,?,?,?,?)",
            (uid, "測試會員", "customer", "before", 1800, 100, server.tw_today().isoformat()),
        )
    worksheet = FakeWorksheet([
        _row("2099/10/01（週四）", "餐A"),
        _row("2099/10/02（週五）", "餐B"),
    ])
    book = FakeSpreadsheet(worksheet, outcome=outcome)
    monkeypatch.setattr(server, "gc", FakeGC(book))
    monkeypatch.setattr(
        server,
        "get_subscription_menu_access",
        lambda _uid: ("active", "menu", 10, "2099-10-31"),
    )
    monkeypatch.setattr(
        server,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="收到 [SWAP_MEAL: 2099/10/01_午餐, 2099/10/02_午餐]"
            ))]
        )))),
    )
    return uid, db_path, worksheet, book


def _summary(db_path, uid):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id=?", (uid,)
        ).fetchone()[0]


@pytest.mark.parametrize("outcome", ["accept", "reject", "apply_then_timeout"])
def test_general_ai_swap_never_reaches_atomic_backend(tmp_path, monkeypatch, outcome):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome=outcome)
    response, _ = server.get_ai_response_with_memory(
        uid, "把 2099/10/01 午餐與 2099/10/02 午餐互換",
        operation_key=f"line-ai:{uid}:SAFE-{outcome}",
    )
    assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("餐A", "餐B")
    assert _summary(db_path, uid) == "before"
    assert "本次未修改菜單" in response
    assert book.batch_calls == []
    assert worksheet.update_cell_calls == []


@pytest.mark.parametrize("outcome", ["accept", "reject", "apply_then_timeout"])
def test_deferred_move_uses_same_atomic_batch_boundary(tmp_path, monkeypatch, outcome):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome=outcome)
    worksheet.rows[1][2] = ""

    request_id = server.create_deferred_meal_request(
        uid, "測試會員", "2099/10/01", "午餐", "2099/10/02", "午餐", False, False
    )
    ok, message = server.execute_deferred_meal_move(
        uid, "2099/10/01", "午餐", "2099/10/02", "午餐",
        request_id=str(request_id), admin_uid="U-DIRECT-ADMIN",
    )

    assert len(book.batch_calls) == 1
    assert len(book.batch_calls[0]["requests"]) == 2
    assert worksheet.update_cell_calls == []
    if outcome == "accept":
        assert ok
        assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("無", "餐A")
        assert "延至" in _summary(db_path, uid)
    elif outcome == "reject":
        assert not ok
        assert "失敗" in message
        assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("餐A", "")
        assert _summary(db_path, uid) == "before"
    else:
        assert not ok
        assert "結果未確認" in message
        assert "請勿自行重試" in message
        assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("無", "餐A")
        assert _summary(db_path, uid) == "before"


def test_registered_admin_approval_path_reaches_atomic_deferred_move(tmp_path, monkeypatch):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    request_id = server.create_deferred_meal_request(
        uid,
        "測試會員",
        "2099/10/01",
        "午餐",
        "2099/10/02",
        "午餐",
        False,
        False,
    )
    replies = []
    monkeypatch.setattr(server, "ADMIN_UID", uid)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings(key,value) VALUES('admin_id',?)", (uid,)
        )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(
        server, "is_authorized_privileged_text_command", lambda *_args: True
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message.text)
    )
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda *_args: None)
    server.processed_messages.clear()
    event = SimpleNamespace(
        message=SimpleNamespace(id="ADMIN-DEFER-ATOMIC", text=f"#核准延餐 {request_id}"),
        source=SimpleNamespace(user_id=uid),
        reply_token="reply-admin-defer",
    )

    server.handle_message(event)

    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM deferred_meals WHERE id=?", (request_id,)
        ).fetchone()[0]
    assert status == "completed"
    assert len(book.batch_calls) == 1
    assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("無", "餐A")
    assert len(replies) == 1
    assert "已核准延餐申請" in replies[0]
