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


def test_meal_moved_by_an_earlier_reschedule_can_be_moved_again(tmp_path):
    """Staging 10/11: 10/26 (moved there earlier) → 11/02 failed with
    'single formalized source profile row is required' because only the latest
    reschedule's snapshot was searched for the moved meal's Master profile."""
    import json as _json
    from test_pair_reschedule_coordinator import (
        AtomicSheetFake, OWNER as COORD_OWNER, SOURCE as A, TARGET as B, THIRD as C,
        call, old_source_row, open_db,
    )
    fourth, fifth = "2026-09-30", "2026-10-01"
    third_row = old_source_row()
    third_row[0], third_row[1], third_row[2], third_row[5] = C, "第2週-二", "午餐C", "晚餐D"
    conn, _ = open_db(tmp_path / "chain.sqlite3", extra_published_rows=[("dispatch-third", third_row)])
    conn.executemany("INSERT INTO subscription_service_calendar VALUES(1,?,1,?)",
                     [(fourth, "第2週-三"), (fifth, "第2週-四")])
    # Formalized Master profile exists only for the original meal days A and C.
    payload = _json.loads(conn.execute("SELECT form_payload_json FROM subscription_orders WHERE id=1").fetchone()[0])
    template = payload["master_api_rows"][0]
    rows = []
    for day in (A, C):
        row = list(template)
        row[0] = day
        rows.append(row)
    conn.execute("UPDATE subscription_orders SET form_payload_json=? WHERE id=1",
                 (_json.dumps({"master_api_rows": rows}, ensure_ascii=False),))
    conn.commit()
    class RealisticMasterSheet(AtomicSheetFake):
        # The base fake records blank Master rows; record real profile rows
        # (like Google does) so a later move can reuse the moved profile.
        def plan_pair_reschedule(self, **kwargs):
            plan = super().plan_pair_reschedule(**kwargs)
            for day, sched in plan.expected_readback["schedule"].items():
                row = [day.replace("-", "/")] + list(template[1:])
                row[3], row[4] = (sched[2] or "無"), (sched[5] or "無")
                plan.expected_readback["master"][day] = row
            return plan

    sheet = RealisticMasterSheet([
        old_source_row() + ["dispatch-old", "1", "order-1-v1"],
        third_row + ["dispatch-third", "1", "order-1-v1"],
    ])
    assert call(conn, sheet, source_date=A, target_date=B, request_id="move-1").status == "confirmed"
    assert call(conn, sheet, source_date=C, target_date=fourth, request_id="move-2").status == "confirmed"
    # B was moved by move-1, not by the latest reschedule (move-2).
    assert call(conn, sheet, source_date=B, target_date=fifth, request_id="move-3").status == "confirmed"
    assert sheet.rows[fifth][2] == "午餐A"
