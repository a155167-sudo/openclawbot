# Semantic meal pipeline public contract (round 3 tracer bullet)

## Goal

One authorized text-message batch is parsed by AI for **meal slot + every item + amount + unit only**. Nutrition never comes from the parser. The same batch owns at most one quota debit even when nutrition fallback is needed.

## Public API

```python
run_semantic_meal_pipeline(
    *, text: str, user_id: str, message_id: str,
    claim_batch: Callable[[str, str], BatchClaim],
    parse_semantics: Callable[[str, str], Mapping],
    find_reference_nutrition: Callable[[Mapping], Mapping | None],
    estimate_per_100: Callable[[list[Mapping], str], list[Mapping]],
    audit: Callable[[Mapping], None] | None = None,
) -> Mapping
```

`claim_batch(user_id, message_id)` is called **before either AI provider**. It must atomically authorize and claim/debit the server-owned batch receipt, and returns `{allowed, batch_id}`. Denial stops before parser/fallback and before any food-log write. Provider-started unknown/refund behavior remains owned by the existing quota authority; this module never refunds.

`parse_semantics(text, batch_id)` is the AI parser. It must preserve all items and return no nutrition:

```json
{
  "intent": "meal_log|other|clarification",
  "meal_slot": "早餐|午餐|晚餐|點心|",
  "items": [
    {"food_name": "無糖豆漿", "amount": 500, "unit": "ml", "portion_assumption": ""}
  ],
  "clarification": ""
}
```

Allowed canonical units: `g`, `ml`, `serving`, `package`, `natural`. `cc` is normalized by the AI/parser adapter to `ml`. Natural units (e.g. 大杯、一碗) must remain `natural` with a non-empty assumption or clarification; they must never silently become g/ml.

`find_reference_nutrition(request)` is called first for each parsed item. It returns either an official result:

```json
{"status":"matched", "food_name":"...", "basis_amount":100, "basis_unit":"ml", "nutrition": {"calories_kcal":40,"protein_g":3.2,"fat_g":2,"carbohydrate_g":2.5}, "source": {...}}
```

or `null` for a true miss, or an incompatible-unit result:

```json
{"status":"unit_mismatch", "food_exists":true, "available_units":["g"]}
```

An incompatible unit is a clarification, **not** an AI-nutrition miss. `g` and `ml` are never interchangeable.

`estimate_per_100(misses, batch_id)` is called at most once, with every true miss expressed as exactly `100` of the same requested unit. It returns one aligned result per miss with per-100 nutrition, provider/model/raw-audit identity. Application code scales every nutrient/range by `requested_amount / 100`; the model must not perform final-amount scaling.

## Result

```json
{
  "status":"draft|not_meal|clarification|denied",
  "batch_id":"...",
  "meal_slot":"午餐",
  "items":[
    {"request": {...}, "nutrition": {...}, "source_label":"官方資料|AI估算", "source": {...}}
  ],
  "clarifications": []
}
```

Multiple parsed items are never silently reduced to the first item. The caller may render one multi-item draft or explicitly ask for clarification if the current draft UI is single-item-only. `run_semantic_meal_pipeline` does not write `food_logs`; existing owner/version confirmation remains the sole commit boundary.

## Audit separation

Audit events use stages `semantic_parse` and `nutrition_fallback`. Each records the batch id and actual provider/model/raw trace supplied by that provider. An official nutrition match does not erase the parser AI usage.
