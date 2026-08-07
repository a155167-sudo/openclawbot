import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from nutrition_system import (
    apply_nutrition_text_edit,
    approve_food_exchange_suggestion,
    attach_latest_pending_identity,
    attach_pending_identity,
    build_label_confirmation_bubble,
    cancel_pending_label,
    confirm_pending_label,
    daily_consumed_totals,
    daily_food_summary,
    ensure_nutrition_schema,
    estimate_nutrition_from_exchanges,
    food_fingerprint,
    insert_approved_meal_photo_log,
    get_latest_awaiting_identity,
    get_nutrition_input_state,
    new_id,
    normalize_garmin_payload,
    normalize_label_payload,
    normalize_product_identity_payload,
    nutrition_consistency_warnings,
    nutrition_sheet_specs,
    rank_menu_candidates,
    quick_log_from_catalog,
    remaining_targets,
    save_pending_label,
    scale_nutrition,
    search_food_catalog,
    search_food_history,
    suggest_exchange_portions,
    set_nutrition_input_state,
    clear_nutrition_input_state,
    update_pending_label_name,
    update_pending_label_nutrient,
    update_pending_consumption,
    utcish_now,
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
    chinese_unit = normalize_label_payload({**SOY_MILK_PAYLOAD, "package_unit": "毫升"})
    assert chinese_unit["package_unit"] == "ml"


@pytest.mark.parametrize("bad_confidence", [float("nan"), float("inf"), -1, 2, "錯誤"])
def test_invalid_optional_observed_time_confidence_does_not_reject_nutrition_label(
    bad_confidence,
):
    payload = {**SOY_MILK_PAYLOAD, "observed_at_confidence": bad_confidence}
    normalized = normalize_label_payload(payload)
    assert normalized["observed_at_confidence"] == 0


def test_normalize_label_payload_rejects_negative_nutrition():
    payload = {**SOY_MILK_PAYLOAD, "per_serving": {**SOY_MILK_PAYLOAD["per_serving"], "protein_g": -1}}
    with pytest.raises(ValueError, match="protein_g"):
        normalize_label_payload(payload)


def test_scale_nutrition_supports_half_package():
    scaled = scale_nutrition(SOY_MILK_PAYLOAD["per_serving"], 0.5)
    assert scaled["calories_kcal"] == pytest.approx(95.1)
    assert scaled["protein_g"] == pytest.approx(9.55)
    assert scaled["sodium_mg"] == pytest.approx(30.0)


def test_suggest_exchange_portions_splits_soy_milk_without_counting_fat_exchange():
    result = suggest_exchange_portions(
        product_name="高蛋白無加糖豆漿",
        nutrition={"calories_kcal": 228, "protein_g": 21.2, "fat_g": 10.9, "carbohydrate_g": 13.9},
    )

    assert result["categories"] == ["protein", "starch"]
    assert result["exchanges"] == {
        "milk_exchange": 0.0,
        "protein_low_exchange": 3.03,
        "protein_medium_exchange": 0.0,
        "protein_high_exchange": 0.0,
        "starch_exchange": 0.93,
        "vegetable_exchange": 0.0,
        "fruit_exchange": 0.0,
        "fat_exchange": 0.0,
    }
    assert result["review_status"] == "pending_review"
    assert result["rule_version"] == "tw-exchange-v1"


def test_suggest_exchange_portions_does_not_create_protein_for_energy_drink():
    result = suggest_exchange_portions(
        product_name="Monster 無糖能量飲料",
        nutrition={"calories_kcal": 11.4, "protein_g": 0, "fat_g": 0, "carbohydrate_g": 2.8},
    )

    assert result["categories"] == ["starch"]
    assert result["exchanges"]["starch_exchange"] == 0.19
    assert result["exchanges"]["protein_low_exchange"] == 0.0


def test_suggest_exchange_portions_keeps_milk_carbohydrate_inside_milk_exchange():
    result = suggest_exchange_portions(
        product_name="低脂鮮乳",
        nutrition={"calories_kcal": 120, "protein_g": 8, "fat_g": 4, "carbohydrate_g": 12},
    )

    assert result["categories"] == ["milk"]
    assert result["exchanges"]["milk_exchange"] == 1.0
    assert result["exchanges"]["starch_exchange"] == 0.0
    assert result["exchanges"]["fat_exchange"] == 0.0


def test_suggest_exchange_portions_never_reuses_carbohydrate_across_categories():
    juice = suggest_exchange_portions(
        product_name="蘋果汁飲料",
        nutrition={"calories_kcal": 120, "protein_g": 0, "fat_g": 0, "carbohydrate_g": 30},
    )
    mixed = suggest_exchange_portions(
        product_name="雞肉蔬菜飯",
        nutrition={"calories_kcal": 400, "protein_g": 21, "fat_g": 8, "carbohydrate_g": 45},
    )
    assert juice["categories"] == ["fruit"]
    assert juice["exchanges"]["fruit_exchange"] == 2.0
    assert juice["exchanges"]["starch_exchange"] == 0.0
    assert mixed["categories"] == ["protein", "starch"]
    assert mixed["exchanges"]["starch_exchange"] == 3.0
    assert mixed["exchanges"]["vegetable_exchange"] == 0.0
    assert "ambiguous_carbohydrate_category" in mixed["warnings"]


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
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute(
        "SELECT version FROM nutrition_schema_versions WHERE component='nutrition_system'"
    ).fetchone()[0] == 4


def test_schema_migration_deduplicates_legacy_line_message_ids_before_unique_indexes():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    first = save_pending_label(
        conn, user_id="U_DUP", payload=SOY_MILK_PAYLOAD, source_message_id="BACK_A"
    )
    second = save_pending_label(
        conn, user_id="U_DUP", payload=SOY_MILK_PAYLOAD, source_message_id="BACK_B"
    )
    conn.execute("DROP INDEX idx_pending_source_message")
    conn.execute("DROP INDEX idx_pending_identity_message")
    conn.execute(
        "UPDATE pending_nutrition_logs SET identity_message_id='FRONT_DUP' WHERE token IN (?,?)",
        (first, second),
    )
    conn.execute(
        "UPDATE pending_nutrition_logs SET source_message_id='BACK_DUP' WHERE token IN (?,?)",
        (first, second),
    )
    conn.execute(
        "DELETE FROM nutrition_schema_versions WHERE component='nutrition_system'"
    )
    conn.commit()
    ensure_nutrition_schema(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM pending_nutrition_logs WHERE source_message_id='BACK_DUP'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM pending_nutrition_logs WHERE identity_message_id='FRONT_DUP'"
    ).fetchone()[0] == 1
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {"idx_pending_source_message", "idx_pending_identity_message"} <= indexes


def test_existing_v1_marker_runs_latest_additive_migration():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE nutrition_schema_versions (
            component TEXT PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL
        );
        INSERT INTO nutrition_schema_versions VALUES ('nutrition_system',1,'legacy');
        CREATE TABLE nutrition_sheet_outbox (
            outbox_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '', created_at TEXT NOT NULL, synced_at TEXT DEFAULT ''
        );
        """
    )
    ensure_nutrition_schema(conn)
    outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(nutrition_sheet_outbox)")}
    log_columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
    assert {"claimed_at", "lease_owner", "resync_required"} <= outbox_columns
    assert {"approved_exchange_json", "exchange_approval_id"} <= log_columns
    pending_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_nutrition_logs)")
    }
    assert "consumed_time_source" in pending_columns
    assert conn.execute(
        "SELECT version FROM nutrition_schema_versions WHERE component='nutrition_system'"
    ).fetchone()[0] == 4


def test_existing_v2_marker_runs_v3_time_source_migration():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    conn.execute("ALTER TABLE pending_nutrition_logs DROP COLUMN consumed_time_source")
    conn.execute(
        "UPDATE nutrition_schema_versions SET version=2 WHERE component='nutrition_system'"
    )
    conn.commit()
    ensure_nutrition_schema(conn)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_nutrition_logs)")
    }
    assert "consumed_time_source" in columns
    assert conn.execute(
        "SELECT version FROM nutrition_schema_versions WHERE component='nutrition_system'"
    ).fetchone()[0] == 4


def test_sheet_specs_include_four_required_tabs_and_exchange_seed_rows():
    specs = nutrition_sheet_specs()
    assert set(specs) == {"營養份量規則", "食品資料庫", "客製化營養計畫", "飲食紀錄"}
    assert specs["食品資料庫"]["headers"][0:4] == ["food_id", "品名", "品牌", "條碼"]
    assert specs["飲食紀錄"]["headers"][0:3] == ["log_id", "User_ID", "food_id"]
    seed_codes = {row[0] for row in specs["營養份量規則"]["seed_rows"]}
    assert {"奶", "蛋", "主", "菜", "果", "油"} <= seed_codes


def test_confirmation_bubble_contains_nutrition_and_token_actions():
    label = normalize_label_payload(SOY_MILK_PAYLOAD)
    bubble = build_label_confirmation_bubble(
        label,
        token="abc123",
        consumed_servings=1,
        consumed_at="2026-07-21T20:42:00+08:00",
        consumed_time_source="photo_timestamp",
    )
    body_text = str(bubble)
    assert "無加糖濃豆漿" in body_text
    assert "190.2" in body_text
    assert "19.1" in body_text
    assert "確認營養紀錄:abc123" in body_text
    assert "修改營養時間:abc123" in body_text
    assert "進食時間：2026/07/21 20:42（照片時間）" in body_text
    assert "取消營養紀錄:abc123" in body_text
    assert "推算營養份數" in body_text
    assert "低脂蛋白 2.73份" in body_text
    assert "主食 0.53份" in body_text
    assert "尚未扣入個人計畫" in body_text
    assert "油脂份不計" in body_text
    manual_bubble = build_label_confirmation_bubble(
        label,
        token="abc123",
        consumed_at="2026-07-21T20:42:00+08:00",
        consumed_time_source="manual",
    )
    assert "進食時間：2026/07/21 20:42（手動設定）" in str(manual_bubble)


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
    assert result["log"]["exchange"]["protein_low_exchange"] == 2.73
    assert result["log"]["exchange"]["starch_exchange"] == 0.53
    assert result["log"]["exchange"]["fat_exchange"] == 0.0
    assert result["log"]["exchange"]["_review_status"] == "pending_review"
    assert result["log"]["exchange"]["_rule_version"] == "tw-exchange-v1"
    assert result["log"]["exchange"]["_categories"] == ["protein", "starch"]
    assert conn.execute(
        "SELECT exchange_review_status FROM food_catalog"
    ).fetchone()[0] == "pending_review"
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
    assert totals["protein_low_exchange"] == 0.0
    assert totals["starch_exchange"] == 0.0
    conn.execute("UPDATE food_catalog SET exchange_review_status='approved'")
    conn.commit()
    still_pending = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert still_pending["protein_low_exchange"] == 0.0
    assert still_pending["starch_exchange"] == 0.0


def test_daily_food_summary_is_scoped_sorted_and_counts_pending_reviews():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)

    first_token = save_pending_label(
        conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T09:05:00+08:00",
    )
    first = confirm_pending_label(conn, token=first_token, user_id="U_TEST")
    approve_food_exchange_suggestion(
        conn, food_id=first["food"]["food_id"], reviewer="ADMIN"
    )

    second_payload = {**SOY_MILK_PAYLOAD, "product_name": "另一款無糖豆漿", "package_amount": 400}
    second_token = save_pending_label(
        conn, user_id="U_TEST", payload=second_payload,
        consumed_at="2026-07-19T13:10:00+08:00",
    )
    confirm_pending_label(conn, token=second_token, user_id="U_TEST")
    other_token = save_pending_label(
        conn, user_id="OTHER", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T08:00:00+08:00",
    )
    confirm_pending_label(conn, token=other_token, user_id="OTHER")

    summary = daily_food_summary(conn, user_id="U_TEST", date_iso="2026-07-19")

    assert [item["time"] for item in summary["foods"]] == ["09:05", "13:10"]
    assert [item["name"] for item in summary["foods"]] == ["無加糖濃豆漿", "另一款無糖豆漿"]
    assert summary["totals"]["calories_kcal"] == pytest.approx(380.4)
    assert summary["totals"]["protein_low_exchange"] == pytest.approx(2.73)
    assert summary["pending_reviews"] == 1


def test_approving_exchange_suggestion_preserves_suggestion_and_creates_applied_snapshot():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T21:53:00+08:00",
    )
    confirmed = confirm_pending_label(conn, token=token, user_id="U_TEST")
    food_id = confirmed["food"]["food_id"]
    original_suggestion = conn.execute(
        "SELECT exchange_snapshot_json FROM food_logs WHERE log_id=?",
        (confirmed["log"]["log_id"],),
    ).fetchone()[0]

    first = approve_food_exchange_suggestion(conn, food_id=food_id, reviewer="ADMIN")
    replay = approve_food_exchange_suggestion(conn, food_id=food_id, reviewer="ADMIN")

    assert first["already_approved"] is False
    assert replay["already_approved"] is True
    assert first["updated_logs"] == 1
    assert replay["updated_logs"] == 0
    totals = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert totals["protein_low_exchange"] == 2.73
    assert totals["starch_exchange"] == 0.53
    food_status, food_exchange_json = conn.execute(
        "SELECT exchange_review_status,exchange_json FROM food_catalog WHERE food_id=?", (food_id,)
    ).fetchone()
    assert food_status == "approved"
    assert __import__("json").loads(food_exchange_json)["_review_status"] == "pending_review"
    log_exchange_json, approved_exchange_json, approval_id = conn.execute(
        """SELECT exchange_snapshot_json,approved_exchange_json,exchange_approval_id
           FROM food_logs WHERE food_id=?""", (food_id,)
    ).fetchone()
    assert log_exchange_json == original_suggestion
    approved = __import__("json").loads(approved_exchange_json)
    assert approved["protein_low_exchange"] == 2.73
    assert approved["starch_exchange"] == 0.53
    assert approval_id.startswith("approval_")
    approval = conn.execute(
        """SELECT food_fingerprint,approved_exchange_hash,reviewer
           FROM food_exchange_approvals WHERE approval_id=?""", (approval_id,)
    ).fetchone()
    assert approval[0]
    assert len(approval[1]) == 64
    assert approval[2] == "ADMIN"
    assert set(conn.execute(
        "SELECT entity_type,status FROM nutrition_sheet_outbox WHERE entity_id IN (?,?)",
        (food_id, confirmed["log"]["log_id"]),
    ).fetchall()) == {("food", "pending"), ("food_log", "pending")}


def test_reusing_valid_approved_food_keeps_suggestion_separate_and_applies_approval():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    first_token = save_pending_label(
        conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T08:00:00+08:00",
    )
    first = confirm_pending_label(conn, token=first_token, user_id="U_TEST")
    approve_food_exchange_suggestion(conn, food_id=first["food"]["food_id"], reviewer="ADMIN")
    second_token = save_pending_label(
        conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T12:00:00+08:00",
    )
    second = confirm_pending_label(conn, token=second_token, user_id="U_TEST")
    assert second["log"]["exchange_review_status"] == "approved"
    assert second["log"]["suggested_exchange"]["_review_status"] == "pending_review"
    assert second["log"]["approved_exchange"]["protein_low_exchange"] == 2.73
    totals = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert totals["protein_low_exchange"] == 5.46
    tampered = __import__("json").loads(conn.execute(
        "SELECT approved_exchange_json FROM food_logs WHERE log_id=?",
        (second["log"]["log_id"],),
    ).fetchone()[0])
    tampered["protein_low_exchange"] = 99
    conn.execute(
        "UPDATE food_logs SET approved_exchange_json=? WHERE log_id=?",
        (__import__("json").dumps(tampered), second["log"]["log_id"]),
    )
    conn.commit()
    tampered_result = __import__("nutrition_system")._confirmed_result(
        conn, second["log"]["log_id"], already_confirmed=True
    )
    assert tampered_result["log"]["exchange_review_status"] == "pending_review"
    assert tampered_result["log"]["approved_exchange"] == {}
    fail_closed = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert fail_closed["protein_low_exchange"] == 2.73


def test_tampered_approval_hash_fails_closed_for_daily_totals_and_future_logs():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T08:00:00+08:00",
    )
    first = confirm_pending_label(conn, token=token, user_id="U_TEST")
    approve_food_exchange_suggestion(conn, food_id=first["food"]["food_id"], reviewer="ADMIN")
    conn.execute("UPDATE food_exchange_approvals SET approved_exchange_hash='tampered'")
    conn.commit()
    totals = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert totals["protein_low_exchange"] == 0.0
    next_token = save_pending_label(
        conn, user_id="U_TEST", payload=SOY_MILK_PAYLOAD,
        consumed_at="2026-07-19T12:00:00+08:00",
    )
    second = confirm_pending_label(conn, token=next_token, user_id="U_TEST")
    assert second["log"]["exchange_review_status"] == "pending_review"
    replacement = approve_food_exchange_suggestion(
        conn, food_id=first["food"]["food_id"], reviewer="ADMIN_2"
    )
    assert replacement["updated_logs"] == 2
    restored = daily_consumed_totals(conn, user_id="U_TEST", date_iso="2026-07-19")
    assert restored["protein_low_exchange"] == 5.46
    assert conn.execute("SELECT COUNT(*) FROM food_exchange_approvals").fetchone()[0] == 2


def test_rejects_nan_infinity_zero_and_flags_inconsistent_labels():
    for bad in ("nan", "inf", "-inf"):
        payload = {**SOY_MILK_PAYLOAD, "per_serving": {**SOY_MILK_PAYLOAD["per_serving"], "protein_g": bad}}
        with pytest.raises(ValueError):
            normalize_label_payload(payload)
    zero = {**SOY_MILK_PAYLOAD, "per_serving": {key: 0 for key in SOY_MILK_PAYLOAD["per_serving"]}}
    with pytest.raises(ValueError, match="營養"):
        normalize_label_payload(zero)
    inconsistent = {**SOY_MILK_PAYLOAD, "per_100": {**SOY_MILK_PAYLOAD["per_100"], "calories_kcal": 500}}
    normalized = normalize_label_payload(inconsistent)
    assert "calories_kcal" in nutrition_consistency_warnings(normalized)


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


def incomplete_soy_label():
    return {
        "status": "success", "image_type": "nutrition_label", "product_name": "", "brand": "",
        "barcode": "4710126206298", "package_amount": 400, "package_unit": "ml",
        "servings_per_package": 1,
        "per_serving": {
            "calories_kcal": 228, "protein_g": 21.2, "fat_g": 10.9,
            "saturated_fat_g": 1.7, "trans_fat_g": 0, "cholesterol_mg": 0,
            "carbohydrate_g": 13.9, "sugar_g": 4.3, "fiber_g": 5.2, "sodium_mg": 48,
        },
        "per_100": {
            "calories_kcal": 57, "protein_g": 5.3, "fat_g": 2.725,
            "saturated_fat_g": 0.425, "trans_fat_g": 0, "cholesterol_mg": 0,
            "carbohydrate_g": 3.475, "sugar_g": 1.075, "fiber_g": 1.3, "sodium_mg": 12,
        },
        "observed_at": "2026-07-21T20:42:00+08:00", "confidence": 0.9,
    }


def test_missing_identity_draft_pairs_only_with_same_user():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U1", payload=incomplete_soy_label(), source_message_id="M_BACK",
        allow_missing_identity=True,
    )
    assert conn.execute("SELECT status FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0] == "awaiting_identity"
    assert get_latest_awaiting_identity(conn, user_id="U1")["token"] == token
    assert get_latest_awaiting_identity(conn, user_id="U2") is None
    with pytest.raises(ValueError):
        attach_pending_identity(
            conn, user_id="U2", token=token,
            identity={"status": "success", "image_type": "product_front", "product_name": "高蛋白豆漿", "brand": "測試品牌", "barcode": "4710126206298", "confidence": 0.98},
        )
    label = attach_pending_identity(
        conn, user_id="U1", token=token,
        identity={"status": "success", "image_type": "product_front", "product_name": "高蛋白豆漿", "brand": "測試品牌", "barcode": "4710126206298", "confidence": 0.98},
    )
    assert label["product_name"] == "高蛋白豆漿"
    assert label["brand"] == "測試品牌"
    assert conn.execute("SELECT status FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0] == "pending"


def test_expired_identity_draft_cannot_be_paired():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(conn, user_id="U1", payload=incomplete_soy_label(), allow_missing_identity=True)
    conn.execute("UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (token,))
    conn.commit()
    assert get_latest_awaiting_identity(conn, user_id="U1") is None
    with pytest.raises(ValueError, match="逾時"):
        attach_pending_identity(
            conn, user_id="U1", token=token,
            identity={"status": "success", "image_type": "product_front", "product_name": "豆漿", "confidence": 0.9},
        )


def test_pending_name_and_nutrient_are_editable_before_confirmation():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    payload = {**incomplete_soy_label(), "product_name": "高蛋白豆漿"}
    token = save_pending_label(conn, user_id="U1", payload=payload)
    renamed = update_pending_label_name(conn, user_id="U1", token=token, product_name="無加糖高蛋白豆漿")
    assert renamed["product_name"] == "無加糖高蛋白豆漿"
    corrected = update_pending_label_nutrient(conn, user_id="U1", token=token, field="sodium_mg", value=48)
    assert corrected["per_serving"]["sodium_mg"] == 48
    assert corrected["per_100"]["sodium_mg"] == 12
    with pytest.raises(ValueError):
        update_pending_label_nutrient(conn, user_id="U1", token=token, field="caffeine_mg", value=100)


def test_product_front_identity_validation_and_confirmation_edit_actions():
    identity = normalize_product_identity_payload({
        "status": "success", "image_type": "product_front", "product_name": "Monster Ultra",
        "brand": "Monster", "barcode": "", "confidence": 0.95,
    })
    assert identity["product_name"] == "Monster Ultra"
    with pytest.raises(ValueError):
        normalize_product_identity_payload({"status": "success", "image_type": "product_front", "product_name": ""})
    bubble = build_label_confirmation_bubble(
        normalize_label_payload({**incomplete_soy_label(), "product_name": "高蛋白豆漿"}),
        token="abc123", consumed_servings=1,
    )
    text = str(bubble)
    assert "修改營養品名:abc123" in text
    assert "修改營養數字:abc123" in text


def test_nutrition_input_state_is_persistent_owned_and_clearable():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U1", payload={**incomplete_soy_label(), "product_name": "高蛋白豆漿"}
    )
    set_nutrition_input_state(conn, user_id="U1", token=token, input_type="nutrient")
    assert get_nutrition_input_state(conn, user_id="U1") == {"token": token, "input_type": "nutrient"}
    assert get_nutrition_input_state(conn, user_id="U2") is None
    with pytest.raises(ValueError):
        set_nutrition_input_state(conn, user_id="U2", token=token, input_type="name")
    clear_nutrition_input_state(conn, user_id="U1")
    assert get_nutrition_input_state(conn, user_id="U1") is None


def test_expired_nutrition_input_state_is_not_returned():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U1", payload={**incomplete_soy_label(), "product_name": "高蛋白豆漿"}
    )
    set_nutrition_input_state(conn, user_id="U1", token=token, input_type="name")
    conn.execute("UPDATE nutrition_input_states SET expires_at='2000-01-01T00:00:00+08:00'")
    conn.commit()
    assert get_nutrition_input_state(conn, user_id="U1") is None


def test_inconsistent_per_serving_and_per100_blocks_confirmation_until_corrected():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    bad = incomplete_soy_label()
    bad["product_name"] = "高蛋白豆漿"
    bad["per_serving"]["sodium_mg"] = 58
    normalized = normalize_label_payload(bad)
    assert nutrition_consistency_warnings(normalized) == ["sodium_mg"]
    token = save_pending_label(conn, user_id="U1", payload=bad)
    bubble_text = str(build_label_confirmation_bubble(normalized, token=token))
    assert "鈉換算不一致" in bubble_text
    with pytest.raises(ValueError, match="換算不一致"):
        confirm_pending_label(conn, token=token, user_id="U1")
    corrected = update_pending_label_nutrient(
        conn, user_id="U1", token=token, field="sodium_mg", value=48
    )
    assert nutrition_consistency_warnings(corrected) == []
    result = confirm_pending_label(conn, token=token, user_id="U1")
    assert result["log"]["nutrition"]["sodium_mg"] == 48


def test_gross_calorie_macro_mismatch_is_editable_but_not_confirmable():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    bad = {**incomplete_soy_label(), "product_name": "高蛋白豆漿"}
    bad["per_serving"] = {**bad["per_serving"], "calories_kcal": 900}
    bad["per_100"] = {**bad["per_100"], "calories_kcal": 225}
    normalized = normalize_label_payload(bad)
    assert "calories_kcal" in nutrition_consistency_warnings(normalized)
    token = save_pending_label(conn, user_id="U1", payload=bad)
    with pytest.raises(ValueError, match="熱量"):
        confirm_pending_label(conn, token=token, user_id="U1")
    corrected = update_pending_label_nutrient(
        conn, user_id="U1", token=token, field="calories_kcal", value=228
    )
    assert nutrition_consistency_warnings(corrected) == []


def test_only_one_awaiting_identity_draft_per_user():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    first = save_pending_label(
        conn, user_id="U1", payload=incomplete_soy_label(),
        source_message_id="BACK_A", allow_missing_identity=True,
    )
    with pytest.raises(ValueError, match="等待商品正面"):
        save_pending_label(
            conn, user_id="U1", payload=incomplete_soy_label(),
            source_message_id="BACK_B", allow_missing_identity=True,
        )
    assert get_latest_awaiting_identity(conn, user_id="U1")["token"] == first


def test_product_front_message_replay_returns_original_token():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    first = save_pending_label(
        conn, user_id="U1", payload=incomplete_soy_label(),
        source_message_id="BACK_A", allow_missing_identity=True,
    )
    identity = {
        "status": "success", "image_type": "product_front", "product_name": "高蛋白豆漿",
        "brand": "測試", "barcode": "4710126206298", "confidence": 0.99,
    }
    paired = attach_latest_pending_identity(
        conn, user_id="U1", identity=identity, message_id="FRONT_A"
    )
    assert paired["token"] == first
    second = save_pending_label(
        conn, user_id="U1", payload=incomplete_soy_label(),
        source_message_id="BACK_B", allow_missing_identity=True,
    )
    replay = attach_latest_pending_identity(
        conn, user_id="U1", identity=identity, message_id="FRONT_A"
    )
    assert replay["token"] == first
    assert replay["replayed"] is True
    assert get_latest_awaiting_identity(conn, user_id="U1")["token"] == second


def test_expired_pending_cannot_change_servings_or_time():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U1", payload={**incomplete_soy_label(), "product_name": "高蛋白豆漿"}
    )
    conn.execute(
        "UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
        (token,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="逾時"):
        update_pending_consumption(conn, user_id="U1", token=token, consumed_servings=2)
    assert conn.execute(
        "SELECT status,consumed_servings FROM pending_nutrition_logs WHERE token=?", (token,)
    ).fetchone() == ("expired", 1.0)


def test_cancel_cannot_overwrite_confirmed_log():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U1", payload={**incomplete_soy_label(), "product_name": "高蛋白豆漿"},
        source_image_ref="nutrition-image:test.jpg",
    )
    confirm_pending_label(conn, token=token, user_id="U1")
    cancelled = cancel_pending_label(conn, user_id="U1", token=token)
    assert cancelled["cancelled"] is False
    assert conn.execute(
        "SELECT status,source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)
    ).fetchone() == ("confirmed", "nutrition-image:test.jpg")


def test_confirm_and_cancel_concurrent_connections_have_one_consistent_winner(tmp_path):
    db = tmp_path / "confirm-cancel-race.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn,
            user_id="U_RACE",
            payload={**incomplete_soy_label(), "product_name": "高蛋白豆漿"},
        )
    barrier = threading.Barrier(2)

    def do_confirm():
        with sqlite3.connect(db, timeout=10) as conn:
            ensure_nutrition_schema(conn)
            barrier.wait()
            try:
                confirm_pending_label(conn, token=token, user_id="U_RACE")
                return True
            except ValueError:
                return False

    def do_cancel():
        with sqlite3.connect(db, timeout=10) as conn:
            ensure_nutrition_schema(conn)
            barrier.wait()
            return cancel_pending_label(conn, user_id="U_RACE", token=token)["cancelled"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirm_future = pool.submit(do_confirm)
        cancel_future = pool.submit(do_cancel)
        confirm_won = confirm_future.result(timeout=15)
        cancel_won = cancel_future.result(timeout=15)
    assert confirm_won != cancel_won
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            "SELECT status FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0]
        log_count = conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0]
    assert (status, log_count) in {("confirmed", 1), ("cancelled", 0)}


def test_nutrition_text_edit_message_is_replayable():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    token = save_pending_label(
        conn, user_id="U1", payload={**incomplete_soy_label(), "product_name": "高蛋白豆漿"}
    )
    set_nutrition_input_state(conn, user_id="U1", token=token, input_type="nutrient")
    first = apply_nutrition_text_edit(
        conn, user_id="U1", message_id="TEXT_EDIT_1", field="sodium_mg", value=48
    )
    replay = apply_nutrition_text_edit(
        conn, user_id="U1", message_id="TEXT_EDIT_1", field="sodium_mg", value=999
    )
    assert first["token"] == replay["token"] == token
    assert replay["replayed"] is True
    assert replay["label"]["per_serving"]["sodium_mg"] == 48


def test_estimate_nutrition_from_approved_exchanges_uses_versioned_macro_rules():
    estimate = estimate_nutrition_from_exchanges({
        "milk_exchange": 0,
        "protein_low_exchange": 3,
        "protein_medium_exchange": 0,
        "protein_high_exchange": 0,
        "starch_exchange": 1.5,
        "vegetable_exchange": 0.5,
        "fruit_exchange": 0,
        "fat_exchange": 0,
    })
    assert estimate["calories_kcal"] == 279.0
    assert estimate["protein_g"] == 24.5
    assert estimate["fat_g"] == 9.0
    assert estimate["carbohydrate_g"] == 25.0
    assert estimate["_estimate_type"] == "approved_exchange_estimate"
    assert estimate["_rule_version"] == "tw-exchange-macros-v1"


def test_insert_approved_meal_photo_log_uses_verified_approval_chain_and_estimated_nutrients():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    exact = {
        "milk_exchange": 0.0,
        "protein_low_exchange": 0.0,
        "protein_medium_exchange": 2.5,
        "protein_high_exchange": 0.0,
        "starch_exchange": 6.0,
        "vegetable_exchange": 1.0,
        "fruit_exchange": 0.0,
        "fat_exchange": 0.0,
    }
    conn.execute("BEGIN IMMEDIATE")
    result = insert_approved_meal_photo_log(
        conn,
        token="abcdef123456",
        user_id="U_MEAL",
        reviewer="U_MEAL",
        consumed_at="2026-07-23T12:10:00+08:00",
        meal_slot="午餐",
        source_image_ref="nutrition-image:meal.jpg",
        observed_payload={
            "visible_items": [
                {"name": "雞肉", "category": "protein", "confidence": 0.9},
                {"name": "白飯", "category": "starch", "confidence": 0.9},
            ]
        },
        answers={"scope": "visible_only"},
        exact_exchange=exact,
    )
    conn.commit()

    totals = daily_consumed_totals(conn, user_id="U_MEAL", date_iso="2026-07-23")
    assert totals["protein_medium_exchange"] == 2.5
    assert totals["starch_exchange"] == 6.0
    assert totals["vegetable_exchange"] == 1.0
    assert totals["calories_kcal"] == 614.5
    assert totals["protein_g"] == 30.5
    row = conn.execute(
        """SELECT f.source_type,l.nutrition_snapshot_json,l.legacy_applied_at,
                  l.exchange_approval_id,a.approved_exchange_hash
           FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
           JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
           WHERE l.log_id=?""",
        (result["log_id"],),
    ).fetchone()
    assert row[0] == "user_meal_photo"
    nutrition = __import__("json").loads(row[1])
    assert nutrition["calories_kcal"] == 614.5
    assert nutrition["protein_g"] == 30.5
    assert nutrition["_estimate_type"] == "approved_exchange_estimate"
    assert row[2] == "not_applicable"
    assert row[3] == result["approval_id"]

    conn.execute(
        "UPDATE food_exchange_approvals SET approved_exchange_hash='tampered' WHERE approval_id=?",
        (result["approval_id"],),
    )
    tampered = daily_consumed_totals(conn, user_id="U_MEAL", date_iso="2026-07-23")
    assert tampered["protein_medium_exchange"] == 0.0
    assert tampered["starch_exchange"] == 0.0
    assert tampered["calories_kcal"] == 0.0
    assert tampered["protein_g"] == 0.0


def test_search_food_catalog_returns_user_foods_by_name_prefix():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    now = utcish_now()
    for name, uid in [("舒肥雞胸", "U1"), ("舒肥雞腿", "U1"), ("鮭魚生魚片", "U1"), ("舒肥雞胸", "U2")]:
        fid = new_id("food")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES (?,'"""+name+"""','','','user_private_food',?,'private',100,'g',1,'{}','{}','{}',
                       'approved',?,'',0,'user_confirmed',?,?)""",
            (fid, uid, fid, now, now),
        )
    results = search_food_catalog(conn, user_id="U1", query="舒肥")
    assert len(results) == 2
    assert all("舒肥" in r["product_name"] for r in results)
    assert all(r["owner_user_id"] == "U1" for r in results)

    results2 = search_food_catalog(conn, user_id="U1", query="鮭魚")
    assert len(results2) == 1
    assert results2[0]["product_name"] == "鮭魚生魚片"

    results3 = search_food_catalog(conn, user_id="U1", query="不存在")
    assert results3 == []


def test_search_food_history_returns_recent_user_logs():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    now = utcish_now()
    food_id = new_id("food")
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
            exchange_json,exchange_review_status,fingerprint,original_image_ref,
            recognition_confidence,verification_status,created_at,updated_at)
           VALUES (?,'舒肥雞胸','好市多','','user_private_food','U1','private',
                   100,'g',1,'{"calories_kcal":120}','{}','{}','approved',
                   'fp_test','',0,'user_confirmed',?,?)""",
        (food_id, now, now),
    )
    for i in range(3):
        conn.execute(
            """INSERT INTO food_logs
               (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,
                consumed_amount,consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,
                approved_exchange_json,exchange_approval_id,source_image_ref,plan_id,
                plan_link_status,confirmation_status,legacy_applied_at,created_at,updated_at)
               VALUES (?,?,?,'2026-07-20T12:00:00+08:00','午餐',1,100,'g','{}','{}','{}',
                       '','','','pending','confirmed','not_applicable',?,?)""",
            (new_id("log"), "U1", food_id, now, now),
        )
    log_id = new_id("log")
    conn.execute(
        """INSERT INTO food_logs
           (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,
            consumed_amount,consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,
            approved_exchange_json,exchange_approval_id,source_image_ref,plan_id,
            plan_link_status,confirmation_status,legacy_applied_at,created_at,updated_at)
           VALUES (?,?,?,'2026-07-23T12:00:00+08:00','午餐',1.5,150,'g',
                   '{"calories_kcal":180}','{}','{}','','','','pending',
                   'confirmed','not_applicable',?,?)""",
        (log_id, "U1", food_id, now, now),
    )
    results = search_food_history(conn, user_id="U1", query="舒肥")
    assert len(results) >= 1
    assert results[0]["product_name"] == "舒肥雞胸"
    assert results[0]["last_consumed_at"] is not None
    assert results[0]["use_count"] == 4


def test_search_food_catalog_includes_meal_photo_synthetic():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    now = utcish_now()
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
            exchange_json,exchange_review_status,fingerprint,original_image_ref,
            recognition_confidence,verification_status,created_at,updated_at)
           VALUES (?,'餐點照片：雞肉、白飯','','','user_meal_photo','U1','private',
                   1,'meal',1,'{}','{}',
                   '{"starch_exchange":6,"protein_medium_exchange":2.5}',
                   'approved','fp_photo','',0,'admin_approved',?,?)""",
        (new_id("food"), now, now),
    )
    results = search_food_catalog(conn, user_id="U1", query="餐點照片")
    assert len(results) == 1
    assert results[0]["source_type"] == "user_meal_photo"
    assert "雞肉" in results[0]["product_name"]


def test_quick_log_allows_public_system_menu_item():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    now = utcish_now()
    food_id = "menu_lowcarb01"
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
            exchange_json,exchange_review_status,fingerprint,original_image_ref,
            recognition_confidence,verification_status,created_at,updated_at)
           VALUES (?,'低碳嫩雞餐','','','label','system','public',1,'份',1,
                   '{"calories_kcal":350,"protein_g":35}','{}','{}','approved',
                   'fp_public_lowcarb','',1,'auto',?,?)""",
        (food_id, now, now),
    )
    conn.commit()

    result = quick_log_from_catalog(
        conn,
        user_id="U_CUSTOMER",
        food_id=food_id,
        consumed_servings=1.5,
        meal_slot="午餐",
        consumed_at="2026-08-04T12:00:00+08:00",
    )

    assert result["product_name"] == "低碳嫩雞餐"
    assert result["nutrition"]["calories_kcal"] == 525
    row = conn.execute(
        "SELECT user_id,food_id,consumed_servings,meal_slot FROM food_logs"
    ).fetchone()
    assert row == ("U_CUSTOMER", food_id, 1.5, "午餐")
