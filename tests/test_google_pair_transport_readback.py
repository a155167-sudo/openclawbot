"""Offline Google transport readback regressions for the two-view pair."""
import copy

import pytest

from gspread_pair_reschedule_adapter import (
    PairSheetConflict, PersistedMasterProfile, pair_transport_row_equal,
)
from test_gspread_pair_reschedule_adapter import (
    BOOK, OWNER, SOURCE, TARGET, after_rows, make_adapter, master_row,
    persisted_profile,
)
from test_pair_reschedule_coordinator import (
    ADMIN, NOW, AtomicSheetFake, call, old_source_row, open_db,
)
from pair_reschedule_coordinator import reconcile_pair_reschedule_readback, verify_admin_context


NUMERIC_COLUMNS = (2, 6, 18, 20)


def test_target_master_optional_user_level_stays_exact():
    row = [""] * 21
    row[0], row[1], row[2], row[6], row[20] = (
        "2026-10-28", "owner", "1904", "0", "0",
    )

    def equal(observed):
        return pair_transport_row_equal(
            "master", row, observed, owner="owner", target="2026-10-28",
        )

    assert equal(row)
    for index in (2, 6, 20):
        changed = row.copy()
        changed[index] = ""
        assert not equal(changed)
        assert not pair_transport_row_equal(
            "master", changed, changed, owner="owner", target="2026-10-28",
        )
    for index, value in ((1, "another-owner"), (2, "1905"),
                         (6, False), (20, float("nan")),
                         (18, "2"), (18, False)):
        changed = row.copy()
        changed[index] = value
        assert not equal(changed)

    named_level = row.copy()
    named_level[18] = "beginner"
    assert pair_transport_row_equal(
        "master", named_level, named_level,
        owner="owner", target="2026-10-28",
    )
    changed_level = named_level.copy()
    changed_level[18] = "Beginner"
    assert not pair_transport_row_equal(
        "master", named_level, changed_level,
        owner="owner", target="2026-10-28",
    )
    named_level[18] = "NaN"
    assert not pair_transport_row_equal(
        "master", named_level, named_level,
        owner="owner", target="2026-10-28",
    )


@pytest.mark.parametrize("index,value", [
    (2, 1904), (6, 0), (18, 2), (20, 0),
])
def test_expected_numeric_text_never_accepts_native_number(index, value):
    expected = [""] * 21
    expected[0:3] = ["2026-10-28", "owner", "1904"]
    expected[6], expected[18], expected[20] = "0", "2", "0"
    observed = expected.copy()
    observed[index] = value
    assert not pair_transport_row_equal(
        "master", expected, observed, owner="owner", target="2026-10-28",
    )


def test_native_number_to_decimal_text_is_one_way_and_finite():
    expected = [""] * 21
    expected[0:3] = ["2026-10-28", "owner", 1904]
    expected[6], expected[18], expected[20] = 0, 2.0, 0
    observed = expected.copy()
    for index, value in ((2, "1904.0"), (6, "0"), (18, "2"), (20, "0")):
        observed[index] = value
    assert pair_transport_row_equal(
        "master", expected, observed, owner="owner", target="2026-10-28",
    )
    for invalid in ("", "NaN", "Inf", " 1904", True, float("inf")):
        changed = observed.copy()
        changed[2] = invalid
        assert not pair_transport_row_equal(
            "master", expected, changed, owner="owner", target="2026-10-28",
        )


def numeric_profile():
    profile = persisted_profile()
    values = dict(profile.__dict__)
    values.update(tdee=1904, is_coaching_enabled=0, user_level=2,
                  is_carb_cycling_enabled=0)
    return PersistedMasterProfile(**values)


def make_written_adapter():
    adapter, book, schedule, master = make_adapter(master_rows=[
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B"),
        master_row("2026-09-29", owner="another-user", lunch="other"),
        [],
    ])
    plan = adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date=TARGET, schedule_rows=after_rows(),
        target_master_profile=numeric_profile(),
    )
    adapter.apply_pair_reschedule(plan)
    # Model get_all_values after appendCells used hidden trailing blank rows.
    master.rows[-1:-1] = [[], ["", ""], [""]]
    for column in NUMERIC_COLUMNS:
        master.rows[-1][column] = str(master.rows[-1][column])
    return adapter, book, schedule, master, plan


def test_first_readback_accepts_only_numeric_transport_and_blank_padding():
    adapter, book, _schedule, _master, plan = make_written_adapter()
    before = copy.deepcopy(plan.expected_readback)
    observed = adapter.read_pair_reschedule(plan)
    assert observed["master"][TARGET][2] == "1904"
    assert len(book.batch_calls) == 1
    assert plan.expected_readback == before


@pytest.mark.parametrize("change", [
    "meal", "date", "owner", "version", "numeric", "bool", "nan", "inf",
    "other_owner", "extra_column", "header", "duplicate", "unapproved_number",
    "nonblank_padding",
])
def test_first_readback_rejects_other_drift(change):
    adapter, book, schedule, master, plan = make_written_adapter()
    target = master.rows[-1]
    if change == "meal":
        target[3] = "other meal"
    elif change == "date":
        target[0] = "2026-09-30"
    elif change == "owner":
        target[1] = "another-user"
    elif change == "version":
        schedule.rows[2][16] = "other-version"
    elif change == "numeric":
        target[2] = "1905"
    elif change == "bool":
        target[6] = False
    elif change == "nan":
        target[2] = float("nan")
    elif change == "inf":
        target[2] = float("inf")
    elif change == "other_owner":
        master.rows[2][3] = "changed"
    elif change == "extra_column":
        target.append("unexpected")
    elif change == "header":
        master.rows[0][3] = "changed"
    elif change == "duplicate":
        master.rows.append(copy.deepcopy(target))
    elif change == "unapproved_number":
        target[12] = 3
    elif change == "nonblank_padding":
        master.rows[-2] = ["hidden-data"]
    with pytest.raises(PairSheetConflict):
        adapter.read_pair_reschedule(plan)
    assert len(book.batch_calls) == 1


class SnapshotTransportSheet(AtomicSheetFake):
    """Models immutable numeric expected cells and Google string readback."""

    def plan_pair_reschedule(self, **kwargs):
        plan = super().plan_pair_reschedule(**kwargs)
        plan.expected_readback["master"][TARGET][0] = TARGET
        for index, value in zip(NUMERIC_COLUMNS, (1904, 0, 2, 0)):
            plan.expected_readback["master"][TARGET][index] = value
        self.plan = plan
        return plan

    def read_pair_reschedule(self, plan):
        observed = super().read_pair_reschedule(plan)
        for index in NUMERIC_COLUMNS:
            observed["master"][TARGET][index] = str(observed["master"][TARGET][index])
        return observed

    def read_pair_rows(self, **kwargs):
        observed = super().read_pair_rows(**kwargs)
        observed["master"][TARGET][0] = TARGET
        for index in NUMERIC_COLUMNS:
            observed["master"][TARGET][index] = str(self.plan.expected_readback["master"][TARGET][index])
        return observed


def test_first_coordinator_readback_confirms_transport_equivalent(tmp_path):
    conn, _ = open_db(tmp_path / "first.sqlite3")
    sheet = SnapshotTransportSheet([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    result = call(conn, sheet)
    assert result.status == "confirmed"
    assert len(sheet.batch_calls) == 1


def test_unknown_reconcile_checks_immutable_snapshot_without_sheet_write(tmp_path):
    conn, _ = open_db(tmp_path / "unknown.sqlite3")
    sheet = SnapshotTransportSheet(
        [old_source_row() + ["dispatch-old", "1", "order-1-v1"]],
        "apply_then_timeout",
    )
    initial = call(conn, sheet)
    assert initial.status == "sheet_unknown"
    before_snapshot = conn.execute(
        "SELECT view_name,service_date,row_json,row_hash FROM pair_reschedule_view_rows ORDER BY view_name,service_date"
    ).fetchall()
    before_snapshot = [tuple(row) for row in before_snapshot]
    assert call(conn, sheet).status == "sheet_unknown"
    result = reconcile_pair_reschedule_readback(
        conn, sheet, order_id=1, request_id="request-1",
        admin_context=verify_admin_context(conn, ADMIN), now=NOW,
    )
    assert result.status == "confirmed"
    assert len(sheet.batch_calls) == 1
    assert sheet.update_cell_calls == []
    assert [tuple(row) for row in conn.execute(
        "SELECT view_name,service_date,row_json,row_hash FROM pair_reschedule_view_rows ORDER BY view_name,service_date"
    )] == before_snapshot
    assert tuple(conn.execute(
        "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id='book-1'"
    ).fetchone()) == ("released", "confirmed")
    assert reconcile_pair_reschedule_readback(
        conn, sheet, order_id=1, request_id="request-1",
        admin_context=verify_admin_context(conn, ADMIN), now=NOW,
    ).status == "confirmed"
    assert len(sheet.batch_calls) == 1


@pytest.mark.parametrize("change", ["meal", "owner", "version", "numeric", "bool", "nan", "inf", "unapproved_number"])
def test_unknown_reconcile_rejects_drift_and_does_not_write(tmp_path, change):
    conn, _ = open_db(tmp_path / "reject.sqlite3")
    sheet = SnapshotTransportSheet(
        [old_source_row() + ["dispatch-old", "1", "order-1-v1"]],
        "apply_then_timeout",
    )
    assert call(conn, sheet).status == "sheet_unknown"
    original = sheet.read_pair_rows

    def changed(**kwargs):
        observed = original(**kwargs)
        target = observed["master"][TARGET]
        if change == "meal":
            target[3] = "wrong"
        elif change == "owner":
            target[1] = "another-user"
        elif change == "version":
            observed["schedule"][TARGET][16] = "other-version"
        elif change == "numeric":
            target[2] = "1905"
        elif change == "bool":
            target[6] = False
        elif change == "nan":
            target[2] = float("nan")
        elif change == "inf":
            target[2] = float("inf")
        elif change == "unapproved_number":
            target[12] = 3
        return observed

    sheet.read_pair_rows = changed
    assert reconcile_pair_reschedule_readback(
        conn, sheet, order_id=1, request_id="request-1",
        admin_context=verify_admin_context(conn, ADMIN), now=NOW,
    ).status == "sheet_unknown"
    assert len(sheet.batch_calls) == 1
    assert sheet.update_cell_calls == []
