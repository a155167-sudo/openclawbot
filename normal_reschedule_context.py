"""Read the locally confirmed meal version for the normal customer LIFF."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from reschedule_dispatch_versions import exportable_versions


def current_order_schedule_rows(
    conn: sqlite3.Connection, *, order_id: int, owner_user_id: str,
) -> list[tuple[str, list]] | None:
    """(service_date, 14 source columns) view of :func:`current_order_schedule_records`."""
    records = current_order_schedule_records(
        conn, order_id=order_id, owner_user_id=owner_user_id)
    if records is None:
        return None
    return [(row["service_date"], row["source_columns"]) for row in records]


def current_order_schedule_records(
    conn: sqlite3.Connection, *, order_id: int, owner_user_id: str,
) -> list[dict] | None:
    """Return (service_date, 14 source columns) for the order's current schedule.

    The current schedule is the single confirmed reschedule version when one
    exists, otherwise the receipt-backed published dispatch rows.  ``None``
    means the schedule cannot be trusted right now (an open operation, a
    broken chain, missing receipts); callers must not guess.
    """
    has_versions = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='reschedule_dispatch_versions'").fetchone()
    versions = exportable_versions(conn, order_id=order_id) if has_versions else []
    if len(versions) == 1:
        from dispatch_authority_bridge import current_authority_snapshot
        binding = conn.execute('''SELECT store_id FROM dispatch_authority_version_bindings
            WHERE order_id=? LIMIT 1''', (order_id,)).fetchone()
        if not binding:
            return None
        snapshot = current_authority_snapshot(
            conn, version_id=str(versions[0]["version_id"]), order_id=order_id,
            owner_user_id=owner_user_id, store_id=str(binding[0]))
        return [_schedule_record(row) for row in snapshot.rows]
    if versions:
        return None
    if has_versions and conn.execute(
            """SELECT 1 FROM reschedule_dispatch_operations WHERE order_id=?
                 AND status IN ('pending','sheet_unknown','manual_hold','confirmed')
               LIMIT 1""",
            (order_id,)).fetchone():
        # An open/uncertain operation, or a confirmed one whose version cannot
        # be exported: the published baseline may be stale, so report
        # "unknown" rather than old meals.  Rejected-only history keeps the
        # baseline because the Sheet was never changed.
        return None
    from subscription_dispatch_contract import _trusted_publication_rows
    trusted = _trusted_publication_rows(
        conn, 'r.order_id=? AND r.customer_uid=?', (order_id, owner_user_id))
    published = conn.execute('''SELECT count(*) FROM subscription_dispatch_rows
        WHERE order_id=? AND customer_uid=? AND publish_state='published' ''',
        (order_id, owner_user_id)).fetchone()[0]
    if not trusted or len(trusted) != published:
        return None
    return [_schedule_record(row) for row in trusted]


def _schedule_record(row) -> dict:
    return {"service_date": str(row["service_date"]),
            "source_columns": list(row["source_columns"]),
            "dispatch_row_id": str(row["dispatch_row_id"])}


def normal_order_menu_context(
    conn: sqlite3.Connection, *, order_id: int, owner_user_id: str, today: date,
) -> dict:
    """Only receipt-backed current rows may become normal date choices."""
    empty = {"order_id": order_id, "authoritative": True,
             "source_dates": [], "occupied_dates": []}
    try:
        rows = current_order_schedule_rows(
            conn, order_id=order_id, owner_user_id=owner_user_id)
        if rows is None:
            return empty
        dates, occupied, seen = [], [], set()
        for day, columns in rows:
            if day in seen or len(columns) != 14 or str(columns[0]).replace('/', '-') != day:
                return empty
            seen.add(day)
            lunch, dinner = str(columns[2] or '').strip(), str(columns[5] or '').strip()
            if lunch or dinner:
                occupied.append(day)
            if day >= today.isoformat() and lunch not in ('', '無') and dinner not in ('', '無'):
                dates.append({'date': day, 'label': day + ' 午餐／晚餐', 'meals': [
                    {'slot': '午餐', 'name': lunch}, {'slot': '晚餐', 'name': dinner}]})
        return {'order_id': order_id, 'authoritative': True,
                'source_dates': sorted(dates, key=lambda item: item['date']),
                'occupied_dates': sorted(occupied)}
    except Exception:
        return empty


def confirmed_reschedule_menu_context(
    conn: sqlite3.Connection, *, order_id: int, owner_user_id: str, today: date,
) -> dict | None:
    """Return None only when this order has no reschedule history.

    An open or uncertain operation returns an empty authoritative context so an
    old health-profile summary cannot be presented as the current schedule.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reschedule_dispatch_operations'"
    ).fetchone()
    if not exists:
        return None
    history = conn.execute(
        "SELECT 1 FROM reschedule_dispatch_operations WHERE order_id=? LIMIT 1",
        (order_id,),
    ).fetchone()
    if not history:
        return None
    empty = {"order_id": order_id, "authoritative": True,
             "source_dates": [], "occupied_dates": []}
    current = exportable_versions(conn, order_id=order_id)
    if len(current) != 1:
        return empty
    version = current[0]
    if str(version["owner_user_id"]) != owner_user_id:
        return empty
    try:
        payload = json.loads(str(version["payload_json"]))
        if (payload.get("authority") != "pair_reschedule_snapshot/v1"
                or payload.get("order_id") != order_id
                or payload.get("owner_user_id") != owner_user_id
                or not isinstance(payload.get("rows"), list)):
            return empty
        dates = []
        occupied = []
        seen = set()
        for row in payload["rows"]:
            day = str(row["service_date"])
            if date.fromisoformat(day).isoformat() != day or day in seen:
                return empty
            seen.add(day)
            columns = row["columns"]
            if not isinstance(columns, list) or len(columns) != 17 or str(columns[0]).replace("/", "-") != day:
                return empty
            lunch, dinner = str(columns[2] or "").strip(), str(columns[5] or "").strip()
            if not lunch and not dinner:
                continue
            occupied.append(day)
            if day >= today.isoformat() and lunch and dinner:
                dates.append({"date": day, "label": "午餐／晚餐", "meals": [
                    {"slot": "午餐", "name": lunch}, {"slot": "晚餐", "name": dinner},
                ]})
        return {"order_id": order_id, "authoritative": True,
                "source_dates": sorted(dates, key=lambda item: item["date"]),
                "occupied_dates": sorted(occupied)}
    except (KeyError, TypeError, ValueError, AttributeError):
        return empty
