import importlib.util
from pathlib import Path

def test_energy_mismatch_is_not_accepted_as_normal():
    path = Path(__file__).resolve().parents[1] / 'nutrition_estimate_checks.py'
    assert path.exists(), 'nutrition plausibility gate not implemented'
    spec = importlib.util.spec_from_file_location('nutrition_estimate_checks', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.assess_nutrition({'calories_kcal': 287, 'protein_g': 113, 'fat_g': 5, 'carbohydrate_g': 49})
    assert result['status'] == 'inconsistent'
    assert result['requires_correction'] is True


def test_incomplete_nutrients_are_unknown_not_zero():
    import nutrition_estimate_checks as module
    result = module.assess_nutrition({'calories_kcal': 287, 'protein_g': 11.3})
    assert result['status'] == 'unknown'
    assert 'macro_energy_kcal' not in result


def test_missing_fat_and_carbs_cannot_hide_impossible_protein_energy():
    import nutrition_estimate_checks as module
    result = module.assess_nutrition({'calories_kcal':287,'protein_g':113})
    assert result['status'] == 'inconsistent'
    assert result['requires_correction']
    assert 'macro_energy_kcal' not in result


def test_partial_values_are_validated_before_unknown_result():
    import nutrition_estimate_checks as module
    assert module.assess_nutrition({'calories_kcal':float('nan'),'protein_g':11.3})['status'] == 'invalid'


def test_consistent_macros_cannot_prove_food_identity():
    import nutrition_estimate_checks as module
    result = module.assess_nutrition({'calories_kcal':287,'protein_g':11.3,'fat_g':5,'carbohydrate_g':49})
    assert result['status'] == 'consistent'
    assert result['macro_energy_kcal'] == 4*11.3+9*5+4*49


def test_invalid_numbers_never_pass():
    import nutrition_estimate_checks as module
    for value in [-1, True, float('nan'), float('inf'), '11.3']:
        result = module.assess_nutrition({'calories_kcal':100,'protein_g':value,'fat_g':0,'carbohydrate_g':0})
        assert result['status'] == 'invalid'
        assert result['requires_correction']


def test_rounding_and_absolute_threshold():
    import nutrition_estimate_checks as module
    assert module.assess_nutrition({'calories_kcal':0,'protein_g':0,'fat_g':0,'carbohydrate_g':0})['status'] == 'consistent'
    assert module.assess_nutrition({'calories_kcal':10,'protein_g':0,'fat_g':0,'carbohydrate_g':0})['status'] == 'consistent'


def test_explicit_basis_must_match_requested_total_not_just_contain_number():
    import nutrition_estimate_checks as module
    import pytest
    assert hasattr(module, 'validate_total_basis'), 'structured total basis validator missing'
    request = {'amount':500,'unit':'ml'}
    module.validate_total_basis({'basis_amount':500,'basis_unit':'ml'}, request)
    for payload in [{'basis_amount':100,'basis_unit':'ml'}, {'basis_amount':500,'basis_unit':'g'},
                    {'portion_assumption':'500ml，但營養為每100ml'}, {'basis_amount':True,'basis_unit':'ml'}]:
        with pytest.raises(ValueError):
            module.validate_total_basis(payload, request)
