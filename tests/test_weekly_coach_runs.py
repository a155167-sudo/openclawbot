import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import weekly_coach_runs as wcr

NOW = datetime(2026, 11, 12, 1, 0, 0)


def test_claim_once_then_done_returns_saved_result(tmp_path):
    db = str(tmp_path / "t.db")
    assert wcr.claim(db, "U1", "2026/11/16", NOW)["state"] == "claimed"
    assert wcr.claim(db, "U1", "2026/11/16", NOW)["state"] == "running"
    wcr.finish(db, "U1", "2026/11/16", NOW, "課表", {"2026/11/16": "跑步"})
    again = wcr.claim(db, "U1", "2026/11/16", NOW)
    assert again == {"state": "done", "line_message": "課表", "daily_plan": {"2026/11/16": "跑步"}}
    # 別人、別週不受影響
    assert wcr.claim(db, "U2", "2026/11/16", NOW)["state"] == "claimed"
    assert wcr.claim(db, "U1", "2026/11/23", NOW)["state"] == "claimed"


def test_stale_running_can_be_reclaimed_and_release_frees(tmp_path):
    db = str(tmp_path / "t.db")
    wcr.claim(db, "U1", "w", NOW)
    later = NOW + timedelta(seconds=wcr.STALE_SECONDS + 1)
    assert wcr.claim(db, "U1", "w", later)["state"] == "claimed"
    wcr.release(db, "U1", "w")
    assert wcr.claim(db, "U1", "w", later)["state"] == "claimed"


def test_force_regenerates_done_but_not_running(tmp_path):
    db = str(tmp_path / "t.db")
    wcr.claim(db, "U1", "w", NOW)
    assert wcr.claim(db, "U1", "w", NOW, force=True)["state"] == "running"
    wcr.finish(db, "U1", "w", NOW, "a", {})
    assert wcr.claim(db, "U1", "w", NOW, force=True)["state"] == "claimed"


@pytest.fixture
def coach(tmp_path, monkeypatch):
    import server

    db = str(tmp_path / "coach.db")
    monkeypatch.setattr(server, "DB_PATH", db)
    calls = {"generate": 0, "sent": []}

    def fake_generate(uid, reply_token=None):
        calls["generate"] += 1
        return calls.get("result", (True, "下週課表", {"2026/11/16": "E 跑"}))

    monkeypatch.setattr(server, "_generate_weekly_coach", fake_generate)
    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(
        push_message=lambda uid, m: calls["sent"].append(m.text),
        reply_message=lambda token, m: calls["sent"].append(m.text),
    ))
    return server, calls


def test_second_request_same_week_does_not_call_ai_again(coach):
    server, calls = coach
    assert server.run_weekly_coach("U1") == (True, "下週課表")
    assert server.run_weekly_coach("U1") == (True, "下週課表")
    assert calls["generate"] == 1
    assert calls["sent"] == ["下週課表"]  # 第二次重送上次結果（第一次由生成流程自己推）


def test_failed_run_is_released_and_force_regenerates(coach):
    server, calls = coach
    calls["result"] = (False, "失敗", {})
    assert server.run_weekly_coach("U1") == (False, "失敗")
    calls["result"] = (True, "好了", {})
    assert server.run_weekly_coach("U1") == (True, "好了")
    assert server.run_weekly_coach("U1", force=True) == (True, "好了")
    assert calls["generate"] == 3


def test_api_endpoint_returns_success_instead_of_500(coach, monkeypatch):
    import asyncio

    server, calls = coach
    monkeypatch.setattr(server, "ADMIN_SECRET", "s")
    payload = server.WeeklyCoachPayload(admin_secret="s", user_id="U1")
    out = asyncio.run(server.lobster_weekly_coach(payload))
    assert out["status"] == "success" and out["plan_preview"].startswith("下週課表")


class _FakeSheet:
    def __init__(self, records):
        self.records, self.batches, self.appended = records, [], []

    def get_all_values(self):
        # 標題列故意帶兩個空白欄，模擬電腦營養那張表（get_all_records 會因此失敗）
        header = ["Date", "User_ID", "", ""]
        return [header] + [[r["Date"], r["User_ID"], "", "x"] for r in self.records]

    def get_all_records(self):
        raise AssertionError("標題列有重複空白時 get_all_records 會失敗，不能用")

    def batch_update(self, data):
        self.batches.append(data)

    def append_rows(self, rows):
        self.appended.extend(rows)


def _fake_gc(sheet):
    return SimpleNamespace(open_by_key=lambda _id: SimpleNamespace(worksheet=lambda _name: sheet))


def test_sheet_write_rereads_rows_and_batches(tmp_path, monkeypatch):
    import server

    sheet = _FakeSheet([
        {"User_ID": "other", "Date": "2026/11/16"},
        {"User_ID": "U1", "Date": "2026/11/16"},
        {"User_ID": "U1", "Date": "2026/11/17"},
    ])
    monkeypatch.setattr(server, "gc", _fake_gc(sheet))
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", False)
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "w.db"))
    plan = {"2026/11/16": "E 跑", "2026/11/17": "休息", "2026/11/21": "長跑"}
    server._write_weekly_plan_to_sheet("U1", plan, lambda d, p: [d, "U1", p])
    assert sheet.batches == [[
        {"range": "J3", "values": [["E 跑"]]},
        {"range": "J4", "values": [["休息"]]},
    ]]
    assert sheet.appended == [["2026/11/21", "U1", "長跑"]]


def test_sheet_write_with_reschedule_mode_takes_and_releases_lease(tmp_path, monkeypatch):
    import sqlite3 as _sqlite3

    import server

    db = str(tmp_path / "w.db")
    sheet = _FakeSheet([{"User_ID": "U1", "Date": "2026/11/16"}])
    monkeypatch.setattr(server, "gc", _fake_gc(sheet))
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "DB_PATH", db)
    from tests.test_workbook_write_lease import FULL_WRITER_INVENTORY
    from workbook_write_lease import configure_workbook_writer_capability, ensure_workbook_lease_schema

    conn = _sqlite3.connect(db)
    ensure_workbook_lease_schema(conn)
    configure_workbook_writer_capability(
        conn, workbook_id=server.SPREADSHEET_ID, controlled_writers=FULL_WRITER_INVENTORY,
        external_writer_state="controlled", cutover_evidence="fixture-only",
    )
    conn.commit(); conn.close()
    server._write_weekly_plan_to_sheet("U1", {"2026/11/16": "E 跑"}, lambda d, p: [d])
    assert sheet.batches == [[{"range": "J2", "values": [["E 跑"]]}]]
    # 寫完要放掉保護，第二次才能再取得
    server._write_weekly_plan_to_sheet("U1", {"2026/11/16": "T 跑"}, lambda d, p: [d])
    assert len(sheet.batches) == 2
