"""Offline admission checks through the registered normal service and strict fake Sheet."""
import copy
import json

import pytest

from gspread_pair_reschedule_adapter import (
    PairSheetConflict, PersistedMasterProfile, build_target_master_row,
)
from normal_reschedule_policy import normal_pair_policy
from pair_reschedule_coordinator import verify_admin_context
from reschedule_service_integration import (
    RescheduleRequestConflict, approve_customer_pair_reschedule,
    submit_customer_pair_reschedule_pending, verify_customer_reschedule_context,
)
from test_gspread_pair_reschedule_adapter import (
    BOOK, after_rows, make_adapter, master_row, persisted_profile,
)
from test_normal_reschedule_integration import TARGET, normal_db
from test_pair_reschedule_coordinator import ADMIN, NOW, OWNER, SOURCE
from test_reschedule_service_integration import sheet_fixture


INVALID = [
    (2, 0), (2, -1), (2, "0"), (2, "-1"),
    (2, float("nan")), (2, float("inf")), (2, float("-inf")),
    (6, 2), (6, .5), (6, -1), (6, True), (6, float("nan")),
    (20, 2), (20, .5), (20, -1), (20, False), (20, float("inf")),
]


@pytest.mark.parametrize("index,value", INVALID)
@pytest.mark.parametrize("target_present", [True, False], ids=["existing", "missing"])
def test_normal_service_rejects_invalid_target_numeric_before_batch(
    tmp_path, index, value, target_present,
):
    conn = normal_db(tmp_path / "normal.sqlite3")
    sheet, book = sheet_fixture()
    if target_present:
        target = master_row(TARGET)
        target[index] = value
        sheet._master.rows.append(target)
    else:
        source = master_row(SOURCE, lunch="午餐A", dinner="晚餐B")
        source[index] = value
        conn.execute("UPDATE subscription_orders SET form_payload_json=? WHERE id=1", (
            json.dumps({"master_api_rows": [source]}, ensure_ascii=False),
        ))
        conn.commit()
    before_master = copy.deepcopy(sheet._master.rows)
    before_receipts = conn.execute(
        "SELECT count(*) FROM subscription_dispatch_publication_receipts"
    ).fetchone()[0]
    context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule_pending(
        conn, context=context, source_date=SOURCE, target_date=TARGET,
        request_id="RS_numeric", now=NOW, feature_enabled=True,
    )
    with pytest.raises(RescheduleRequestConflict):
        approve_customer_pair_reschedule(
            conn, sheet, request_id="RS_numeric",
            admin_context=verify_admin_context(conn, ADMIN), now=NOW,
            feature_enabled=True, policy_validator=normal_pair_policy,
        )
    assert book.batch_calls == []
    assert repr(sheet._master.rows) == repr(before_master)
    assert conn.execute(
        "SELECT count(*) FROM subscription_dispatch_publication_receipts"
    ).fetchone()[0] == before_receipts
    assert conn.execute(
        "SELECT count(*) FROM customer_pair_reschedule_requests WHERE status='confirmed'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("index,value", INVALID)
def test_builder_rejects_invalid_numeric_metadata(index, value):
    profile = persisted_profile()
    fields = {2: "tdee", 6: "is_coaching_enabled", 20: "is_carb_cycling_enabled"}
    values = {**profile.__dict__, fields[index]: value}
    with pytest.raises(PairSheetConflict):
        build_target_master_row(
            target_date=TARGET, owner_user_id=OWNER, lunch_item="午餐A",
            dinner_item="晚餐B", profile=PersistedMasterProfile(**values),
        )


@pytest.mark.parametrize("tdee,coaching,carb", [
    ("1904.25", "0", "1"), (1904.25, 0, 1), ("1.90425e3", "1.0", "0.0"),
])
def test_builder_preserves_valid_numeric_values_and_user_level_text(tdee, coaching, carb):
    profile = persisted_profile()
    profile = PersistedMasterProfile(**{
        **profile.__dict__, "tdee": tdee, "is_coaching_enabled": coaching,
        "is_carb_cycling_enabled": carb, "user_level": "自由文字",
    })
    original = copy.deepcopy(profile)
    row = build_target_master_row(
        target_date=TARGET, owner_user_id=OWNER, lunch_item="午餐A",
        dinner_item="晚餐B", profile=profile,
    )
    assert (row[2], row[6], row[20], row[18]) == (tdee, coaching, carb, "自由文字")
    assert profile == original


@pytest.mark.parametrize("index,value", [(2, 0), (2, float("nan")), (6, 2), (20, False)])
def test_invalid_expected_target_snapshot_cannot_be_applied_or_read(index, value):
    adapter, book, _schedule, _master = make_adapter()
    plan = adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date="2026-09-28", schedule_rows=after_rows(),
    )
    plan.expected_readback["master"]["2026-09-28"][index] = value
    with pytest.raises(PairSheetConflict):
        adapter.apply_pair_reschedule(plan)
    with pytest.raises(PairSheetConflict):
        adapter.read_pair_reschedule(plan)
    assert book.batch_calls == []


@pytest.mark.parametrize("index,value", [(2, -1), (2, float("inf")), (6, True), (20, .5)])
def test_reconcile_row_reader_rejects_invalid_observed_target(index, value):
    target = master_row("2026-09-28")
    target[index] = value
    adapter, book, _schedule, _master = make_adapter(master_rows=[
        master_row(SOURCE, lunch="午餐A", dinner="晚餐B"), target,
    ])
    with pytest.raises(PairSheetConflict):
        adapter.read_pair_rows(
            owner_user_id=OWNER, source_date=SOURCE, target_date="2026-09-28",
        )
    assert book.batch_calls == []


def test_target_numeric_admission_leaves_source_and_other_owner_exact():
    source = master_row(SOURCE, lunch="午餐A", dinner="晚餐B")
    source[2] = -1
    other = master_row("2026-09-29", owner="another-owner")
    other[6] = 2
    adapter, book, _schedule, master = make_adapter(master_rows=[
        source, master_row("2026-09-28"), other,
    ])
    plan = adapter.plan_pair_reschedule(
        workbook_id=BOOK, worksheet_id=101, owner_user_id=OWNER,
        source_date=SOURCE, target_date="2026-09-28", schedule_rows=after_rows(),
    )
    adapter.apply_pair_reschedule(plan)
    assert adapter.read_pair_reschedule(plan) == plan.expected_readback
    assert master.rows[1][2] == -1
    assert master.rows[3] == other
    assert len(book.batch_calls) == 1
