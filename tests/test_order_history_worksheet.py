"""Order-history rows must go to the raw_logs tab by name, never the first tab."""
import ast
from pathlib import Path
from types import SimpleNamespace

import gspread
import pytest

SERVER = Path(__file__).parents[1] / "server.py"


def _load_helper():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    nodes = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "_order_history_worksheet")
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "ORDER_HISTORY_WORKSHEET_TITLE" for t in n.targets))
    ]
    assert len(nodes) == 2
    ns = {"gspread": gspread}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "order_history_helper", "exec"), ns)
    return ns["_order_history_worksheet"]


class FakeBook:
    def __init__(self, titles):
        self.titles = titles
        self.requested = []

    @property
    def sheet1(self):  # pragma: no cover - must never be touched
        raise AssertionError("first tab must not be used for order history")

    def worksheet(self, title):
        self.requested.append(title)
        if title not in self.titles:
            raise gspread.exceptions.WorksheetNotFound(title)
        return SimpleNamespace(title=title)


def test_returns_raw_logs_even_when_first_tab_is_master_api_view():
    helper = _load_helper()
    book = FakeBook(["Master_API_View", "raw_logs"])
    assert helper(book).title == "raw_logs"
    assert book.requested == ["raw_logs"]


def test_nanjing_layout_unchanged():
    helper = _load_helper()
    book = FakeBook(["raw_logs", "Master_API_View"])
    assert helper(book).title == "raw_logs"


def test_missing_raw_logs_fails_closed():
    helper = _load_helper()
    book = FakeBook(["Master_API_View"])
    with pytest.raises(RuntimeError, match="raw_logs"):
        helper(book)


def test_server_never_appends_order_history_to_first_tab():
    source = SERVER.read_text(encoding="utf-8")
    assert ".sheet1" not in source
    assert source.count("main_sheet = _order_history_worksheet(sheet)") == 2


def test_payment_gate_resolves_history_tab_before_any_write():
    source = SERVER.read_text(encoding="utf-8")
    start = source.index('print(f"📊 [PAYMENT_GATE] 正式寫入 Google Sheet')
    lookup = source.index("main_sheet = _order_history_worksheet(sheet)", start)
    first_write = source.index("active_fence.mark_write_started()", start)
    append = source.index("main_sheet.append_row(", start)
    assert lookup < first_write < append


def _load_schedule_helpers():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_schedule_number", "_schedule_balance_text")]
    assert len(nodes) == 2
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "schedule_helpers", "exec"), ns)
    return ns["_schedule_number"], ns["_schedule_balance_text"]


def test_schedule_sums_drop_float_noise_that_breaks_sheet_readback():
    number, _ = _load_schedule_helpers()
    # 30.989 + 32.53 == 63.519000000000005 in binary floats; Sheets shows 63.519,
    # which made the dispatch readback (and the printer's exact compare) fail.
    assert str(number(30.989 + 32.53)) == "63.519"
    assert str(number(383.21 + 508.605)) == "891.815"
    assert str(number(484 + 0)) == "484"
    assert isinstance(number(484.0), int)


def test_balance_text_integer_kcal_one_decimal_protein():
    _, text = _load_schedule_helpers()
    assert text(2511 - 871.015, 140 - 63.519000000000005) == "剩 1640kcal / 補 76.5g"
    assert text(1619, 79) == "剩 1619kcal / 補 79g"


def test_schedule_builders_use_rounded_values():
    source = SERVER.read_text(encoding="utf-8")
    assert 'f"剩 {day_tdee_left}kcal / 補 {day_p_need}g"' not in source
    assert source.count("_schedule_balance_text(day_tdee_left, day_p_need)") == 2
