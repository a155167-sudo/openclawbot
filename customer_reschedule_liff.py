"""Customer-facing policy boundary for the staging meal-reschedule LIFF.

This module deliberately stops at creating a pending-admin request. Approval and
Google Sheets writes remain in the existing admin-only service boundary.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Any

MAX_FORWARD_DAYS = 30


def _parse_iso(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("date must be an ISO string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD format")
    return parsed


def allowed_target_dates(expiry_date: str) -> list[str]:
    """Return the inclusive customer reschedule window."""
    expiry = _parse_iso(expiry_date)
    return [
        (expiry + timedelta(days=offset)).isoformat()
        for offset in range(MAX_FORWARD_DAYS + 1)
    ]


def validate_target_date(
    expiry_date: str,
    target_date: str,
    occupied_dates: set[str] | frozenset[str],
) -> None:
    """Validate a target without mutating storage or calling external services."""
    expiry = _parse_iso(expiry_date)
    target = _parse_iso(target_date)
    if target < expiry:
        raise ValueError("target date must be on or after package expiry")
    if target > expiry + timedelta(days=MAX_FORWARD_DAYS):
        raise ValueError("target date is more than 30 days after package expiry")
    if target_date in occupied_dates:
        raise ValueError("target date already has a meal plan")


def submit_pending_admin_request(
    submit_fn: Callable[..., Any],
    *,
    owner_id: str,
    order_id: int | str,
    source_date: str,
    target_date: str,
    request_id: str,
) -> Any:
    """Delegate one owner-bound request; never approves or writes Sheets.

    ``submit_fn`` is injected by the server boundary, where owner/order context
    has already been verified. The adapter does not accept a client UID or
    perform authorization itself.
    """
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("verified owner_id is required")
    if order_id in (None, "", 0):
        raise ValueError("verified order_id is required")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id is required")
    if not isinstance(source_date, str) or not isinstance(target_date, str):
        raise ValueError("source_date and target_date are required")
    return submit_fn(
        owner_id=owner_id,
        order_id=order_id,
        source_date=source_date,
        target_date=target_date,
        request_id=request_id,
        status="pending_admin",
    )
