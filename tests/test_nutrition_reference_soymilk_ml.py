import importlib.util
from pathlib import Path

import pytest


def load_reference():
    path = Path(__file__).resolve().parents[1] / "nutrition_reference.py"
    spec = importlib.util.spec_from_file_location("nutrition_reference_soymilk_ml", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.__file__ is not None
    assert Path(module.__file__).resolve() == path
    return module


@pytest.mark.parametrize("amount", [100, 250, 400, 500])
def test_unsweetened_soymilk_scales_official_ml_label(amount):
    # Historical test ID retained for baseline traceability. Round2 explicitly
    # retires the foreign-brand ml label: TFDA publishes mass, not density.
    module = load_reference()
    assert module.resolve_reference(
        {"food_name": "無糖豆漿", "amount": amount, "unit": "ml"}
    ) is None
    result = module.resolve_reference(
        {"food_name": "無糖豆漿", "amount": amount, "unit": "g"}
    )
    ratio = amount / 100
    assert result["nutrition"] == pytest.approx({
        "calories_kcal": 35 * ratio,
        "protein_g": 3.6 * ratio,
        "fat_g": 1.9 * ratio,
        "carbohydrate_g": 0.7 * ratio,
    })
    assert result["source"]["basis_amount"] == 100
    assert result["source"]["basis_unit"] == "g"
    assert result["source"]["publisher"] == "TFDA"
    assert result["source"]["source_type"] == "government_food_composition"
    assert "TFDA一般參考值" in result["portion_assumption"]


def test_unsweetened_soymilk_ml_accepts_only_declared_exact_aliases():
    module = load_reference()

    assert module.resolve_reference(
        {"food_name": "豆漿(無糖)", "amount": 100, "unit": "毫升"}
    ) is None  # Alias normalization must not invent mass-to-volume density.
    assert module.resolve_reference(
        {"food_name": "豆漿(無糖)", "amount": 100, "unit": "g"}
    ) is not None
    for name in ["品牌無糖豆漿", "Silk無糖豆漿", "濃無糖豆漿", "無糖豆漿飲品"]:
        assert module.resolve_reference(
            {"food_name": name, "amount": 100, "unit": "ml"}
        ) is None


def test_tfda_gram_reference_keeps_tfda_source_scope():
    result = load_reference().resolve_reference(
        {"food_name": "無糖豆漿", "amount": 100, "unit": "g"}
    )

    assert result["source"]["publisher"] == "TFDA"
    assert result["source"]["source_type"] == "government_food_composition"
    assert "TFDA一般參考值" in result["portion_assumption"]
