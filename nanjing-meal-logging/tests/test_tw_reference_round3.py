import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_reference():
    path = ROOT / "nutrition_reference.py"
    spec = importlib.util.spec_from_file_location("nutrition_reference_round3", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path
    return module


def load_catalog():
    return json.loads((ROOT / "nutrition_reference_data.json").read_text(encoding="utf-8"))


def test_catalog_has_at_least_30_tfda_rows_with_complete_traceability():
    document = load_catalog()
    assert len(document["items"]) >= 30
    assert document["dataset"]["archive_sha256"] == (
        "755d2f0e4a7c1fe80ac0636863117e376773c9342cfd68e7b05e67d81e3c6064"
    )
    for item in document["items"]:
        assert item["publisher"] == "TFDA"
        assert item["source_type"] == "government_food_composition"
        assert item["source_row_id"] == item["food_code"]
        assert item["dataset_version"] == "2026-10-05 08:34:48"
        assert item["source_url"].startswith("https://data.fda.gov.tw/")
        assert len(item["evidence_sha256"]) == 64
        assert item["basis_amount"] == 100
        assert item["basis_unit"] == "g"
        assert set(item["nutrition"]) == {
            "calories_kcal", "protein_g", "fat_g", "carbohydrate_g"
        }
        assert all(value is None or isinstance(value, (int, float))
                   for value in item["nutrition"].values())


def test_common_food_rows_use_documented_references_not_cross_state_aliases():
    module = load_reference()
    expected = {
        "白飯": "A0550601",
        "生去皮雞胸肉": "I04024",
        "香蕉": "D08001",
        "生雞蛋": "K01001",
        "水煮蛋": "K0111101",
        "無糖豆漿": "H1150201",
        "全脂鮮乳": "L01021",
        "傳統豆腐": "R4700902",
        "嫩豆腐": "R4701201",
        "生菠菜": "E5000101",
        "蘋果": "D32004",
        "生去皮虱目魚": "J04010",
    }
    for name, code in expected.items():
        result = module.resolve_reference(
            {"food_name": name, "amount": 100, "unit": "g"}
        )
        assert result is not None, name
        assert result["source"]["food_code"] == code

    for unsafe_alias in ["生雞胸肉", "虱目魚", "菠菜", "甘藷葉", "花椰菜", "熟雞胸肉", "熟菠菜", "熟豬絞肉", "烤鮭魚", "品牌無糖豆漿"]:
        assert module.resolve_reference(
            {"food_name": unsafe_alias, "amount": 100, "unit": "g"}
        ) is None


def test_find_reference_nutrition_keeps_per_100_contract_and_resolve_scales_totals():
    module = load_reference()
    request = {"food_name": "香蕉", "amount": 250, "unit": "g"}
    reference = module.find_reference_nutrition(request)
    assert reference["status"] == "matched"
    assert reference["basis_amount"] == 100
    assert reference["basis_unit"] == "g"
    assert reference["nutrition"]["calories_kcal"] == 85
    assert reference["source"]["food_code"] == "D08001"

    total = module.resolve_reference(request)
    assert total["nutrition"]["calories_kcal"] == pytest.approx(212.5)
    assert total["nutrition"]["protein_g"] == pytest.approx(
        reference["nutrition"]["protein_g"] * 2.5
    )


def test_reference_capability_distinguishes_exact_unit_mismatch_and_missing():
    module = load_reference()
    exact = module.reference_capability("白飯", "克")
    assert exact["status"] == "exact"
    assert exact["available_units"] == ["g"]
    assert exact["source_basis"] == {"amount": 100, "unit": "g"}
    assert exact["source"]["food_code"] == "A0550601"

    mismatch = module.reference_capability("白飯", "ml")
    assert mismatch["status"] == "unit_mismatch"
    assert mismatch["food_exists"] is True
    assert mismatch["available_units"] == ["g"]
    assert mismatch["source_basis"] == {"amount": 100, "unit": "g"}

    missing = module.reference_capability("雞肉便當", "g")
    assert missing == {
        "status": "missing", "food_exists": False, "available_units": [],
        "source_basis": None, "source": None,
    }


def test_cc_is_only_ml_spelling_and_never_creates_density():
    module = load_reference()
    for name in ["無糖豆漿", "全脂鮮乳"]:
        capability = module.reference_capability(name, "cc")
        assert capability["status"] == "unit_mismatch"
        assert capability["available_units"] == ["g"]
        assert module.resolve_reference(
            {"food_name": name, "amount": 250, "unit": "cc"}
        ) is None
        mismatch = module.find_reference_nutrition(
            {"food_name": name, "amount": 250, "unit": "cc"}
        )
        assert mismatch == {
            "status": "unit_mismatch", "food_exists": True,
            "available_units": ["g"]
        }


def test_aliases_are_unique_and_no_foreign_brand_reference_remains():
    document = load_catalog()
    normalized = {}
    for item in document["items"]:
        for alias in item["aliases"]:
            key = "".join(alias.split()).casefold()
            assert key not in normalized, (alias, normalized.get(key), item["food_code"])
            normalized[key] = item["food_code"]
    payload = json.dumps(document, ensure_ascii=False).casefold()
    for foreign_brand in ["silk", "oatly", "quaker"]:
        assert foreign_brand not in payload
