"""Reschedule must not move another order's Master_API_View row; new personal
rows look like activation rows (2026/10/30, 第N週-週X)."""
import sqlite3
from datetime import date

import pytest

from gspread_pair_reschedule_adapter import PairSheetConflict
from normal_reschedule_policy import _schedule_week_label
from test_gspread_pair_reschedule_adapter import (
    BOOK, OWNER, SOURCE, TARGET, after_rows, make_adapter, master_row,
)


def _plan(adapter):
    return adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
    )


def test_master_row_from_other_overlapping_order_is_refused_before_write():
    # Same customer, same date, but Master holds the other order's meals.
    adapter, book, _schedule, _master = make_adapter(master_rows=[
        master_row(SOURCE, lunch="別期午餐", dinner="別期晚餐", workout="長跑"),
        master_row(TARGET, workout="間歇"),
    ])
    with pytest.raises(PairSheetConflict, match="different plan"):
        _plan(adapter)
    assert book.batch_calls == []


def test_matching_master_row_ignores_price_suffix():
    adapter, _book, _schedule, _master = make_adapter()  # 午餐A ($100) vs 午餐A
    assert _plan(adapter).target_date == TARGET


def test_week_label_counts_from_first_service_week():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE subscription_dispatch_rows(order_id INTEGER, service_date TEXT)")
    conn.executemany("INSERT INTO subscription_dispatch_rows VALUES (1, ?)",
                     [("2026-09-28",), ("2026-10-23",)])
    assert _schedule_week_label(conn, 1, date(2026, 9, 28)) == "第1週-週一"
    assert _schedule_week_label(conn, 1, date(2026, 10, 12)) == "第3週-週一"
    assert _schedule_week_label(conn, 1, date(2026, 10, 30)) == "第5週-週五"
    assert _schedule_week_label(conn, 2, date(2026, 10, 30)) == ""
