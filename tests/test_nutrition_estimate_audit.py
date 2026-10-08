import importlib.util
import json
from pathlib import Path
import sqlite3

def load_audit():
    path = Path(__file__).resolve().parents[1] / 'nutrition_estimate_audit.py'
    assert path.exists(), 'AI response audit not implemented'
    spec = importlib.util.spec_from_file_location('nutrition_estimate_audit', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_saves_response_nutrition_not_customer_identity(tmp_path):
    module = load_audit()
    path = tmp_path / 'estimate-audit.sqlite3'
    response = json.dumps({'food_name':'豆漿','calories_kcal':{'estimate':287},
                           'protein_g':{'estimate':11.3},'customer_name':'王小明',
                           'user_id':'U'+'a'*32,'email':'someone@example.com'},ensure_ascii=False)
    trace = module.record_estimate(path, model='test-model', response=response,
                                   operation_id='U'+'a'*32, outcome='received')
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT trace_id,response_json,operation_hash FROM estimate_audit').fetchone()
    assert row[0] == trace
    assert json.loads(row[1])['protein_g']['estimate'] == 11.3
    assert '王小明' not in row[1] and 'someone@' not in row[1]
    assert 'U'+'a'*32 not in repr(row)
    assert path.stat().st_mode & 0o077 == 0


def test_invalid_provider_numbers_still_leave_auditable_response(tmp_path):
    module = load_audit()
    path = tmp_path / 'audit.db'
    module.record_estimate(path, model='test', response='{"protein_g":NaN}', outcome='rejected')
    with sqlite3.connect(path) as conn:
        value = json.loads(conn.execute('SELECT response_json FROM estimate_audit').fetchone()[0])
    assert value['protein_g'] == '[NONFINITE]'


def test_private_identifiers_redacted_inside_allowed_text(tmp_path):
    module = load_audit()
    path = tmp_path / 'audit.db'
    module.record_estimate(path, model='test', response=json.dumps({'portion_assumption':
        'user@example.com 0912345678 https://example.com/private?token=secret U'+'a'*32}))
    with sqlite3.connect(path) as conn:
        text = conn.execute('SELECT response_json FROM estimate_audit').fetchone()[0]
    for private in ['user@example.com', '0912345678', 'token=secret', 'U'+'a'*32]:
        assert private not in text


def test_audit_refuses_symlink(tmp_path):
    import pytest
    target = tmp_path / 'history.db'
    target.write_bytes(b'do not touch')
    alias = tmp_path / 'audit.db'
    alias.symlink_to(target)
    with pytest.raises(ValueError):
        load_audit().record_estimate(alias, model='test', response='{}')
    assert target.read_bytes() == b'do not touch'
