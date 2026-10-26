"""營養師工作台 AI 擬稿：去識別、輸出檢查、路由權限、不儲存。"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dietitian_health_check_ai_draft as ai
import server
from dietitian_health_check_api import create_dietitian_health_check_router
from vip_health_check import (
    configure_vip_health_check_connection,
    create_first_vip_health_check_case,
    ensure_vip_health_check_schema,
)

TPE = ZoneInfo("Asia/Taipei")
UID = "U" + "d" * 32
NOW = datetime(2026, 10, 27, 21, 0, tzinfo=TPE)
GOOD = {
    "good": "・三天的午晚餐都有吃完，蛋白質來源穩定。",
    "priority": "・早餐幾乎沒有記錄，蛋白質主要集中在午晚餐。",
    "next_7_days": "・早餐加一顆茶葉蛋或一杯無糖豆漿，先連續做 5 天。",
}


class FakeClient:
    def __init__(self, content):
        self.sent = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.sent.append(kwargs)
                msg = SimpleNamespace(content=content, refusal=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    path = str(tmp_path / "ai.db")
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setattr(server, "APP_ENV", "staging")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(server, "tw_now", lambda: NOW)
    monkeypatch.setattr(server, "_health_check_ai_draft_last_by_case", {})
    monkeypatch.setattr(server, "_health_check_ai_draft_calls", [])
    with closing(sqlite3.connect(path)) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, "
                     "protein REAL, goal TEXT, restrictions TEXT, active_days TEXT)")
        conn.execute("INSERT INTO health_profile VALUES (?,?,?,?,?,?,?)", (UID, '王小明', 1800, 110, '減脂', '不吃牛', ''))
        create_first_vip_health_check_case(conn, user_id=UID, first_vip_activation_id="a1",
                                           activation_event_key="a1", activated_at=NOW - timedelta(days=9))
        conn.commit()
    assert "待營養師審核" in server.run_staging_health_check_qa(UID)
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT case_id FROM vip_health_check_cases WHERE user_id=?", (UID,)).fetchone()[0]


def test_drafts_from_seeded_case_without_identity(seeded, monkeypatch):
    fake = FakeClient(json.dumps(GOOD, ensure_ascii=False))
    monkeypatch.setattr(server, "client", fake)
    result = server.ai_draft_dietitian_health_check(seeded, "Uactor")
    assert result["good"] == GOOD["good"] and result["persistence"] == "not_saved"
    sent = fake.sent[0]["messages"][1]["content"]
    assert UID not in sent and "王小明" not in sent and seeded not in sent
    payload = json.loads(sent)
    assert payload["顧客目標"] == "減脂" and payload["飲食限制"] == "不吃牛"
    assert len(payload["三日紀錄"]) == 3
    assert all(day["餐點"][0]["品名"] != "（未提供品名）" for day in payload["三日紀錄"])
    # 不儲存：沒有任何 review 被寫入
    with closing(sqlite3.connect(server.DB_PATH)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0] == 0


def test_cooldown(seeded, monkeypatch):
    monkeypatch.setattr(server, "client", FakeClient(json.dumps(GOOD, ensure_ascii=False)))
    server.ai_draft_dietitian_health_check(seeded, "Uactor")
    with pytest.raises(ai.AiDraftUnavailable):
        server.ai_draft_dietitian_health_check(seeded, "Uactor")


@pytest.mark.parametrize("bad", [
    "not json",
    json.dumps({"good": "太短", "priority": GOOD["priority"], "next_7_days": GOOD["next_7_days"]}),
    json.dumps({**GOOD, "priority": "・你可能有糖尿病的風險，建議少吃澱粉。"}, ensure_ascii=False),
    json.dumps({**GOOD, "comment": "多一欄"}, ensure_ascii=False),
])
def test_bad_output_is_rejected(bad):
    with pytest.raises(ai.AiDraftUnavailable):
        ai.parse_ai_draft(bad)


def test_not_reviewable_case_refused():
    with pytest.raises(ai.AiDraftUnavailable):
        ai.build_ai_draft_input({"status": "delivered"}, {})


def _app(drafter):
    app = FastAPI()
    app.include_router(create_dietitian_health_check_router(
        channel_id="1654353734", allowed_uids=frozenset({"U" + "e" * 32}),
        list_loader=lambda **k: {}, detail_loader=lambda cid: None,
        token_verifier=lambda token, channel_id: token, ai_drafter=drafter))
    return TestClient(app)


def test_route_requires_allowed_dietitian():
    client = _app(lambda cid, actor: GOOD)
    assert client.post("/api/dietitian/health-checks/vhc_1/ai-draft").status_code == 401
    assert client.post("/api/dietitian/health-checks/vhc_1/ai-draft",
                       headers={"Authorization": "Bearer U" + "f" * 32}).status_code == 403
    ok = client.post("/api/dietitian/health-checks/vhc_1/ai-draft",
                     headers={"Authorization": "Bearer U" + "e" * 32})
    assert ok.status_code == 200 and ok.json()["good"] == GOOD["good"]


def test_route_maps_unavailable_to_409():
    def drafter(cid, actor):
        raise ai.AiDraftUnavailable("剛剛才擬過，請等幾秒再按。")
    r = _app(drafter).post("/api/dietitian/health-checks/vhc_1/ai-draft",
                           headers={"Authorization": "Bearer U" + "e" * 32})
    assert r.status_code == 409 and "剛剛" in r.json()["detail"]
