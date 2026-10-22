"""包月介紹頁：頁面、名額 API、圖片白名單。"""
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import first_batch_trial
import plan_landing
import server


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(server, "DB_PATH", db)
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT)")
        first_batch_trial.ensure_schema(c)
        for uid, status in (("A", "pending"), ("B", "activated"), ("C", "rejected")):
            c.execute("INSERT INTO subscription_orders (user_id,status,first_batch) VALUES (?,?,1)", (uid, status))
        c.commit()
    return TestClient(server.app)


def test_page_shows_live_seats_and_line_cta(client, monkeypatch):
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "true")
    for path in ("/", "/plan"):
        r = client.get(path)
        assert r.status_code == 200
        assert "還剩 <b>13</b> 名" in r.text
        assert r.text.count('class="dot taken"') == 2
        assert "https://line.me/R/oaMessage/%40a25080337/?%E5%8C%85%E6%9C%88%E6%96%B9%E6%A1%88" in r.text
        assert "$170" in r.text and "$220" in r.text


def test_page_hides_seats_when_trial_off(client, monkeypatch):
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "false")
    r = client.get("/plan")
    assert r.status_code == 200 and "首批試跑" not in r.text.split("<main>")[0]
    assert "首批試跑期間" not in r.text


def test_seat_api_has_numbers_only(client, monkeypatch):
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "true")
    assert client.get("/api/first-batch/seats").json() == {"enabled": True, "limit": 15, "left": 13}


def test_full_shows_closed_message(client, monkeypatch):
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "true")
    monkeypatch.setenv("FIRST_BATCH_TRIAL_LIMIT", "2")
    assert "已額滿" in client.get("/plan").text


def test_assets_whitelist(client):
    assert client.get("/plan/assets/salmon.jpg").status_code == 200
    assert client.get("/plan/assets/..%2Fserver.py").status_code == 404
    assert client.get("/plan/assets/nope.jpg").status_code == 404
    for name in plan_landing.ASSETS:
        assert (plan_landing.ASSET_DIR / name).is_file()
