"""菜單營養一致：一般對話、搜尋卡、食品庫同步都用校正後的 menu.csv 數字。"""
import json
import sqlite3

import server

DISHES = [
    {"name": "豆腐", "cal": 59, "pro": 5.2, "fat": 2.0, "carbs": 5.1, "ingredients": "滷豆腐",
     "calories_kcal": 59, "protein_g": 5.2, "fat_g": 2.0, "carbohydrate_g": 5.1, "category": "side"},
    {"name": "豆腐食蔬", "cal": 475.605, "pro": 26.729, "fat": 12.242, "carbs": 66.8395,
     "ingredients": "日式滷豆腐,地瓜", "calories_kcal": 475.605, "protein_g": 26.729,
     "fat_g": 12.242, "carbohydrate_g": 66.8395, "category": "main"},
]


def test_chat_memo_gives_menu_nutrition_and_prefers_longest_name(monkeypatch):
    monkeypatch.setattr(server, "MAIN_DISHES", DISHES)
    memo = server._menu_nutrition_memo("豆腐食蔬熱量多少", False)
    assert "豆腐食蔬（本店菜單數值，每份）：熱量 475.605 大卡" in memo
    assert "脂肪 12.242 克" in memo and "碳水 66.8395 克" in memo
    assert "- 豆腐（" not in memo  # 不再誤配單品「豆腐」
    assert "- 豆腐（本店菜單數值" in server._menu_nutrition_memo("滷豆腐一塊熱量", False)


def test_sync_updates_existing_menu_rows_to_calibrated_numbers(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "MAIN_DISHES", DISHES)
    with sqlite3.connect(db) as conn:
        server.ensure_nutrition_schema(conn)
    server.sync_menu_to_food_catalog()
    stale = {"calories_kcal": 484, "protein_g": 35, "fat_g": 19, "carbohydrate_g": 17}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE food_catalog SET per_serving_json=? WHERE product_name='豆腐食蔬'",
                     (json.dumps(stale),))
    server.sync_menu_to_food_catalog()
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT per_serving_json FROM food_catalog WHERE product_name='豆腐食蔬'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["calories_kcal"] == 475.605


def test_search_card_shows_all_four_and_official_label():
    item = {"product_name": "雞肉便當", "food_id": "menu_x", "source_type": "label",
            "owner_user_id": "system",
            "per_serving": {"calories_kcal": 604.585, "protein_g": 43.16, "fat_g": 14.289,
                            "carbohydrate_g": 77.2055}}
    text = json.dumps(server.build_food_search_result_bubble(item), ensure_ascii=False)
    assert "一日樂食餐點" in text and "包裝食品" not in text
    for label, value in (("每份熱量", "605 kcal"), ("每份蛋白質", "43.2g"),
                         ("每份脂肪", "14.3g"), ("每份碳水", "77.2g")):
        assert label in text and value in text
