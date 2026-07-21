import sqlite3

import pytest

from nutrition_system import (
    build_label_confirmation_bubble,
    confirm_pending_label,
    daily_consumed_totals,
    ensure_nutrition_schema,
    food_fingerprint,
    normalize_garmin_payload,
    normalize_label_payload,
    nutrition_sheet_specs,
    rank_menu_candidates,
    remaining_targets,
    save_pending_label,
    scale_nutrition,
)


SOY_MILK_PAYLOAD = {
    "status": "success",
    "image_type": "nutrition_label",
    "product_name": "無加糖濃豆漿",
    "brand": "",
    "package_amount": 375,
    "package_unit": "ml",
    "servings_per_package": 1,
    "per_serving": {
        "calories_kcal": 190.2,
        "protein_g": 19.1,
        "fat_g": 9.8,
        "carbohydrate_g": 7.9,
        "sugar_g": 2.6,
        "fiber_g": 3.0,
        "sodium_mg": 60,
    },
    "per_100": {
        "calories_kcal": 50.6,
        "protein_g": 5.1,
        "fat_g": 2.6,
        "carbohydrate_g": 2.1,
        "sugar_g": 0.7,
        "fiber_g": 0.8,
        "sodium_mg": 16,
    },
    "observed_at": "2026-07-19T21:53:00+08:00",
    "confidence": 0.96,
}


def test_normalize_label_payload_accepts_soy_milk_label():
    item = normalize_label_payload(SOY_MILK_PAYLOAD)
    assert item["product_name"] == "無加糖濃豆漿"
    assert item["package_amount"] == 375.0
    assert item["per_serving"]["protein_g"] == 19.1
    assert item["per_serving"]["sodium_mg"] == 60.0


def test_normalize_label_payload_rejects_negative_nutrition():
    payload = {**SOY_MILK_PAYLOAD, "per_serving": {**SOY_MILK_PAYLOAD["per_serving"], "protein_g": -1}}
    with pytest.raises(ValueError, match="protein_g"):
        normalize_label_payload(payload)


def test_scale_nutrition_supports_half_package():
    scaled = scale_nutrition(SOY_MILK_PAYLOAD["per_serving"], 0.5)
    assert scaled["calories_kcal"] == pytest.approx(95.1)
    assert scaled["protein_g"] == pytest.approx(9.55)
    assert scaled["sodium_mg"] == pytest.approx(30.0)


def test_food_fingerprint_is_stable_for_equivalent_values():
    a = food_fingerprint(" 無加糖濃豆漿 ", "", 375, "ml", SOY_MILK_PAYLOAD["per_serving"])
    b = food_fingerprint("無加糖濃豆漿", "", 375.0, "ML", dict(reversed(list(SOY_MILK_PAYLOAD["per_serving"].items()))))
    assert a == b


def test_remaining_targets_never_goes_below_zero():
    target = {"calories_kcal": 500, "protein_g": 30, "vegetable_exchange": 2}
    consumed = {"calories_kcal": 600, "protein_g": 19.1, "vegetable_exchange": 0.5}
    assert remaining_targets(target, consumed) == {
        "calories_kcal": 0.0,
        "protein_g": 10.9,
        "vegetable_exchange": 1.5,
    }


def test_rank_menu_candidates_prefers_closest_safe_meal():
    remaining = {"calories_kcal": 500, "protein_g": 35, "carbohydrate_g": 45, "fat_g": 18}
    candidates = [
        {"name": "A", "calories_kcal": 490, "protein_g": 34, "carbohydrate_g": 44, "fat_g": 18, "safe": True},
        {"name": "B", "calories_kcal": 300, "protein_g": 18, "carbohydrate_g": 15, "fat_g": 8, "safe": True},
        {"name": "C", "calories_kcal": 500, "protein_g": 35, "carbohydrate_g": 45, "fat_g": 18, "safe": False},
    ]
    ranked = rank_menu_candidates(remaining, candidates, limit=2)
    assert [x["name"] for x in ranked] == ["A", "B"]
    assert ranked[0]["match_score"] > ranked[1]["match_score"]


def test_ensure_nutrition_schema_creates_required_tables():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"food_catalog", "nutrition_plans", "nutrition_plan_slots", "food_logs", "pending_nutrition_logs"} <= tables


def test_sheet_specs_include_four_required_tabs_and_exchange_seed_rows():
    specs = nutrition_sheet_specs()
    assert set(specs) == {"營養份量規則", "食品資料庫", "客製化營養計畫", "飲食紀錄"}
    assert specs["食品資料庫"]["headers"][0:4] == ["food_id", "品名", "品牌", "條碼"]
    assert specs["飲食紀錄"]["headers"][0:3] == ["log_id", "User_ID", "food_id"]
    seed_codes = {row[0] for row in specs["營養份量規則"]["seed_rows"]}
    assert {"奶", "蛋", "主", "菜", "果", "油"} <= seed_codes


def test_confirmation_bubble_contains_nutrition_and_token_actions():
    label = normalize_label_payload(SOY_MILK_PAYLOAD)
    bubble = build_label_confirmation_bubble(label, token="abc123", consumed_servings=1)
    body_text = str(bubble)
    assert "無加糖濃豆漿" in body_text
    assert "190.2" in body_text
    assert "19.1" in body_text
    assert "確認營養紀錄:abc123" in body_text
    assert "修改營養時間:abc123" in body_text
    assert "取消營養紀錄:abc123" in body_text


def test_pending_label_confirmation_creates_private_food_and_snapshot_log():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn,
        user_id="U_TEST",
        payload=SOY_MILK_PAYLOAD,
        source_image_ref="line-message-123",
        consumed_servings=1,
        consumed_at="2026-07-19T21:53:00+08:00",
    )

    result = confirm_pending_label(conn, token=token, user_id="U_TEST")

    assert result["food"]["source_type"] == "user_private_food"
    assert result["log"]["nutrition"]["calories_kcal"] == pytest.approx(190.2)
    assert conn.execute("SELECT COUNT(*) FROM food_catalog").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0] == "confirmed"


def test_confirming_same_food_reuses_catalog_but_creates_another_log():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    for timestamp in ("2026-07-19T21:53:00+08:00", "2026-07-20T08:00:00+08:00"):
        token = save_pending_label(conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD, consumed_at=timestamp)
        confirm_pending_label(conn, token=token, user_id="U_TEST")
    assert conn.execute("SELECT COUNT(*) FROM food_catalog").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 2


def test_daily_consumed_totals_can_filter_meal_slot():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    for meal_slot in ("早餐", "晚餐"):
        token = save_pending_label(
            conn,
            user_id="U_TEST",
            payload=SOY_MILK_PAYLOAD,
            meal_slot=meal_slot,
            consumed_at="2026-07-19T08:00:00+08:00",
        )
        confirm_pending_label(conn, token=token, user_id="U_TEST")
    breakfast = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19", meal_slot="早餐")
    assert breakfast["calories_kcal"] == pytest.approx(190.2)


def test_daily_consumed_totals_sums_confirmed_logs_only():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    for servings in (1, 0.5):
        token = save_pending_label(
            conn,
            user_id="U_TEST",
            payload=SOY_MILK_PAYLOAD,
            consumed_servings=servings,
            consumed_at="2026-07-19T21:53:00+08:00",
        )
        confirm_pending_label(conn, token=token, user_id="U_TEST")
    totals = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert totals["calories_kcal"] == pytest.approx(285.3)
    assert totals["protein_g"] == pytest.approx(28.65)


def test_rejects_nan_infinity_zero_and_inconsistent_labels():
    for bad in ("nan", "inf", "-inf"):
        payload = {**SOY_MILK_PAYLOAD, "per_serving": {**SOY_MILK_PAYLOAD["per_serving"], "protein_g": bad}}
        with pytest.raises(ValueError):
            normalize_label_payload(payload)
    zero = {**SOY_MILK_PAYLOAD, "per_serving": {key: 0 for key in SOY_MILK_PAYLOAD["per_serving"]}}
    with pytest.raises(ValueError, match="營養"):
        normalize_label_payload(zero)
    inconsistent = {**SOY_MILK_PAYLOAD, "per_100": {**SOY_MILK_PAYLOAD["per_100"], "calories_kcal": 500}}
    with pytest.raises(ValueError, match="每份.*每100"):
        normalize_label_payload(inconsistent)


def test_pending_creation_is_idempotent_by_line_message_id_and_confirmation_replay():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    kwargs = dict(user_id="U_TEST", payload=SOY_MILK_PAYLOAD, source_message_id="LINE_123")
    token1 = save_pending_label(conn, **kwargs)
    token2 = save_pending_label(conn, **kwargs)
    assert token1 == token2
    first = confirm_pending_label(conn, token=token1, user_id="U_TEST", plan_id="plan_v3")
    replay = confirm_pending_label(conn, token=token1, user_id="U_TEST", plan_id="wrong_plan")
    assert first["log"]["plan_id"] == "plan_v3"
    assert replay["log"]["plan_id"] == "plan_v3"
    assert replay["already_confirmed"] is True
    assert replay["log"]["log_id"] == first["log"]["log_id"]
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1


def test_fingerprint_distinguishes_barcode_servings_and_per100():
    base = dict(product_name="豆漿", brand="A", package_amount=375, package_unit="ml", per_serving=SOY_MILK_PAYLOAD["per_serving"])
    a = food_fingerprint(**base, barcode="111", servings_per_package=1, per_100=SOY_MILK_PAYLOAD["per_100"])
    b = food_fingerprint(**base, barcode="222", servings_per_package=1, per_100=SOY_MILK_PAYLOAD["per_100"])
    c = food_fingerprint(**base, barcode="111", servings_per_package=2, per_100=SOY_MILK_PAYLOAD["per_100"])
    assert len({a, b, c}) == 3


def test_macro_ranking_ignores_unavailable_exchange_keys():
    remaining = {"starch_exchange": 3, "calories_kcal": 500, "protein_g": 35, "carbohydrate_g": 45, "fat_g": 18}
    perfect_macros = {"name": "perfect", "calories_kcal": 500, "protein_g": 35, "carbohydrate_g": 45, "fat_g": 18}
    ranked = rank_menu_candidates(remaining, [perfect_macros])
    assert ranked[0]["match_score"] == 100.0


def test_normalize_garmin_payload_rejects_missing_and_out_of_range_values():
    valid = normalize_garmin_payload({
        "status": "success", "image_type": "garmin_workout", "workout_type": "跑步",
        "duration_min": 60, "avg_hr": 150, "max_hr": 180, "aerobic_te": 3.2,
        "anaerobic_te": 1.0, "primary_benefit": "有氧", "load_value": 150,
        "np_w": 0, "if_value": 0, "tss": 0, "ftp_w": 0,
    })
    assert valid["duration_min"] == 60
    no_hr = normalize_garmin_payload({
        "status": "success", "image_type": "garmin_workout", "workout_type": "其他",
        "duration_min": 30, "aerobic_te": 1.0, "anaerobic_te": 0,
        "primary_benefit": "恢復", "load_value": 20,
    })
    assert no_hr["avg_hr"] == 0
    assert no_hr["max_hr"] == 0
    with pytest.raises(ValueError):
        normalize_garmin_payload({"status": "success", "image_type": "garmin_workout", "workout_type": "跑步"})
    with pytest.raises(ValueError):
        normalize_garmin_payload({**valid, "avg_hr": 999})
