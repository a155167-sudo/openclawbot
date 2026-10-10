import json
import sqlite3
from types import SimpleNamespace

import server


UID = "U-PHONE-COLLISION"
OLD_ID = "ledger_05b180c3feecf0883dff3d15"
MENU_ID = "menu_2cedebcbd8324aef"
NAME = "雞肉便當"
OLD = {"calories_kcal": 484, "protein_g": 35, "fat_g": 19, "carbohydrate_g": 17}
NEW = {"calories_kcal": 604.585, "protein_g": 43.16, "fat_g": 14.289, "carbohydrate_g": 77.2055}


def _text(message_id, text):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=UID), reply_token=f"r-{message_id}",
        webhook_event_id=f"w-{message_id}",
    )


def _postback(data, event_id):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id=UID),
        reply_token=f"r-{event_id}", webhook_event_id=event_id, timestamp=0,
    )


def _actions(message):
    found = []
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") in {"postback", "uri"}:
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(message.as_json_dict())
    return found


def _insert_food(conn, food_id, owner, visibility, values, *, source_type="official_menu", name=NAME):
    now = "2026-10-09T01:30:00+08:00"
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,
            per_100_json,exchange_json,exchange_review_status,fingerprint,
            verification_status,created_at,updated_at,menu_category)
           VALUES (?,?,?,?,?,1,'serving',1,?,'{}','{}','approved',?,
                   'verified',?,?,'main')""",
        (food_id, name, source_type, owner, visibility, json.dumps(values), food_id, now, now),
    )


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "phone-menu-collision.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES (?,3,10,?,'vip','2099-12-31',3)""",
            (UID, server.tw_today().isoformat()),
        )
        _insert_food(conn, OLD_ID, UID, "ledger_internal", OLD)
        # This is the pre-existing historical intake; it must remain byte-for-byte nutritional history.
        server.quick_log_from_catalog(
            conn, user_id=UID, food_id=OLD_ID, consumed_servings=1,
            meal_slot="點心", consumed_at="2026-10-09T01:44:00+08:00",
            manage_transaction=False,
        )
        _insert_food(conn, MENU_ID, "system", "public", NEW, source_type="label")
        _insert_food(
            conn, "private_customer_oatmeal", UID, "private",
            {"calories_kcal": 210, "protein_g": 8, "fat_g": 4, "carbohydrate_g": 36},
            source_type="nutrition_label", name="我的燕麥杯",
        )
        conn.commit()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, payload: replies.append(payload))
    monkeypatch.setattr(server, "_refresh_health_check_after_food_log", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "build_dashboard_flex", lambda _uid: server.TextSendMessage(text="既有總覽"))
    monkeypatch.setattr(server, "current_meal_slot", lambda *_a, **_k: "點心")
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("menu must not call AI")))
    monkeypatch.setattr(server, "_charge_text_meal_estimate_quota", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("menu must not debit quota")))
    server.processed_messages.clear()
    return db, replies


def _log_snapshots(db):
    with sqlite3.connect(db) as conn:
        rows = [json.loads(row[0]) for row in conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs ORDER BY consumed_at,log_id"
        )]
    return [
        {key: row[key] for key in ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")}
        for row in rows
    ]


def test_registered_name_collision_uses_public_menu_draft_then_confirms_exactly_once(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)

    server.handle_message(_text("ENTER", "我要紀錄飲食"))
    server.handle_message(_text("NAME", NAME))
    picker = replies[-1]
    one = next(a["data"] for a in _actions(picker) if a.get("data", "").endswith(":servings:1:meal:點心"))
    assert MENU_ID in one
    server.handle_postback_event(_postback(one, "PICK"))

    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert all(value in rendered for value in ("尚未記錄", "605", "43.2", "14.3", "77.2", "一日樂食餐點"))
    assert _log_snapshots(db) == [OLD]

    confirm = next(a["data"] for a in _actions(replies[-1]) if a.get("data", "").endswith(":confirm"))
    event = _postback(confirm, "CONFIRM")
    server.handle_postback_event(event)
    server.handle_postback_event(event)
    assert _log_snapshots(db) == [OLD, NEW]


def test_old_ledger_internal_serving_card_is_rejected_without_write(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    server.handle_postback_event(_postback(
        f"nlfood:v1:{OLD_ID}:servings:1:meal:點心", "OLD-CARD"
    ))
    assert "尚未記錄" in replies[-1].text
    assert _log_snapshots(db) == [OLD]


def test_collision_menu_cancel_and_amount_edit_do_not_write(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    server.handle_postback_event(_postback(
        f"nlfood:v1:{MENU_ID}:servings:1:meal:點心", "MENU-EDIT"
    ))
    confirm = next(a["data"] for a in _actions(replies[-1]) if a.get("data", "").endswith(":confirm"))
    parts = confirm.split(":")
    revised = server.apply_text_meal_custom_input(
        user_id=UID, token=parts[2], expected_version=int(parts[3]),
        mode="amount", text="1.5 份",
    )
    assert revised["estimate"]["basis_amount"] == 1.5
    cancel = f"tmest:v3:{parts[2]}:{revised['version']}:cancel"
    server.handle_postback_event(_postback(cancel, "MENU-CANCEL"))
    assert "沒有寫入" in replies[-1].text
    assert _log_snapshots(db) == [OLD]


def test_fuzzy_excludes_internal_but_private_customer_food_remains_searchable(tmp_path, monkeypatch):
    db, _ = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        collision, kind = server._natural_food_candidates(conn, UID, "雞肉")
        private, private_kind = server._natural_food_candidates(conn, UID, "燕麥")
    assert kind == "fuzzy"
    assert [item["food_id"] for item in collision] == [MENU_ID]
    assert private_kind == "fuzzy"
    assert [item["food_id"] for item in private] == ["private_customer_oatmeal"]

