import json
import sqlite3
from types import SimpleNamespace

import pytest

import server
from tests.test_nanjing_meal_logging_flow import _setup


def _provider_payload(*, basis_amount=500, basis_unit="ml", calories=165, fat=7, carbs=9):
    def nutrient(value, spread, unit):
        return {"estimate": value, "min": max(0, value-spread), "max": value+spread, "unit": unit}
    return {
        "food_name": "無糖豆漿",
        "portion_assumption": "完整 500 ml",
        "basis_amount": basis_amount,
        "basis_unit": basis_unit,
        "calories_kcal": nutrient(calories, 10, "kcal"),
        "protein_g": nutrient(16, 2, "g"),
        "fat_g": nutrient(fat, 1, "g"),
        "carbohydrate_g": nutrient(carbs, 2, "g"),
    }


def _fake_provider(monkeypatch, payload, *, model="observed-model"):
    calls = []
    response = SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), refusal=None),
        )],
    )
    monkeypatch.setattr(server, "client", SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response)
    )))
    return calls


def _normalized(payload):
    value = server._text_meal_provider_payload(payload)
    value["provenance"] = {
        "provider": "offline", "model": "fake", "method": "text_meal_estimate",
    }
    return server._normalize_text_meal_estimate(value)


def test_provider_requires_matching_total_basis_four_nutrients_and_audits_raw_before_normalize(tmp_path, monkeypatch):
    audit_calls = []
    monkeypatch.setattr(server, "NUTRITION_ESTIMATE_AUDIT_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(server, "record_nutrition_estimate_audit", lambda path, **kw: audit_calls.append((path, kw)) or "trace-1")
    calls = _fake_provider(monkeypatch, _provider_payload())

    estimate = server.estimate_text_meal_nutrition(
        {"food_name": "無糖豆漿", "amount": 500, "unit": "ml", "meal_slot": "點心"},
        operation_id="op-1",
    )

    assert set(calls[0]["response_format"]["json_schema"]["schema"]["required"]) == {
        "food_name", "portion_assumption", "basis_amount", "basis_unit",
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g",
    }
    assert estimate["basis_amount"] == 500
    assert estimate["basis_unit"] == "ml"
    assert estimate["fat_g"]["estimate"] == 7
    assert estimate["carbohydrate_g"]["estimate"] == 9
    assert estimate["provenance"]["model"] == "observed-model"
    assert estimate["provenance"]["trace_id"] == "trace-1"
    assert audit_calls[0][1]["model"] == "observed-model"
    assert audit_calls[0][1]["response"] == json.dumps(_provider_payload(), ensure_ascii=False)


@pytest.mark.parametrize("basis_amount,basis_unit", [(100, "ml"), (500, "g")])
def test_provider_rejects_wrong_total_basis_but_still_audits(tmp_path, monkeypatch, basis_amount, basis_unit):
    audited = []
    monkeypatch.setattr(server, "NUTRITION_ESTIMATE_AUDIT_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(server, "record_nutrition_estimate_audit", lambda *_a, **kw: audited.append(kw) or "trace-bad")
    _fake_provider(monkeypatch, _provider_payload(basis_amount=basis_amount, basis_unit=basis_unit))

    with pytest.raises(server.TextMealProviderError, match="完整份量"):
        server.estimate_text_meal_nutrition(
            {"food_name": "無糖豆漿", "amount": 500, "unit": "ml", "meal_slot": "點心"}
        )
    assert len(audited) == 1


def test_ai_four_nutrients_reach_draft_and_ledger_while_inconsistent_cannot_confirm(tmp_path, monkeypatch):
    db, _replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda request, **_kw: _normalized(_provider_payload()))
    draft = server.create_text_meal_estimate_draft(
        user_id="U-NANJING", message_id="FOUR-1",
        request={"food_name": "無糖豆漿", "amount": 500, "unit": "ml", "meal_slot": "點心"},
    )
    assert draft["estimate"]["fat_g"]["estimate"] == 7
    result = server.apply_text_meal_estimate_action(
        user_id="U-NANJING", token=draft["token"], expected_version=1, action="confirm"
    )
    with sqlite3.connect(db) as conn:
        nutrition, metadata = conn.execute(
            "SELECT nutrition_snapshot_json,original_nutrition_snapshot_json FROM food_logs WHERE log_id=?",
            (result["log_id"],),
        ).fetchone()
    assert json.loads(nutrition) == {
        "calories_kcal": 165.0, "protein_g": 16.0, "fat_g": 7.0, "carbohydrate_g": 9.0,
    }
    assert json.loads(metadata)["estimate_metadata"]["basis"] == {"amount": 500.0, "unit": "ml"}

    bad = _provider_payload(calories=500, fat=1, carbs=1)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda request, **_kw: _normalized(bad))
    blocked = server.create_text_meal_estimate_draft(
        user_id="U-NANJING", message_id="FOUR-BAD",
        request={"food_name": "無糖豆漿", "amount": 500, "unit": "ml", "meal_slot": "點心"},
    )
    assert blocked["estimate"]["assessment"]["status"] == "inconsistent"
    with pytest.raises(ValueError, match="不一致"):
        server.apply_text_meal_estimate_action(
            user_id="U-NANJING", token=blocked["token"], expected_version=1, action="confirm"
        )


def test_reference_grams_is_zero_ai_draft_but_500ml_soy_uses_ai(tmp_path, monkeypatch):
    _db, _replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "create_text_meal_estimate_draft", lambda **kw: calls.append(kw) or {"status": "pending", "token": "ai", "version": 1, "estimate": _provider_payload(), "portion_multiplier": 1})
    monkeypatch.setattr(server, "build_text_meal_estimate_flex", lambda draft: draft)

    reference = server.build_natural_food_log_reply(
        user_id="U-NANJING", message_id="REF-1", event=SimpleNamespace(webhook_event_id="REF-E"),
        request={"food_name": "白飯", "amount": 200.0, "unit": "g", "meal_slot": "午餐"},
    )
    assert reference["estimate"]["provenance"]["method"] == "official_reference"
    assert calls == []

    soy = server.build_natural_food_log_reply(
        user_id="U-NANJING", message_id="SOY-ML", event=SimpleNamespace(webhook_event_id="SOY-E"),
        request={"food_name": "無糖豆漿", "amount": 500.0, "unit": "ml", "meal_slot": "宵夜"},
    )
    assert soy["token"] == "ai"
    assert calls[0]["request"]["meal_slot"] == "點心"


def test_exact_private_food_is_zero_ai_confirmation_draft(tmp_path, monkeypatch):
    db, _replies = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        created = server.create_daily_food_log(
            conn, user_id="U-NANJING", product_name="自製豆漿", meal_slot="早餐",
            consumed_at=server.tw_now().isoformat(), servings=1,
            nutrition={"calories_kcal": 120, "protein_g": 12, "fat_g": 4, "carbohydrate_g": 9},
            source_type="user_private_food", operation_key="seed-private",
        )
        conn.execute(
            "UPDATE food_catalog SET package_amount=500,package_unit='ml',servings_per_package=1 WHERE food_id=?",
            (created["food_id"],),
        )
        conn.execute("DELETE FROM food_logs")
        conn.commit()
    provider = []
    monkeypatch.setattr(server, "create_text_meal_estimate_draft", lambda **kw: provider.append(kw))
    monkeypatch.setattr(server, "build_text_meal_estimate_flex", lambda draft: draft)

    draft = server.build_natural_food_log_reply(
        user_id="U-NANJING", message_id="PRIVATE-1",
        event=SimpleNamespace(webhook_event_id="PRIVATE-E"),
        request={"food_name": "自製豆漿", "amount": 500.0, "unit": "ml", "meal_slot": "早餐"},
    )

    assert draft["estimate"]["provenance"]["method"] == "owner_private_catalog"
    assert provider == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_night_alias_maps_to_snack_for_natural_and_legacy_entries():
    parsed = server.parse_natural_food_log_intent("宵夜吃了無糖豆漿500ml")
    assert parsed["meal_slot"] == "點心"
    assert server.ai_estimate_meal_slot("請用一般估算記錄 宵夜 無糖豆漿") == "點心"


def test_general_food_chat_does_not_inject_entire_menu(tmp_path, monkeypatch):
    _db, _replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "MAIN_DISHES", [{"name": "燕麥豆漿", "cal": 287, "pro": 11.3, "ingredients": "燕麥"}])
    calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="一般回答"))])
    monkeypatch.setattr(server, "client", SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: calls.append(kw) or response)
    )))

    server.get_ai_response_with_memory("U-NANJING", "無糖豆漿怎麼選？", "CHAT-1")

    prompt = calls[0]["messages"][0]["content"]
    assert "燕麥豆漿" not in prompt
    assert "287" not in prompt
    assert "11.3" not in prompt
