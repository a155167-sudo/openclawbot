import copy
import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "nutrition_reference_data.json"
EVIDENCE_PATH = ROOT / "evidence" / "round3" / "TFDA-CATALOG.json"


def load_reference():
    path = ROOT / "nutrition_reference.py"
    spec = importlib.util.spec_from_file_location("nutrition_reference_scope_round3", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.__file__ is not None
    assert Path(module.__file__).resolve() == path
    return module


def load_data():
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def by_code(document, code):
    return next(item for item in document["items"] if item["food_code"] == code)


def canonical_sha256(value):
    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_raw_skinless_meat_aliases_do_not_capture_unspecified_skin_or_cooked_food():
    module = load_reference()
    document = load_data()

    assert by_code(document, "I04024")["aliases"] == ["生去皮雞胸肉"]
    assert by_code(document, "J04010")["aliases"] == ["生去皮虱目魚"]

    for unsafe_alias in ["生雞胸肉", "雞胸肉", "熟雞胸肉", "虱目魚", "熟虱目魚"]:
        assert module.resolve_reference(
            {"food_name": unsafe_alias, "amount": 100, "unit": "g"}
        ) is None

    assert module.resolve_reference(
        {"food_name": "生去皮雞胸肉", "amount": 100, "unit": "g"}
    )["source"]["source_row_id"] == "I04024"
    assert module.resolve_reference(
        {"food_name": "生去皮虱目魚", "amount": 100, "unit": "g"}
    )["source"]["source_row_id"] == "J04010"


def test_plain_raw_vegetable_names_do_not_silently_mean_raw():
    module = load_reference()
    document = load_data()
    expected_aliases = {
        "E5000101": ["生菠菜"],
        "E3100101": ["生甘藷葉", "生地瓜葉"],
        "E5800101": ["生花椰菜", "生白花椰菜"],
    }

    for code, aliases in expected_aliases.items():
        assert by_code(document, code)["aliases"] == aliases
        for alias in aliases:
            result = module.resolve_reference(
                {"food_name": alias, "amount": 100, "unit": "g"}
            )
            assert result["source"]["source_row_id"] == code

    for unspecified in ["菠菜", "甘藷葉", "地瓜葉", "花椰菜", "白花椰菜"]:
        assert module.resolve_reference(
            {"food_name": unspecified, "amount": 100, "unit": "g"}
        ) is None


def test_generic_tofu_discloses_latest_2022_dataset_policy_not_exact_truth():
    module = load_reference()
    item = by_code(load_data(), "R4700902")
    assert item["aliases"] == ["傳統豆腐", "板豆腐"]
    assert item["reference_scope"] == "latest_dataset_representative_benchmark"
    assert "最新" in item["version_assumption"]
    assert "2022" in item["version_assumption"]
    assert "非唯一" in item["version_assumption"]
    assert "非實測" in item["reference_label"]

    source = module.find_reference_nutrition(
        {"food_name": "傳統豆腐", "amount": 100, "unit": "g"}
    )["source"]
    assert source["source_name"] == "傳統豆腐(2022年取樣)"
    assert source["source_row_id"] == "R4700902"
    assert source["dataset_version"] == "2026-10-05 08:34:48"
    assert source["reference_scope"] == "latest_dataset_representative_benchmark"
    assert source["version_assumption"] == item["version_assumption"]


def test_generic_fruit_lookup_is_scoped_to_general_edible_portion_benchmark():
    module = load_reference()
    document = load_data()
    generic_rows = {
        "D08001": "香蕉",
        "D15002": "芭樂",
        "D36001": "柳橙",
        "D32004": "蘋果",
        "D11002": "鳳梨",
        "D02001": "木瓜",
        "D19002": "西瓜",
        "D0500201": "奇異果",
        "D21002": "芒果",
    }

    for code, alias in generic_rows.items():
        item = by_code(document, code)
        assert item["reference_scope"] == "general_edible_portion_benchmark"
        assert item["preparation_assumption"]
        assert item["variety_assumption"]
        assert item["version_assumption"]
        assert "非實測" in item["reference_label"]

        result = module.find_reference_nutrition(
            {"food_name": alias, "amount": 100, "unit": "g"}
        )
        assert result["status"] == "matched"
        source = result["source"]
        for field in (
            "source_name",
            "source_row_id",
            "dataset_version",
            "reference_scope",
            "preparation_assumption",
            "variety_assumption",
            "version_assumption",
        ):
            assert source[field] == item[field if field != "source_name" else "name"]
        assert "非實測" in source["reference_label"]


def test_duplicate_alias_fails_closed_as_ambiguous_without_arbitrary_source(tmp_path):
    module = load_reference()
    document = load_data()
    first = copy.deepcopy(document["items"][0])
    second = copy.deepcopy(document["items"][1])
    first["aliases"] = ["碰撞食品"]
    second["aliases"] = ["碰撞食品"]
    document["items"] = [first, second]
    collision_path = tmp_path / "collision.json"
    collision_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    setattr(module, "DATA_PATH", collision_path)

    capability = module.reference_capability("碰撞食品", "g")
    assert capability == {
        "status": "ambiguous_reference",
        "food_exists": True,
        "available_units": ["g"],
        "source_basis": None,
        "source": None,
    }
    assert module.find_reference_nutrition(
        {"food_name": "碰撞食品", "amount": 100, "unit": "g"}
    ) == {
        "status": "ambiguous_reference",
        "food_exists": True,
        "available_units": ["g"],
    }


def test_official_176_values_and_source_hashes_remain_frozen():
    document = load_data()
    # Freeze the original cohort, not catalog capacity: additions cannot change these hashes.
    original_codes = {
        'A0400301', 'A0550601', 'A0810101', 'B0700202', 'D02001', 'D0500201',
        'D08001', 'D11002', 'D15002', 'D19002', 'D21002', 'D32004', 'D36001',
        'E02001', 'E04001', 'E23001', 'E30001', 'E3100101', 'E5000101',
        'E5800101', 'E63001', 'E74001', 'G08001', 'G13002', 'H1150201',
        'I0108302', 'I01101', 'I0302301', 'I03104', 'I04024', 'J04010',
        'J04024', 'J0412101', 'J21009', 'J31002', 'J35001', 'K01001',
        'K0111101', 'K01121', 'L01021', 'R2000101', 'R2400201', 'R4700902', 'R4701201',
    }
    document = {"items": [item for item in document["items"] if item["food_code"] in original_codes]}
    assert len(document["items"]) == 44
    assert {item["food_code"] for item in document["items"]} == original_codes
    nutrition = {item["food_code"]: item["nutrition"] for item in document["items"]}
    source_hashes = {
        item["food_code"]: {
            key: item[key]
            for key in ("archive_sha256", "evidence_sha256", "selected_rows_sha256")
        }
        for item in document["items"]
    }
    assert sum(len(values) for values in nutrition.values()) == 176
    assert canonical_sha256(nutrition) == (
        "d4c6d33da0ef935b86844d17bb90b5bd5e2ac3166cd2266162ac22359bb2f3db"
    )
    assert canonical_sha256(source_hashes) == (
        "e5518e62200e9a14ae543ae041bfbf28d927fedd7530316ab0df8041cb918cd3"
    )


def test_evidence_catalog_discloses_scope_without_changing_selected_rows():
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    records = {record["food_code"]: record for record in evidence["records"]}
    assert evidence["alias_policy"]["unspecified_preparation"] == "no_raw_default"
    assert evidence["alias_policy"]["generic_foods"] == (
        "general_edible_portion_benchmark_not_individual_measurement"
    )

    for code in ("R4700902", "D08001", "D32004", "D21002"):
        record = records[code]
        assert record["reference_scope"] != "exact_identity"
        assert "非實測" in record["acceptance_note"]
        assert canonical_sha256(record["selected_source_rows"]) == record["selected_rows_sha256"]
