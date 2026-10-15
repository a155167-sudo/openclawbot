"""順風車折扣：新客加入現有外送客戶路線(額外繞行 <= 7 分鐘)，每趟再折 $10。"""
import os
import sqlite3
from contextlib import closing

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server

B = "台北市松山區新客路1號"
A_ADDR = "台北市松山區秘密路99號"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(server, "DB_PATH", db)
    with closing(sqlite3.connect(db)) as c:
        c.execute("""CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, address TEXT, delivery_fee INTEGER, delivery_count INTEGER, status TEXT,
            delivery_type TEXT DEFAULT '')""")
        c.commit()
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)
    monkeypatch.setattr(server, "CARPOOL_DISCOUNT_ENABLED", True)
    monkeypatch.setattr(server, "get_distance", lambda *a, **k: (True, "0.8 公里", 800, "8 分鐘"))
    yield db


def _add(db, uid, addr=A_ADDR, fee=15, count=20, status="activated", dtype="晚鳥"):
    with closing(sqlite3.connect(db)) as c:
        c.execute("INSERT INTO subscription_orders (user_id,address,delivery_fee,delivery_count,status,delivery_type) VALUES (?,?,?,?,?,?)",
                  (uid, addr, fee, count, status, dtype))
        c.commit()


def _travel(monkeypatch, a_to_b, store_a=600, store_b=600, b_to_a=None):
    b_to_a = a_to_b if b_to_a is None else b_to_a

    def fake(o, d, mode="driving"):
        if o == server.STORE_ADDRESS:
            return {A_ADDR: store_a, B: store_b}[d]
        return a_to_b if o == A_ADDR else b_to_a

    monkeypatch.setattr(server, "get_travel_seconds", fake)


def _est(uid="U-new", dtype="晚鳥"):
    return server.calculate_subscription_estimate(uid, 20, address=B, delivery_count=20, delivery_type=dtype)


def test_within_7_min_gets_discount(monkeypatch, _env):
    _add(_env, "U-old")
    _travel(monkeypatch, 400)
    est = _est()
    assert est["carpool"] is True
    assert est["late_bird_fee"] == 15 and est["delivery_fee"] == 5
    assert est["delivery_total"] == 5 * 20


def test_over_7_min_no_discount(monkeypatch, _env):
    _add(_env, "U-old")
    _travel(monkeypatch, 500, store_a=600, store_b=600)  # insert-before detour = 600+500-600 = 500
    est = _est()
    assert est["carpool"] is False and est["delivery_fee"] == 15


def test_insert_before_branch(monkeypatch, _env):
    _add(_env, "U-old")
    _travel(monkeypatch, 900, store_a=600, store_b=300, b_to_a=500)  # 300+500-600 = 200
    assert _est()["carpool"] is True


def test_fee_floor_zero(monkeypatch, _env):
    _add(_env, "U-old")
    _travel(monkeypatch, 100)
    monkeypatch.setattr(server, "get_distance", lambda *a, **k: (True, "0.5 公里", 500, "5 分鐘"))
    monkeypatch.setattr(server, "CARPOOL_DISCOUNT_PER_TRIP", 20)
    assert _est()["delivery_fee"] == 0


def test_no_other_subscribers(monkeypatch, _env):
    _travel(monkeypatch, 100)
    assert _est()["carpool"] is False


def test_inactive_or_pickup_orders_ignored(monkeypatch, _env):
    _add(_env, "U-a", status="pending")
    _add(_env, "U-b", fee=0, count=0)
    _travel(monkeypatch, 100)
    assert _est()["carpool"] is False


def test_api_failure_still_quotes(monkeypatch, _env):
    _add(_env, "U-old")
    monkeypatch.setattr(server, "get_travel_seconds", lambda *a, **k: None)
    est = _est()
    assert est["carpool"] is False and est["delivery_fee"] == 15


def test_own_order_ignored(monkeypatch, _env):
    _add(_env, "U-new")
    _travel(monkeypatch, 100)
    assert _est("U-new")["carpool"] is False


def test_flag_off(monkeypatch, _env):
    _add(_env, "U-old")
    _travel(monkeypatch, 100)
    monkeypatch.setattr(server, "CARPOOL_DISCOUNT_ENABLED", False)
    est = _est()
    assert est["carpool"] is False
    assert not any("順風車" in l for l in server.format_late_bird_delivery_lines(est))


def test_lines_text_and_privacy(monkeypatch, _env):
    _add(_env, "U-old")
    _travel(monkeypatch, 400)
    on = "\n".join(server.format_late_bird_delivery_lines(_est()))
    assert "🚗 順風車　✅ 你在現有路線上 → $5／趟" in on
    assert "外送費小計 $100" in on
    assert A_ADDR not in on and "秘密路" not in on
    _travel(monkeypatch, 5000, store_a=900, store_b=900)
    off = "\n".join(server.format_late_bird_delivery_lines(_est()))
    assert "🚗 順風車　你這一區目前還沒有路線，揪附近的同事或鄰居一起訂就能再折 $10／趟" in off
    assert A_ADDR not in off


def test_quote_hint_is_neutral_and_private():
    q = server.calculate_delivery_quote(B)
    assert A_ADDR not in q["carpool_hint"] and "同路客戶" not in q["carpool_hint"]


def test_regular_delivery_carpool_applies_ten_off_base_fee(monkeypatch, _env):
    _add(_env, "U-old", fee=30, dtype="一般外送")
    _travel(monkeypatch, 400)
    est = _est(dtype="一般外送")
    assert est["carpool"] is True
    assert est["base_delivery_fee"] == 30 and est["delivery_fee"] == 20
    assert est["delivery_total"] == 20 * 20


def test_carpool_only_matches_same_delivery_type(monkeypatch, _env):
    _add(_env, "U-late", fee=15, dtype="晚鳥")
    _travel(monkeypatch, 100)
    reg = _est(dtype="一般外送")
    assert reg["carpool"] is False and reg["delivery_fee"] == 30
    _add(_env, "U-reg", fee=30, dtype="一般外送")
    assert _est(dtype="一般外送")["carpool"] is True
    assert _est(dtype="晚鳥")["carpool"] is True   # U-late 同為晚鳥
    with closing(sqlite3.connect(_env)) as c:
        c.execute("DELETE FROM subscription_orders WHERE user_id='U-late'")
        c.commit()
    assert _est(dtype="晚鳥")["carpool"] is False   # 只剩一般外送鄰居


def test_legacy_order_without_type_counts_as_regular(monkeypatch, _env):
    _add(_env, "U-legacy", fee=30, dtype="")
    _travel(monkeypatch, 100)
    assert _est(dtype="一般外送")["carpool"] is True
    assert _est(dtype="晚鳥")["carpool"] is False
