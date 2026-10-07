"""Execute the actual Google initialization block; fake only external SDK I/O."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from server_workbook_write_fence import current_server_workbook_write_fence


def startup(pair_enabled, *, google_fails=False):
    tree = ast.parse((Path(__file__).parents[1] / 'server.py').read_text())
    definitions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('_require_controlled_workbook_writer', 'setup_garmin_test_sheet')]
    blocks = [n for n in tree.body if isinstance(n, ast.Try) and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == 'setup_garmin_test_sheet' for x in ast.walk(n))]
    assert len(definitions) == 2 and len(blocks) == 1
    calls = []
    def worksheets():
        calls.append('optional_read')
        return [SimpleNamespace(title='Test_Garmin_Log')]
    def worksheet(name):
        calls.append(name)
        return object()
    book = SimpleNamespace(worksheet=worksheet, worksheets=worksheets)
    def open_by_key(key):
        if google_fails:
            raise RuntimeError('synthetic required Google failure')
        return book
    credentials = SimpleNamespace(from_service_account_info=lambda *a, **k: object())
    ns = dict(os=SimpleNamespace(environ={'GOOGLE_CREDENTIALS': '{}'}), json=json,
              Credentials=credentials, SCOPE=[], APP_ENV='staging', SPREADSHEET_ID='synthetic-only',
              PAIR_RESCHEDULE_ENABLED=pair_enabled,
              current_server_workbook_write_fence=current_server_workbook_write_fence,
              gspread=SimpleNamespace(authorize=lambda c: SimpleNamespace(open_by_key=open_by_key)))
    module = ast.Module(body=definitions + blocks, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'actual_google_startup', 'exec'), ns)
    return ns, book, calls


@pytest.mark.parametrize('pair_enabled', [True, False])
def test_google_connection_survives_optional_startup_in_normal_mode(pair_enabled):
    ns, book, calls = startup(pair_enabled)
    assert ns['sh'] is book, 'optional Garmin bootstrap must not destroy required Google connection'
    assert calls[:2] == ['Master_API_View', 'raw_logs']
    assert calls.count('optional_read') == (0 if pair_enabled else 1)
    if pair_enabled:
        with pytest.raises(RuntimeError, match='garmin_sheet'):
            ns['setup_garmin_test_sheet'](book, app_env='staging')
        with pytest.raises(RuntimeError, match='garmin_sheet'):
            ns['write_workout_to_sheet']('synthetic-user', {})
        assert calls == ['Master_API_View', 'raw_logs']


def test_required_google_failure_is_not_hidden():
    ns, _, calls = startup(True, google_fails=True)
    assert ns['sh'] is None and ns['gc'] is None
    assert calls == []
