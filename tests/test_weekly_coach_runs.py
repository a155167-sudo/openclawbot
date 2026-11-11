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
