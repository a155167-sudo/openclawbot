"""Regression: actual label-confirm handler must acknowledge the committed date."""
import json
import sqlite3
from types import SimpleNamespace

import server
from nutrition_system import ensure_nutrition_schema, save_pending_label


def test_backdated_label_handler_acknowledges_committed_row_and_replay(tmp_path, monkeypatch):
    db = tmp_path / 'health.db'
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    monkeypatch.setattr(server, 'DB_DIR', str(tmp_path))
    monkeypatch.setattr(server, 'gc', None)
    server.init_db()
    fixed = server.datetime(2026, 9, 17, 8, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, 'tw_now', lambda: fixed)
    label = {
        'status': 'success', 'image_type': 'nutrition_label',
        'product_name': '補登鮪魚蛋吐司', 'brand': '測試', 'barcode': '',
        'package_amount': 1, 'package_unit': '份', 'servings_per_package': 1,
        'per_serving': {'calories_kcal': 350, 'protein_g': 17, 'fat_g': 10, 'carbohydrate_g': 30},
        'per_100': {}, 'confidence': 0.99,
    }
    uid = 'U-H3-REGRESSION'
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            'INSERT INTO health_profile '
            '(user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date) '
            "VALUES (?, '測試',2000,100,0,0,'','2026-09-17')", (uid,),
        )
        conn.commit()
        token = save_pending_label(
            conn, user_id=uid, payload=label, meal_slot='早餐',
            consumed_at='2026-09-16T08:00:00+08:00', consumed_time_source='manual',
        )
    monkeypatch.setattr(server, 'get_active_nutrition_target', lambda *a, **k: None)
    monkeypatch.setattr(server, 'sync_confirmed_nutrition_to_sheet', lambda *a, **k: None)
    monkeypatch.setattr(server, 'apply_confirmed_nutrition_to_legacy_dashboard', lambda *a, **k: None)
    replies = []
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda t, m: replies.append(m))
    event = SimpleNamespace(
        source=SimpleNamespace(user_id=uid),
        message=SimpleNamespace(id='H3-CONFIRM-REGRESSION', text=f'確認營養紀錄:{token}'),
        reply_token='offline-reply',
    )
    # Replay after clearing only the process cache: durable storage must deduplicate.
    for _ in range(2):
        server.processed_messages.clear()
        server._handle_message_impl(event)
        rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
        for expected in ('已補登 2026-09-16', '補登鮪魚蛋吐司', '350 kcal', '17 g'):
            assert expected in rendered
        assert '已攝取 0 kcal' in rendered  # Yesterday must not inflate today's totals.
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                'SELECT consumed_at,nutrition_snapshot_json FROM food_logs WHERE user_id=?', (uid,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0].startswith('2026-09-16')
        nutrition = json.loads(rows[0][1])
        assert nutrition['calories_kcal'] == 350
        assert nutrition['protein_g'] == 17
