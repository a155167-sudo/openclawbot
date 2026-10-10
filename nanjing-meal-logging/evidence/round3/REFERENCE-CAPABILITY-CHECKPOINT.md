# Round 3 reference capability checkpoint

## Public pure functions

### `reference_capability(food_name, unit)`

No AI, database, network, or density conversion. `cc` is spelling-normalized to `ml` only.

Exact compatible unit:

```json
{
  "status": "exact",
  "food_exists": true,
  "available_units": ["g"],
  "source_basis": {"amount": 100, "unit": "g"},
  "source": {"food_code": "A0550601", "source_row_id": "A0550601", "...": "trace fields"}
}
```

Known food with incompatible unit:

```json
{
  "status": "unit_mismatch",
  "food_exists": true,
  "available_units": ["g"],
  "source_basis": {"amount": 100, "unit": "g"},
  "source": {"food_code": "A0550601", "source_row_id": "A0550601", "...": "trace fields"}
}
```

Unknown identity:

```json
{
  "status": "missing",
  "food_exists": false,
  "available_units": [],
  "source_basis": null,
  "source": null
}
```

### `find_reference_nutrition(request)`

- Exact identity + compatible unit: returns `status=matched`, published `basis_amount` / `basis_unit`, unscaled per-basis nutrition, and source trace.
- Exact identity + incompatible unit: returns only `status=unit_mismatch`, `food_exists=true`, and `available_units`.
- Unknown identity or invalid amount: returns `None`.
- The caller performs final same-unit scaling from the returned 100-unit basis.

### `resolve_reference(request)`

Legacy API retained. It returns requested-amount totals for exact same-unit matches and `None` otherwise. Official `null` nutrient values remain `None` after scaling.

## Unit and source invariants

- The 44 checked-in TFDA rows all use official `每100克含量`, represented as `basis_amount=100`, `basis_unit=g`.
- `H1150201` soy milk and `L01021` whole fresh milk are mass records. Their gram-denominated unit weights are not ml evidence.
- No g↔ml conversion is implemented.
- Every catalog row carries TFDA integrated row ID, dataset version, official source URL, archive hash, evidence hash, and selected-source-row hash.
- Full catalog and exact adopted evidence rows are in `TFDA-CATALOG.md` and `TFDA-CATALOG.json`.
