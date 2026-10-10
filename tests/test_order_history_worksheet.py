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
