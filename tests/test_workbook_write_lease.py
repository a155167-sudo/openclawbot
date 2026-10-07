from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from workbook_write_lease import (
    WorkbookLeaseConflict,
    acquire_workbook_lease,
    configure_workbook_writer_capability,
    ensure_workbook_lease_schema,
    release_workbook_lease,
)

NOW = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)

FULL_WRITER_INVENTORY = (
    "pair_reschedule",
    "subscription_formalization",
    "deferred_meal",
    "meal_swap",
    "master_api_mutation",
    "garmin_sheet",
    "customer_list",
    "training_assignment",
    "legacy_receive_form",
    "personal_sheet_rebuild",
    "four_week_background",
    "workout_food_tracking",
    "weekly_coach",
    "nutrition_outbox",
    "android_printer_status",
    "apps_script_forms_addons",
    "manual_editors",
    "other_service_accounts",
    "legacy_server_deployments",
)


def connections(tmp_path):
    path = tmp_path / "lease.sqlite3"
    first = sqlite3.connect(path, timeout=0.2)
    second = sqlite3.connect(path, timeout=0.2)
    ensure_workbook_lease_schema(first)
    first.commit()
    return first, second


def enable(conn, *, writers=FULL_WRITER_INVENTORY, external="disabled", evidence="fixture-only"):
    configure_workbook_writer_capability(
        conn, workbook_id="book", controlled_writers=writers,
        external_writer_state=external, cutover_evidence=evidence,
    )
    conn.commit()


def test_two_real_connections_cannot_hold_same_workbook_lease(tmp_path):
    first, second = connections(tmp_path)
    enable(first)
    lease = acquire_workbook_lease(first, workbook_id="book", writer_id="pair_reschedule",
                                   operation_id="op-1", now=NOW, ttl_seconds=30)
    with pytest.raises(WorkbookLeaseConflict, match="already leased"):
        acquire_workbook_lease(second, workbook_id="book", writer_id="pair_reschedule",
                               operation_id="op-2", now=NOW, ttl_seconds=30)
    assert lease.operation_id == "op-1"


def test_expired_lease_is_unknown_and_not_stolen(tmp_path):
    first, second = connections(tmp_path)
    enable(first)
    acquire_workbook_lease(first, workbook_id="book", writer_id="pair_reschedule",
                           operation_id="op-1", now=NOW, ttl_seconds=1)
    with pytest.raises(WorkbookLeaseConflict, match="expired outcome is unknown"):
        acquire_workbook_lease(second, workbook_id="book", writer_id="pair_reschedule",
                               operation_id="op-2", now=NOW + timedelta(seconds=2), ttl_seconds=30)


def test_release_requires_known_final_outcome_and_then_allows_next_writer(tmp_path):
    first, second = connections(tmp_path)
    enable(first)
    lease = acquire_workbook_lease(first, workbook_id="book", writer_id="pair_reschedule",
                                   operation_id="op-1", now=NOW, ttl_seconds=30)
    with pytest.raises(WorkbookLeaseConflict, match="known final outcome"):
        release_workbook_lease(first, lease_token=lease.lease_token,
                               final_outcome="unknown", now=NOW)
    release_workbook_lease(first, lease_token=lease.lease_token,
                           final_outcome="confirmed", now=NOW)
    second_lease = acquire_workbook_lease(second, workbook_id="book", writer_id="pair_reschedule",
                                          operation_id="op-2", now=NOW, ttl_seconds=30)
    assert second_lease.operation_id == "op-2"


@pytest.mark.parametrize("external,evidence,writers", [
    ("unknown", "", ("reschedule",)),
    ("controlled", "", ("reschedule",)),
    ("disabled", "cutover", ("activation",)),
])
def test_no_exclusive_capability_means_no_lease(tmp_path, external, evidence, writers):
    first, _second = connections(tmp_path)
    enable(first, writers=writers, external=external, evidence=evidence)
    with pytest.raises(WorkbookLeaseConflict, match="inventory|capability"):
        acquire_workbook_lease(first, workbook_id="book", writer_id="pair_reschedule",
                               operation_id="op", now=NOW, ttl_seconds=30)


def test_pair_only_capability_cannot_bypass_full_inventory(tmp_path):
    first, _second = connections(tmp_path)
    enable(first, writers=("pair_reschedule",), external="controlled", evidence="fixture-only")

    with pytest.raises(WorkbookLeaseConflict, match="full writer inventory"):
        acquire_workbook_lease(
            first, workbook_id="book", writer_id="pair_reschedule",
            operation_id="pair-only", now=NOW, ttl_seconds=30,
        )


def test_full_fixture_inventory_allows_a_named_writer_only(tmp_path):
    first, _second = connections(tmp_path)
    enable(first, writers=FULL_WRITER_INVENTORY, external="controlled", evidence="fixture-only")

    lease = acquire_workbook_lease(
        first, workbook_id="book", writer_id="pair_reschedule",
        operation_id="full-fixture", now=NOW, ttl_seconds=30,
    )
    assert lease.writer_id == "pair_reschedule"

    release_workbook_lease(
        first, lease_token=lease.lease_token, final_outcome="confirmed", now=NOW,
    )
    with pytest.raises(WorkbookLeaseConflict, match="named in full writer inventory"):
        acquire_workbook_lease(
            first, workbook_id="book", writer_id="caller_claimed_known",
            operation_id="unknown-writer", now=NOW, ttl_seconds=30,
        )