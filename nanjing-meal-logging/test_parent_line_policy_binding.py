"""Narrow regression using the actual handler AST, not a LINE E2E claim."""
import ast
import sqlite3
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path('/mnt/c/Users/WIN-XI/AppData/Local/Temp/computer-nutrition-normal-cutover/candidate/server.py')

def test_admin_branch_resolves_shared_policy_when_anchor_table_exists(tmp_path):
    tree = ast.parse(SOURCE.read_text())
    # Select by source semantics rather than assuming the same line after fixes.
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == '_parse_pair_reschedule_command' for x in ast.walk(n)))
    db = tmp_path / 'binding.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE normal_reschedule_expiry_anchors(order_id INTEGER)')
    replies, calls = [], []
    policy = lambda *a: None
    def approve(*args, **kwargs):
        calls.append(kwargs['policy_validator'])
        return SimpleNamespace(status='confirmed')
    env = dict(sqlite3=sqlite3, DB_PATH=str(db), normal_pair_policy=policy,
               _pair_reschedule_command_kind=lambda _: 'admin',
               _pair_reschedule_runtime_enabled=lambda: True,
               _parse_pair_reschedule_command=lambda *a: ('RS_test1234',),
               _reply_pair_reschedule=lambda event, message: replies.append(message),
               verify_admin_context=lambda *a: object(),
               approve_customer_pair_reschedule=approve,
               RESCHEDULE_SHEET_ADAPTER_FACTORY=object(), tw_now=lambda: object(),
               RescheduleAuthorizationError=type('Authorization', (Exception,), {}),
               RescheduleFeatureUnavailable=type('Unavailable', (Exception,), {}),
               RescheduleRequestConflict=type('Conflict', (Exception,), {}))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), 'exec'), env)
    event = SimpleNamespace(source=SimpleNamespace(user_id='fixture-admin'))
    assert env[fn.name](event, '#核准雙餐改期 RS_test1234') is True
    assert len(calls) == 1 and callable(calls[0])
    assert replies == ['雙餐改期已核准並完成。']
