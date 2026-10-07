"""Offline, staging-only cutover for a confirmed empty printer-consumer inventory.

The parent process supplies runtime identity and operator evidence.  This module
does not inspect the environment, contact external services, or start itself.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from printer_dispatch_claims import (
    PrinterClaimConflict,
    activate_v2_capability,
    ensure_printer_claim_schema,
)


class CutoverRejected(ValueError):
    """The supplied scope or durable state cannot authorize this cutover."""


_PROJECT_ID = "1220dbe3-b2ea-4119-b21b-0ccdb86a4286"
_SERVICE_ID = "a7fa088c-1fe6-4aa9-b9d7-1b5858691bcd"
_STORE_ID = "nanjing"
_SCOPE = "staging-only/no-device"
_KIND = "operator_confirmed_empty_consumer_inventory"
_CONFIRMATION = "沒有，只有我用 LINE 測試，不接出單設備"
_BASIS = "operator-confirmed-empty-inventory"
_EVIDENCE_KEYS = frozenset({
    "kind", "confirmed", "consumers", "cache_inventory", "confirmation_text",
    "project_id", "service_id", "workbook_id", "store_id", "operation_id",
    "confirmed_at",
})


def _nonempty_string(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _aware_iso_seconds(value: Any, field: str) -> str:
    if type(value) is not str:
        raise CutoverRejected(f"{field} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CutoverRejected(f"{field} must be an aware ISO timestamp") from exc
    if (parsed.tzinfo is None or parsed.utcoffset() is None
            or parsed.isoformat(timespec="seconds") != value):
        raise CutoverRejected(f"{field} must be canonical aware ISO seconds")
    return value


def _validate_input(runtime: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    if not isinstance(runtime, Mapping) or not isinstance(evidence, Mapping):
        raise CutoverRejected("runtime and evidence must be mappings")
    expected_runtime = {
        "app_env": "staging", "project_id": _PROJECT_ID,
        "service_id": _SERVICE_ID, "store_id": _STORE_ID,
    }
    for key, expected in expected_runtime.items():
        if type(runtime.get(key)) is not str or runtime[key] != expected:
            raise CutoverRejected(f"runtime {key} is outside the authorized scope")
    if not _nonempty_string(runtime.get("workbook_id")):
        raise CutoverRejected("runtime workbook_id is required")
    if set(evidence) != _EVIDENCE_KEYS:
        raise CutoverRejected("evidence fields are incomplete or unexpected")
    if evidence["kind"] != _KIND or type(evidence["kind"]) is not str:
        raise CutoverRejected("evidence kind is invalid")
    if evidence["confirmed"] is not True:
        raise CutoverRejected("operator confirmation is required")
    if type(evidence["consumers"]) is not list or evidence["consumers"] != []:
        raise CutoverRejected("consumer inventory must be explicitly empty")
    if type(evidence["cache_inventory"]) is not list or evidence["cache_inventory"] != []:
        raise CutoverRejected("cache inventory must be explicitly empty")
    if type(evidence["confirmation_text"]) is not str or evidence["confirmation_text"] != _CONFIRMATION:
        raise CutoverRejected("operator confirmation text does not match")
    for key in ("project_id", "service_id", "workbook_id", "store_id"):
        if type(evidence[key]) is not str or evidence[key] != runtime[key]:
            raise CutoverRejected(f"evidence {key} does not match runtime")
    if not _nonempty_string(evidence["operation_id"]):
        raise CutoverRejected("operation_id is required")
    _aware_iso_seconds(evidence["confirmed_at"], "confirmed_at")
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def build_plan(
    conn: sqlite3.Connection, *, runtime: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Read durable state and return a safe plan without writing to SQLite."""
    if conn.in_transaction:
        raise CutoverRejected("caller transaction is not permitted")
    digest = _validate_input(runtime, evidence)
    if not _table_exists(conn, "dispatch_authority_version_bindings"):
        raise CutoverRejected("dispatch authority bindings are absent")
    if not _table_exists(conn, "reschedule_dispatch_operations"):
        raise CutoverRejected("reschedule operations schema is absent")
    if conn.execute(
        "SELECT 1 FROM dispatch_authority_version_bindings LIMIT 1"
    ).fetchone() is None:
        raise CutoverRejected("dispatch authority has no bindings")
    if conn.execute(
        """SELECT 1 FROM dispatch_authority_version_bindings
           WHERE store_id IS NULL OR store_id != ? LIMIT 1""", (_STORE_ID,)
    ).fetchone() is not None:
        raise CutoverRejected("dispatch authority includes another store")
    if conn.execute(
        """SELECT 1 FROM reschedule_dispatch_operations
           WHERE status IN ('pending','sheet_unknown','manual_hold') LIMIT 1"""
    ).fetchone() is not None:
        raise CutoverRejected("reschedule operation is still open")

    generation = 1
    replayed = False
    activated_at = None
    if _table_exists(conn, "printer_dispatch_capability_events"):
        previous = conn.execute(
            """SELECT store_id,admin_scope,state,generation,cache_cleared,
                      all_consumers_confirmed,evidence_digest,created_at
               FROM printer_dispatch_capability_events WHERE operation_id=?""",
            (evidence["operation_id"],),
        ).fetchone()
        if previous is not None:
            if (previous[0] != _STORE_ID or previous[1] != _SCOPE
                    or previous[2] != "v2_active" or type(previous[3]) is not int
                    or previous[3] < 1 or previous[4] != 1 or previous[5] != 1
                    or previous[6] != digest):
                raise CutoverRejected("operation_id replay differs from durable evidence or scope")
            generation = previous[3]
            replayed = True
            activated_at = previous[7]
        else:
            latest = conn.execute(
                """SELECT MAX(generation) FROM printer_dispatch_capability_events
                   WHERE store_id=?""", (_STORE_ID,)
            ).fetchone()[0]
            if latest is not None:
                generation = latest + 1

    return {
        "operation_id": evidence["operation_id"],
        "project_id": _PROJECT_ID,
        "service_id": _SERVICE_ID,
        "workbook_id": runtime["workbook_id"],
        "store_id": _STORE_ID,
        "admin_scope": _SCOPE,
        "generation": generation,
        "evidence_digest": digest,
        "evidence_kind": _KIND,
        "confirmed_at": evidence["confirmed_at"],
        "confirmation_text": _CONFIRMATION,
        "basis": _BASIS,
        "consumer_count": 0,
        "cache_inventory_count": 0,
        "cache_cleared": True,
        "all_consumers_confirmed": True,
        "physical_device_verified": False,
        "replayed": replayed,
        "activated_at": activated_at,
    }


def apply_plan(
    conn: sqlite3.Connection, *, runtime: Mapping[str, Any],
    evidence: Mapping[str, Any], now: str,
) -> dict[str, Any]:
    """Activate once through the existing append-only capability API."""
    plan = build_plan(conn, runtime=runtime, evidence=evidence)
    when = _aware_iso_seconds(now, "now")
    if plan["replayed"]:
        return plan
    try:
        ensure_printer_claim_schema(conn)
        activate_v2_capability(
            conn, operation_id=plan["operation_id"], store_id=_STORE_ID,
            admin_scope=_SCOPE, generation=plan["generation"],
            cache_cleared=True, all_consumers_confirmed=True,
            evidence_digest=plan["evidence_digest"], now=when,
        )
    except PrinterClaimConflict as exc:
        raise CutoverRejected(str(exc)) from exc
    plan["activated_at"] = when
    return plan
