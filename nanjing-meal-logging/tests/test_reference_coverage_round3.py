import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unicodedata
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "nutrition_reference_data.json"
EVIDENCE_PATH = ROOT / "evidence" / "round3" / "TFDA-CATALOG.json"
ZIP_PATH = Path(os.environ.get(
    "ROUND3_TFDA_SOURCE_ZIP",
    str(ROOT.parent / "nanjing-meal-logging-evidence" / "round2" / "tfda-food-nutrition-20-json.zip"),
))
ARCHIVE_SHA256 = "755d2f0e4a7c1fe80ac0636863117e376773c9342cfd68e7b05e67d81e3c6064"
ADDED_CODES = (
    "B0400601", "J0414810", "H1100101", "A0800101", "R4600201",
    "C1705301", "C0810101", "I0306101", "E6400301", "L0314201",
)
LOOKUPS = {
    "生去皮黃肉甘藷": "B0400601",
    "蒸鯖魚": "J0414810",
    "生毛豆仁": "H1100101",
    "生燕麥": "A0800101",
    "乾冬粉": "R4600201",
    "生帶膜花生仁": "C1705301",
    "原味熟腰果": "C0810101",
    "生豬小里肌": "I0306101",
    "生水果小胡瓜": "E6400301",
    "無糖纖維優格": "L0314201",
}
NUTRIENTS = {
    "熱量": ("calories_kcal", "kcal"),
    "粗蛋白": ("protein_g", "g"),
    "粗脂肪": ("fat_g", "g"),
    "總碳水化合物": ("carbohydrate_g", "g"),
}


def canonical_sha256(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def norm(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def load_reference():
    path = ROOT / "nutrition_reference.py"
    spec = importlib.util.spec_from_file_location("reference_coverage_round3", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path
    return module


def load_data():
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def test_ten_independently_identified_tfda_rows_are_queryable_before_ai_fallback():
    module = load_reference()
    for alias, code in LOOKUPS.items():
        result = module.find_reference_nutrition(
            {"food_name": alias, "amount": 137, "unit": "g"}
        )
        assert result is not None, alias
        assert result["status"] == "matched"
        assert result["basis_amount"] == 100
        assert result["basis_unit"] == "g"
        assert result["source"]["source_row_id"] == code


def test_added_values_units_states_and_hashes_replay_from_frozen_official_zip():
    assert hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest() == ARCHIVE_SHA256
    with zipfile.ZipFile(ZIP_PATH) as archive:
        info = archive.getinfo("20_5.json")
        assert info.file_size == 129859413
        assert info.CRC == 0x8D7C2E1A
        official = json.loads(archive.read("20_5.json"))

    document = load_data()
    catalog = {item["food_code"]: item for item in document["items"]}
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    records = {record["food_code"]: record for record in evidence["records"]}

    for code in ADDED_CODES:
        source_rows = [
            row for row in official
            if row["整合編號"] == code and row["分析項"] in NUTRIENTS
        ]
        assert len(source_rows) == 4
        selected = [{key: row[key] for key in (
            "整合編號", "樣品名稱", "內容物描述", "分析項", "含量單位",
            "每100克含量", "每單位重",
        )} for row in source_rows]
        item = catalog[code]
        record = records[code]
        assert record["selected_source_rows"] == selected
        assert canonical_sha256(selected) == item["selected_rows_sha256"]
        assert item["selected_rows_sha256"] == record["selected_rows_sha256"]
        assert item["archive_sha256"] == ARCHIVE_SHA256
        assert item["dataset_version"] == document["dataset"]["dataset_version"]
        assert item["state"] == source_rows[0]["內容物描述"]
        for row in source_rows:
            field, unit = NUTRIENTS[row["分析項"]]
            assert row["含量單位"] == unit
            expected = None if row["每100克含量"] is None else float(row["每100克含量"])
            assert item["nutrition"][field] == expected


def test_original_44_rows_remain_frozen_and_expansion_has_separate_digest():
    document = load_data()
    assert len(document["items"]) == 54
    original = document["items"][:44]
    added = document["items"][44:]
    assert [item["food_code"] for item in added] == list(ADDED_CODES)
    assert canonical_sha256({item["food_code"]: item["nutrition"] for item in original}) == (
        "d4c6d33da0ef935b86844d17bb90b5bd5e2ac3166cd2266162ac22359bb2f3db"
    )
    assert canonical_sha256({item["food_code"]: {
        key: item[key] for key in ("archive_sha256", "evidence_sha256", "selected_rows_sha256")
    } for item in original}) == (
        "e5518e62200e9a14ae543ae041bfbf28d927fedd7530316ab0df8041cb918cd3"
    )
    assert canonical_sha256({item["food_code"]: item["nutrition"] for item in added}) == (
        "f549e58fb333e757b0d99d23b97a6b21b2814bd434d7b130ac6e146e4f435bfe"
    )


def test_scope_is_explicit_and_cross_state_or_edible_part_aliases_remain_missing():
    module = load_reference()
    document = load_data()
    by_code = {item["food_code"]: item for item in document["items"]}
    for code in ("B0400601", "E6400301"):
        item = by_code[code]
        assert item["reference_scope"] == "general_edible_portion_benchmark"
        assert item["preparation_assumption"]
        assert item["variety_assumption"]
        assert item["version_assumption"]
        assert "非實測" in item["reference_label"]

    for alias, code in {
        "燕麥乾": "A0800101",
        "帶膜花生": "C1705301",
        "熟腰果": "C0810101",
        "水果胡瓜": "E6400301",
    }.items():
        result = module.find_reference_nutrition(
            {"food_name": alias, "amount": 100, "unit": "g"}
        )
        assert result is not None, alias
        assert result["source"]["source_row_id"] == code

    for unsafe in (
        "熟黃肉甘藷", "帶皮黃肉甘藷", "生鯖魚", "烤鯖魚", "熟毛豆仁",
        "熟燕麥", "煮冬粉", "去膜花生仁", "生腰果", "熟豬小里肌",
        "熟水果小胡瓜", "無糖優格",
    ):
        assert module.resolve_reference(
            {"food_name": unsafe, "amount": 100, "unit": "g"}
        ) is None, unsafe


def test_added_aliases_are_unique_and_future_duplicates_fail_closed(tmp_path):
    module = load_reference()
    document = load_data()
    seen = {}
    for item in document["items"]:
        for alias in item["aliases"]:
            key = norm(alias)
            assert key not in seen, (alias, seen.get(key), item["food_code"])
            seen[key] = item["food_code"]

    collision = copy.deepcopy(document)
    collision["items"][0]["aliases"].append("生燕麥")
    path = tmp_path / "collision.json"
    path.write_text(json.dumps(collision, ensure_ascii=False), encoding="utf-8")
    module.DATA_PATH = path
    result = module.find_reference_nutrition(
        {"food_name": "生燕麥", "amount": 100, "unit": "g"}
    )
    assert result == {
        "status": "ambiguous_reference", "food_exists": True,
        "available_units": ["g"],
    }


def test_zero_is_not_null_and_synthetic_null_still_scales_as_null(tmp_path):
    module = load_reference()
    assert module.find_reference_nutrition(
        {"food_name": "生水果小胡瓜", "amount": 100, "unit": "g"}
    )["nutrition"]["fat_g"] == 0
    assert module.find_reference_nutrition(
        {"food_name": "生豬小里肌", "amount": 100, "unit": "g"}
    )["nutrition"]["carbohydrate_g"] == 0

    document = load_data()
    synthetic = copy.deepcopy(document["items"][-1])
    synthetic["food_code"] = synthetic["source_row_id"] = "SYNTHETIC-NULL"
    synthetic["aliases"] = ["合成缺值探針"]
    synthetic["nutrition"]["fat_g"] = None
    document["items"] = [synthetic]
    path = tmp_path / "null.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    module.DATA_PATH = path
    assert module.resolve_reference(
        {"food_name": "合成缺值探針", "amount": 250, "unit": "g"}
    )["nutrition"]["fat_g"] is None
