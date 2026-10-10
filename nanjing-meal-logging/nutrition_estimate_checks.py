"""Side-effect-free plausibility checks; not a medical or food-identity verdict."""
import math

KEYS = ('calories_kcal', 'protein_g', 'fat_g', 'carbohydrate_g')


def validate_total_basis(payload, request):
    """Require structured identical quantity/unit for explicitly quantified requests."""
    amount = request.get('amount')
    if amount is None:
        return
    reported = payload.get('basis_amount')
    unit = request.get('unit')
    if (isinstance(reported, bool) or not isinstance(reported, (float, int))
            or not math.isfinite(reported) or reported <= 0
            or not math.isclose(reported, amount, rel_tol=1e-9, abs_tol=1e-9)
            or payload.get('basis_unit') != unit):
        raise ValueError('營養估算必須對應本次輸入的完整份量與單位')

def assess_nutrition(nutrition):
    """Missing is unknown; reject invalid values and flag large energy discrepancies."""
    values = {}
    for key in KEYS:
        value = nutrition.get(key)
        if value is None:
            values[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return {'status': 'invalid', 'requires_correction': True, 'reason': key}
        values[key] = float(value)
    if any(value is None for value in values.values()):
        calories = values['calories_kcal']
        lower_bound = sum(values[k] * factor for k, factor in
                          (('protein_g', 4), ('fat_g', 9), ('carbohydrate_g', 4))
                          if values[k] is not None)
        if calories is not None and lower_bound - calories > max(20, calories * .20):
            return {'status': 'inconsistent', 'requires_correction': True,
                    'known_nutrient_energy_lower_bound_kcal': lower_bound,
                    'reason': '已知營養素的能量已超過總熱量，請核對；缺少的營養素仍為未知'}
        return {'status': 'unknown', 'requires_correction': False, 'reason': '缺少營養資料，無法完整核對'}
    energy = 4 * values['protein_g'] + 9 * values['fat_g'] + 4 * values['carbohydrate_g']
    delta = abs(values['calories_kcal'] - energy)
    inconsistent = delta > 20 and delta > values['calories_kcal'] * 0.20
    return {'status': 'inconsistent' if inconsistent else 'consistent',
            'requires_correction': inconsistent, 'macro_energy_kcal': energy,
            'difference_kcal': delta,
            'reason': '熱量與三大營養素差異偏大，請核對；纖維、糖醇與標示四捨五入可能造成差異' if inconsistent else ''}
