"""Retain former damage fixtures as no-damage regressions; no network/server import."""
import ast
from pathlib import Path
from types import SimpleNamespace
import json
from contextlib import closing
from datetime import date
from gspread_pair_reschedule_adapter import GspreadPairRescheduleAdapter


def test_retired_restore_preserves_dispatch_identity():
    source = Path(__file__).resolve().parents[1] / 'server.py'
    tree = ast.parse(source.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'restore_staging_schedule_sheet_once')
    payload = ['2026/10/05', 'Monday', 'lunch', 500, 30, 'dinner', 600, 35, 1100, 65, '', 360, '', 'pending']
    class Conn:
        def execute(self, sql):
            self.sql = sql
            return self
        def fetchone(self):
            if 'form_payload_json' in self.sql:
                return (json.dumps({'schedule_sheet_rows': [['h'] * 14, payload]}),)
            return ('offline-book', 'offline-sheet')
        def close(self): pass
    class Sheet:
        rows = [payload + ['durable-id', '1', 'order-1-v1']]
        def clear(self): self.rows = []
        def update(self, **kwargs): self.rows = kwargs['values']
    sheet = Sheet()
    ns = dict(APP_ENV='staging', os=SimpleNamespace(environ={'STAGING_RESCHEDULE_SHEET_RESTORE':'1'}),
              gc=SimpleNamespace(open_by_key=lambda _: SimpleNamespace(worksheet=lambda _: sheet)),
              sqlite3=SimpleNamespace(connect=lambda _: Conn()), DB_PATH='offline', closing=closing, json=json)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), ns)
    ns[fn.name]()
    assert sheet.rows == [payload + ['durable-id', '1', 'order-1-v1']]
    positions = GspreadPairRescheduleAdapter._schedule_positions(sheet.rows, {'2026-10-05'}, allow_missing={'2026-10-05'})
    assert positions == {'2026-10-05': 0}


def test_retired_duplicate_repair_preserves_both_ambiguous_rows():
    """No first-row-wins deletion even when the legacy row looks width-compatible."""
    source = Path(__file__).resolve().parents[1] / 'server.py'
    tree = ast.parse(source.read_text())
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == 'repair_staging_duplicate_dates_once'
    )
    padded_legacy = ['2026/10/05'] + ['legacy'] * 13 + ['', '', '']
    dispatch = ['2026-10-05'] + ['published'] * 13 + ['durable-id', '1', 'order-1-v1']

    class Conn:
        def execute(self, _sql): return self
        def fetchone(self): return ('offline-book', 'offline-sheet')
        def close(self): pass

    class Sheet:
        def __init__(self):
            self.rows = [['header'] + [''] * 16, padded_legacy, dispatch]
            self.deleted = []
            self.reads = 0
        def get_all_values(self):
            self.reads += 1
            return [list(row) for row in self.rows]
        def delete_rows(self, index):
            self.deleted.append(index)
            del self.rows[index - 1]

    sheet = Sheet()
    ns = dict(
        APP_ENV='staging',
        os=SimpleNamespace(environ={'STAGING_RESCHEDULE_REPAIR_DUPLICATES': '1'}),
        gc=SimpleNamespace(open_by_key=lambda _: SimpleNamespace(worksheet=lambda _: sheet)),
        sqlite3=SimpleNamespace(connect=lambda _: Conn()),
        DB_PATH='offline', closing=closing, date=date,
    )
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), ns)
    ns[fn.name]()

    assert sheet.deleted == []
    assert sheet.reads == 0
    assert sheet.rows[1] == padded_legacy
    assert sheet.rows[2] == dispatch
    assert len(sheet.rows) == 3
