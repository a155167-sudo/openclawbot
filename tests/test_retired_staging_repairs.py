"""Retired repair entrypoints must not access Sheets or SQLite, even with flags on."""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('name,flag',[
    ('restore_staging_schedule_sheet_once','STAGING_RESCHEDULE_SHEET_RESTORE'),
    ('repair_staging_duplicate_dates_once','STAGING_RESCHEDULE_REPAIR_DUPLICATES'),
])
@pytest.mark.parametrize('env',['staging','production','legacy'])
@pytest.mark.parametrize('value',['1','0',''])
def test_retired_entrypoints_cannot_touch_external_state(name,flag,env,value):
    source=Path(__file__).resolve().parents[1]/'server.py'
    tree=ast.parse(source.read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    calls=[]
    def forbidden(*a,**kw):
        calls.append(True)
        raise AssertionError('retired repair attempted external access')
    ns={'APP_ENV':env,'os':SimpleNamespace(environ={flag:value}),
        'sqlite3':SimpleNamespace(connect=forbidden),'DB_PATH':'forbidden',
        'gc':SimpleNamespace(open_by_key=forbidden),'closing':lambda x:x}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),ns)
    ns[name]()
    assert calls==[]
