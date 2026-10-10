from __future__ import annotations

from datetime import date, timedelta

import pytest

from customer_reschedule_liff import (
    allowed_target_dates,
    submit_pending_admin_request,
    validate_target_date,
)


EXPIRY = "2026-10-31"


def test_window_is_expiry_through_expiry_plus_30_inclusive():
    dates = allowed_target_dates(EXPIRY)
    assert dates[0] == EXPIRY
    assert dates[-1] == "2026-11-30"
    assert len(dates) == 31


def test_expiry_plus_30_is_allowed():
    validate_target_date(EXPIRY, "2026-11-30", set())


def test_expiry_plus_31_is_rejected():
    with pytest.raises(ValueError, match="30 days"):
        validate_target_date(EXPIRY, "2026-12-01", set())


def test_before_expiry_is_rejected():
    with pytest.raises(ValueError, match="on or after"):
        validate_target_date(EXPIRY, "2026-10-30", set())


def test_occupied_target_is_rejected():
    with pytest.raises(ValueError, match="already"):
        validate_target_date(EXPIRY, "2026-11-01", {"2026-11-01"})


def test_malformed_dates_are_rejected():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        allowed_target_dates("2026-02-30")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_target_date(EXPIRY, "2026/11/01", set())


def test_validation_does_not_write_or_call_external_services():
    calls = []
    validate_target_date(EXPIRY, "2026-11-01", set())
    assert calls == []


def test_submit_delegates_pending_admin_and_replays_by_request_id():
    calls = []

    def fake_submit(**payload):
        calls.append(payload)
        return payload

    first = submit_pending_admin_request(
        fake_submit,
        owner_id="test-owner",
        order_id=42,
        source_date="2026-10-20",
        target_date="2026-11-01",
        request_id="req-1",
    )
    second = submit_pending_admin_request(
        fake_submit,
        owner_id="test-owner",
        order_id=42,
        source_date="2026-10-20",
        target_date="2026-11-01",
        request_id="req-1",
    )
    assert first == second
    assert len(calls) == 2
    assert all(call["status"] == "pending_admin" for call in calls)


def test_submit_requires_verified_owner_and_order():
    with pytest.raises(ValueError, match="owner_id"):
        submit_pending_admin_request(
            lambda **payload: payload,
            owner_id="",
            order_id=42,
            source_date="2026-10-20",
            target_date="2026-11-01",
            request_id="req-1",
        )
    with pytest.raises(ValueError, match="order_id"):
        submit_pending_admin_request(
            lambda **payload: payload,
            owner_id="test-owner",
            order_id=0,
            source_date="2026-10-20",
            target_date="2026-11-01",
            request_id="req-1",
        )
