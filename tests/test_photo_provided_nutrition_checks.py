import json
import sqlite3

import pytest

import meal_photo_system
from meal_photo_system import (
    apply_meal_photo_action,
    apply_meal_photo_ai_adjustment,
    get_meal_photo_draft,
    normalize_meal_photo_payload,
    save_meal_photo_draft,
)


def _photo_payload(*, calories=680, protein=35):
    return {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": [
            {"name": "雞腿便當", "category": "protein", "confidence": 0.91},
            {"name": "白飯", "category": "starch", "confidence": 0.96},
        ],
        "uncertain_items": [],
        "starch_visibility": "visible",
        "oil_sauce_status": "unknown",
        "observed_at": "2026-10-08T12:00:00+08:00",
        "observed_at_confidence": 0.9,
        "ai_estimate": {
            "items": [
                {"name": "雞腿", "portion": "約1支", "calories_kcal": 330, "protein_g": 27},
                {"name": "白飯", "portion": "約1碗", "calories_kcal": 280, "protein_g": 5},
            ],
            "calories_kcal": {
                "estimate": calories,
                "min": max(0, calories - 50),
                "max": calories + 50,
            },
            "protein_g": {
                "estimate": protein,
                "min": max(0, protein - 5),
                "max": protein + 5,
            },
            "confidence": 0.78,
            "provenance": {
                "provider": "offline-test",
                "model": "fake-vision",
                "method": "vision_model_estimate",
                "nutrition_basis": "unlabeled_meal_photo",
            },
        },
    }


def _save(conn, payload, source_message_id="PHOTO-1"):
    return save_meal_photo_draft(
        conn,
        user_id="U1",
        source_message_id=source_message_id,
        payload=payload,
        meal_slot="午餐",
        consumed_at="2026-10-08T12:00:00+08:00",
        workflow_version="user_confirmed_ai_nutrition_v2",
    )


def test_legacy_kcal_protein_photo_remains_valid_with_unknown_fat_and_carbs(tmp_path):
    payload = _photo_payload()
    normalized = normalize_meal_photo_payload(payload)

    assert normalized["ai_estimate"]["provenance"] == payload["ai_estimate"]["provenance"]
    assert set(normalized["ai_estimate"]["provenance"]) == {
        "provider", "model", "method", "nutrition_basis",
    }

    with sqlite3.connect(tmp_path / "valid-photo.db") as conn:
        token = _save(conn, payload)
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["estimate"]["fat_g"] is None
        assert draft["estimate"]["carbohydrate_g"] is None

        result = apply_meal_photo_action(
            conn,
            event_id="CONFIRM-VALID",
            user_id="U1",
            token=token,
            expected_version=draft["version"],
            action="confirm_estimate",
        )
        nutrition = json.loads(conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id=?",
            (result["result"]["log_id"],),
        ).fetchone()[0])

    assert nutrition == {"calories_kcal": 680.0, "protein_g": 35.0}


def test_initial_photo_rejects_protein_energy_far_above_provided_calories(tmp_path):
    with sqlite3.connect(tmp_path / "invalid-initial.db") as conn:
        with pytest.raises(ValueError, match="營養估算.*核對"):
            _save(conn, _photo_payload(calories=287, protein=113))
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_meal_photo_drafts"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("calories_kcal", -1),
        ("calories_kcal", float("nan")),
        ("protein_g", True),
        ("protein_g", float("inf")),
    ],
)
def test_initial_photo_still_rejects_existing_illegal_provided_values(field, value):
    payload = _photo_payload()
    payload["ai_estimate"][field]["estimate"] = value

    with pytest.raises(ValueError):
        normalize_meal_photo_payload(payload)


def test_confirmation_rechecks_tampered_provided_nutrition_before_canonical_write(tmp_path):
    with sqlite3.connect(tmp_path / "invalid-confirm.db") as conn:
        token = _save(conn, _photo_payload())
        impossible = _photo_payload(calories=287, protein=113)
        impossible_snapshot = meal_photo_system._ai_estimate_snapshot(
            _photo_payload()["ai_estimate"]
        )
        impossible_snapshot.update({
            "calories_kcal": 287.0,
            "protein_g": 113.0,
            "calories_kcal_range": {
                "min": 237.0, "max": 337.0,
                "basis": "ai_vision_estimate_range_v1",
            },
            "protein_g_range": {
                "min": 108.0, "max": 118.0,
                "basis": "ai_vision_estimate_range_v1",
            },
        })
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET observed_payload_json=?, estimate_json=? WHERE token=?",
            (
                json.dumps(impossible, ensure_ascii=False),
                json.dumps(impossible_snapshot, ensure_ascii=False),
                token,
            ),
        )
        conn.commit()

        with pytest.raises(ValueError, match="營養估算.*核對"):
            apply_meal_photo_action(
                conn,
                event_id="CONFIRM-TAMPERED",
                user_id="U1",
                token=token,
                expected_version=1,
                action="confirm_estimate",
            )

        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0] == "estimated"


def test_photo_adjustment_rejects_implausible_provided_nutrition_and_restores_draft(tmp_path):
    with sqlite3.connect(tmp_path / "invalid-adjustment.db") as conn:
        token = _save(conn, _photo_payload())
        waiting = apply_meal_photo_action(
            conn,
            event_id="REQUEST-ADJUST",
            user_id="U1",
            token=token,
            expected_version=1,
            action="request_adjust",
        )

        with pytest.raises(ValueError, match="營養估算.*核對"):
            apply_meal_photo_ai_adjustment(
                conn,
                event_id="INVALID-ADJUSTMENT",
                user_id="U1",
                token=token,
                expected_version=waiting["draft"]["version"],
                correction="請修正份量",
                estimate_provider=lambda *_args: _photo_payload(calories=287, protein=113),
            )

        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["status"] == "awaiting_adjustment"
        assert draft["version"] == waiting["draft"]["version"]
        assert draft["estimate"]["calories_kcal"] == 680
        assert draft["estimate"]["protein_g"] == 35
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
