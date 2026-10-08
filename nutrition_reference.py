"""Taiwan-first nutrition references: exact food identity and units only.

Composition rows are never used to infer a mass/volume conversion.  In
particular, a TFDA ``per 100 g`` soy-milk row cannot answer an ``ml`` request
unless a separately sourced volume reference exists in the data file.
"""
import json
import math
from pathlib import Path
import unicodedata

DATA_PATH = Path(__file__).parent / 'nutrition_reference_data.json'

def _key(value):
    return ''.join(unicodedata.normalize('NFKC', str(value)).split()).casefold()

def resolve_reference(request):
    """Return total nutrients and immutable source metadata, or None (not a match)."""
    amount = request.get('amount')
    if isinstance(amount, bool) or not isinstance(amount, (float, int)) or not math.isfinite(amount) or not 0 < amount <= 10000:
        return None
    unit = {'克': 'g', '公克': 'g', '毫升': 'ml', 'cc': 'ml'}.get(request.get('unit'), request.get('unit'))
    name = _key(request.get('food_name', ''))
    document = json.loads(DATA_PATH.read_text(encoding='utf-8'))
    matches = [item for item in document['items']
               if name in {_key(alias) for alias in item['aliases']} and unit == item['basis_unit']]
    if len(matches) != 1:
        return None
    item = matches[0]
    ratio = amount / item['basis_amount']
    nutrition = {key: None if value is None else value * ratio for key, value in item['nutrition'].items()}
    source_fields = (
        'food_code', 'reference_id', 'name', 'state', 'source_url',
        'basis_amount', 'basis_unit', 'publisher', 'source_product',
        'source_type', 'source_note', 'reference_label', 'source_label',
        'dataset_url', 'retrieved_at', 'evidence_sha256',
    )
    source = {key: item[key] for key in source_fields if key in item}
    source.update(type='reference', version=document['version'])
    return {'food_name': request['food_name'], 'amount': amount, 'unit': unit,
            'portion_assumption': f"{amount:g}{unit}；{item['state']}；{item['reference_label']}",
            'nutrition': nutrition,
            'source': source}
