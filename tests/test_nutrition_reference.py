import importlib.util
from pathlib import Path

def load_reference():
    path = Path(__file__).resolve().parents[1] / 'nutrition_reference.py'
    assert path.exists(), 'verified reference lookup not implemented'
    spec = importlib.util.spec_from_file_location('nutrition_reference', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_verified_cooked_rice_scales_exact_requested_grams():
    result = load_reference().resolve_reference({'food_name': '白飯', 'amount': 200, 'unit': 'g'})
    assert result['nutrition']['calories_kcal'] == 183 * 2
    assert result['nutrition']['protein_g'] == 3.1 * 2
    assert result['source']['food_code'] == 'A0550601'
    assert result['source']['basis_unit'] == 'g'
    assert result['amount'] == 200


def test_reference_rejects_unverified_density_and_distinct_foods():
    module = load_reference()
    for name, unit in [('無糖豆漿', 'ml'), ('燕麥豆漿', 'g'), ('熟雞胸肉', 'g'),
                       ('雞胸肉', 'g'), ('白飯加雞肉', 'g'), ('品牌無糖豆漿', 'g'),
                       ('水煮蛋白', 'g')]:
        assert module.resolve_reference({'food_name': name, 'amount': 500, 'unit': unit}) is None


def test_reference_rejects_invalid_amount_and_keeps_known_zero():
    module = load_reference()
    for amount in [None, True, 0, -1, float('nan'), float('inf'), 10001, '100']:
        assert module.resolve_reference({'food_name': '白飯', 'amount': amount, 'unit': 'g'}) is None
    result = module.resolve_reference({'food_name': '生去皮雞胸肉', 'amount': 100, 'unit': 'g'})
    assert result['nutrition']['carbohydrate_g'] == 0
    assert '生雞胸' in result['portion_assumption']


def test_soy_reference_does_not_substitute_menu_numbers():
    result = load_reference().resolve_reference({'food_name': '無糖豆漿', 'amount': 500, 'unit': 'g'})
    assert result['nutrition']['calories_kcal'] == 35 * 5
    assert result['nutrition']['protein_g'] == 3.6 * 5
    assert '非個別品牌' in result['portion_assumption']
