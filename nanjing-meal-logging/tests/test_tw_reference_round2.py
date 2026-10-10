import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_reference():
    path = ROOT / "nutrition_reference.py"
    spec = importlib.util.spec_from_file_location("nutrition_reference_tw_round2", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("amount", [100, 375, 500])
def test_tfda_unsweetened_soymilk_known_gram_reference_and_scaling(amount):
    result = load_reference().resolve_reference(
        {"food_name": "無糖豆漿", "amount": amount, "unit": "g"}
    )

    ratio = amount / 100
    assert result["nutrition"] == pytest.approx(
        {
            "calories_kcal": 35 * ratio,
            "protein_g": 3.6 * ratio,
            "fat_g": 1.9 * ratio,
            "carbohydrate_g": 0.7 * ratio,
        }
    )
    assert result["source"]["food_code"] == "H1150201"
    assert result["source"]["publisher"] == "TFDA"
    assert result["source"]["basis_amount"] == 100
    assert result["source"]["basis_unit"] == "g"
    assert result["source"]["evidence_sha256"] == (
        "c6b1974def2fa546d75e200e6726b42f453a6e1e4fff672526bfc89fe352c522"
    )


@pytest.mark.parametrize("unit", ["ml", "毫升", "cc"])
def test_tfda_grams_never_impersonate_a_volume_reference(unit):
    assert load_reference().resolve_reference(
        {"food_name": "無糖豆漿", "amount": 500, "unit": unit}
    ) is None


@pytest.mark.parametrize(
    "name",
    ["品牌無糖豆漿", "Silk無糖豆漿", "統一無糖豆漿", "濃無糖豆漿", "無糖豆漿飲品"],
)
def test_unknown_or_brand_specific_soymilk_is_rejected(name):
    module = load_reference()
    assert module.resolve_reference({"food_name": name, "amount": 100, "unit": "g"}) is None
    assert module.resolve_reference({"food_name": name, "amount": 100, "unit": "ml"}) is None


def test_data_contains_only_the_official_generic_soymilk_reference():
    document = json.loads((ROOT / "nutrition_reference_data.json").read_text(encoding="utf-8"))
    soy_items = [item for item in document["items"] if "無糖豆漿" in item["aliases"]]

    assert len(soy_items) == 1
    assert soy_items[0]["publisher"] == "TFDA"
    assert soy_items[0]["basis_unit"] == "g"
    assert "never infer g-to-ml" in document["source_policy"]
    assert "Silk" not in json.dumps(document, ensure_ascii=False)