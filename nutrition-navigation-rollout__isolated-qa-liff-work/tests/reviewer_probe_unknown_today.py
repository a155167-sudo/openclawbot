import json
import sqlite3
from pathlib import Path

import server
from customer_navigation import build_customer_home_contents


def _texts(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node.get("text")
        for value in node.values():
            yield from _texts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _texts(value)


def test_unknown_nutrition_is_not_presented_as_legal_zero(tmp_path, monkeypatch):
    expected_root = Path(__file__).resolve().parent.parent
    assert Path(server.__file__).resolve().parent == expected_root
    db_path = tmp_path / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,tdee,protein,today_date) VALUES (?,?,?,?,?)",
            ("U-UNKNOWN", "未知值會員", 1800, 100, today),
        )
        server.create_daily_food_log(
            conn,
            user_id="U-UNKNOWN",
            product_name="營養未知餐",
            meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00",
            servings=1,
            nutrition={"calories_kcal": None, "protein_g": 10},
            source_type="manual",
            operation_key="review-unknown-1",
            publish_catalog=False,
        )
        conn.commit()
    data = server.get_dashboard_data("U-UNKNOWN")
    assert data["food_list"] == ["營養未知餐"]
    rendered = build_customer_home_contents(data)
    text = list(_texts(rendered))
    print(json.dumps({"extra_cal": data["extra_cal"], "extra_pro": data["extra_pro"], "text": text}, ensure_ascii=False))
    assert data["extra_cal"] is None and data["extra_pro"] == 10
    assert "0" not in text
