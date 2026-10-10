"""Private, bounded estimate-response audit; never stores chat history or LINE identity.

Retention: thirty days, enforced on append. Only response fields used by nutritional
estimation are retained. Invalid JSON retains an error marker and a response digest.
This separate database cannot mutate historical food records.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

FIELDS = frozenset(('food_name', 'portion_assumption', 'calories_kcal', 'protein_g',
                    'fat_g', 'carbohydrate_g', 'basis_amount', 'basis_unit',
                    'estimate', 'min', 'max', 'unit', 'nutrition', 'range', 'result',
                    'data', 'total', 'error', 'ai_estimate', 'items', 'image_type', 'status',
                    'nutrition_response_excerpt'))

def _redact(text):
    text = re.sub(r'\bU[0-9a-fA-F]{32}\b', '[LINE_ID]', str(text))
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?<!\d)(?:\+?886[- ]?|0)9\d(?:[- ]?\d){7}(?!\d)', '[PHONE]', text)
    text = re.sub(r'https?://\S+', '[URL]', text)
    text = re.sub(r'(?i)(?:bearer\s+|sk-)[A-Za-z0-9_./+=-]+', '[SECRET]', text)
    return text[:2000]

def _safe(value, depth=0, parent_key=''):
    if depth > 5:
        return '[DEPTH_LIMIT]'
    if isinstance(value, dict):
        allowed = FIELDS | ({'name', 'portion'} if parent_key == 'items' else set())
        return {key: _safe(item, depth+1, key) for key, item in value.items() if key in allowed}
    if isinstance(value, list):
        return [_safe(item, depth+1, parent_key) for item in value[:20]]
    if isinstance(value, str):
        if parent_key == 'error' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', value):
            return '[REDACTED_ERROR]'
        return _redact(value)
    if isinstance(value, float) and (value != value or value in (float('inf'), float('-inf'))):
        return '[NONFINITE]'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return '[UNSUPPORTED]'

def record_estimate(path, *, model, response, operation_id='', outcome='received'):
    """Persist each actual provider response (also rejected ones), return opaque trace.

    Failure propagates: callers must not claim an audited estimate if storage fails.
    No request/user profile/raw webhook is accepted by this API.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('audit path must not be a symlink')
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    raw = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False, default=str)
    try:
        decoded = json.loads(raw)
        safe = _safe(decoded) if isinstance(decoded, dict) else {'error': 'invalid_response_shape'}
    except (ValueError, TypeError):
        safe = {'error': 'invalid_json'}  # The raw-response digest remains; free text may echo identity.
    payload = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    now = dt.datetime.now(dt.timezone.utc)
    trace = uuid.uuid4().hex
    with sqlite3.connect(path, timeout=5) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS estimate_audit (
            trace_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, model TEXT NOT NULL,
            operation_hash TEXT NOT NULL, outcome TEXT NOT NULL, response_json TEXT NOT NULL,
            response_hash TEXT NOT NULL)''')
        conn.execute('INSERT INTO estimate_audit VALUES (?,?,?,?,?,?,?)',
                     (trace, now.isoformat(), _redact(model)[:120],
                      hashlib.sha256(str(operation_id).encode()).hexdigest() if operation_id else '',
                      _redact(outcome)[:80], payload, hashlib.sha256(raw.encode()).hexdigest()))
        conn.execute('DELETE FROM estimate_audit WHERE created_at < ?',
                     ((now-dt.timedelta(days=30)).isoformat(),))
    return trace
