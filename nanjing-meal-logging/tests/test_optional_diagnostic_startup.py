import ast
from pathlib import Path


def test_sheet_diagnostic_is_not_run_while_importing_server():
    tree=ast.parse((Path(__file__).resolve().parents[1]/'server.py').read_text())
    # Execute only the module-level diagnostic call against a failing network sentinel.
    calls=[n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
           and isinstance(n.value.func,ast.Name) and n.value.func.id=='log_staging_reschedule_sheet_shape']
    attempted=[]
    def blocked_network(): attempted.append('network')
    exec(compile(ast.Module(body=calls,type_ignores=[]),'diagnostic-startup','exec'),
         {'log_staging_reschedule_sheet_shape':blocked_network})
    assert attempted==[], 'optional Sheet diagnostics must not block module import'
