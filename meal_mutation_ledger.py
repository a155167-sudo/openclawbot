"""Isolated durable reservation ledger for meal spreadsheet mutations.

This module deliberately has no server, Google Sheets, food-ledger, or usage-quota
imports.  It owns only its additive SQLite component and short local transactions.
"""

from __future__ import annotations

import datetime as _datetime
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Literal, Mapping, Sequence
import uuid

_COMPONENT = "meal_mutation"
_VERSION = 1
_PREFIX = "meal_mutation_"

_SCHEMA_STATEMENTS = (
    """CREATE TABLE meal_mutation_schema_versions (
      component TEXT PRIMARY KEY NOT NULL CHECK(component='meal_mutation'),
      version INTEGER NOT NULL CHECK(version=1),
      applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE meal_mutation_operations (
      operation_id TEXT PRIMARY KEY NOT NULL,
      event_id TEXT NOT NULL,
      owner_user_id TEXT NOT NULL,
      purpose TEXT NOT NULL CHECK(purpose IN ('swap','defer')),
      request_id TEXT NOT NULL DEFAULT '',
      payload_json TEXT NOT NULL,
      payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
      spreadsheet_id TEXT NOT NULL,
      worksheet_id INTEGER NOT NULL,
      worksheet_name TEXT NOT NULL,
      cells_json TEXT NOT NULL,
      before_json TEXT NOT NULL,
      after_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN
        ('reserved','outcome_unknown','completed','rejected','manual_not_applied')),
      claim_token TEXT NOT NULL,
      result_json TEXT NOT NULL DEFAULT '{}',
      unknown_reason TEXT NOT NULL DEFAULT '',
      resolution_note TEXT NOT NULL DEFAULT '',
      resolved_by TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      completed_at TEXT NOT NULL DEFAULT '',
      CHECK((purpose='swap' AND request_id='') OR
            (purpose='defer' AND request_id<>''))
    )""",
    """CREATE UNIQUE INDEX meal_mutation_operations_event_id
       ON meal_mutation_operations(event_id)""",
    """CREATE TABLE meal_mutation_resource_locks (
      resource_key TEXT PRIMARY KEY NOT NULL,
      operation_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(operation_id) REFERENCES meal_mutation_operations(operation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE INDEX idx_meal_mutation_locks_operation
       ON meal_mutation_resource_locks(operation_id)""",
    """CREATE TRIGGER meal_mutation_binding_immutable
       BEFORE UPDATE ON meal_mutation_operations
       WHEN NEW.operation_id IS NOT OLD.operation_id
         OR NEW.event_id IS NOT OLD.event_id
         OR NEW.owner_user_id IS NOT OLD.owner_user_id
         OR NEW.purpose IS NOT OLD.purpose
         OR NEW.request_id IS NOT OLD.request_id
         OR NEW.payload_json IS NOT OLD.payload_json
         OR NEW.payload_sha256 IS NOT OLD.payload_sha256
         OR NEW.spreadsheet_id IS NOT OLD.spreadsheet_id
         OR NEW.worksheet_id IS NOT OLD.worksheet_id
         OR NEW.worksheet_name IS NOT OLD.worksheet_name
         OR NEW.cells_json IS NOT OLD.cells_json
         OR NEW.before_json IS NOT OLD.before_json
         OR NEW.after_json IS NOT OLD.after_json
         OR NEW.claim_token IS NOT OLD.claim_token
       BEGIN
         SELECT RAISE(ABORT, 'meal mutation binding is immutable');
       END""",
    """CREATE TRIGGER meal_mutation_no_delete
       BEFORE DELETE ON meal_mutation_operations
       BEGIN
         SELECT RAISE(ABORT, 'meal mutation operation cannot be deleted');
       END""",
    """CREATE TRIGGER meal_mutation_active_lock_no_delete
       BEFORE DELETE ON meal_mutation_resource_locks
       WHEN EXISTS (
         SELECT 1 FROM meal_mutation_operations
         WHERE operation_id=OLD.operation_id
           AND status IN ('reserved','outcome_unknown')
       )
       BEGIN
         SELECT RAISE(ABORT, 'active meal mutation lock cannot be deleted');
       END""",
    """CREATE TRIGGER meal_mutation_lock_immutable
       BEFORE UPDATE ON meal_mutation_resource_locks
       BEGIN
         SELECT RAISE(ABORT, 'meal mutation resource lock is immutable');
       END""",
)

_EXPECTED_NAMES = {
    "meal_mutation_schema_versions",
    "meal_mutation_operations",
    "meal_mutation_operations_event_id",
    "meal_mutation_resource_locks",
    "idx_meal_mutation_locks_operation",
    "meal_mutation_active_lock_no_delete",
    "meal_mutation_binding_immutable",
    "meal_mutation_lock_immutable",
    "meal_mutation_no_delete",
}


def _now_text() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_sql(sql: str | None) -> str:
    if sql is None:
        return ""
    # Do not lowercase: quoted status/error literals are contract-significant.
    return " ".join(sql.split())


def _expected_contract() -> dict[str, tuple[str, str, str]]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        return {
            row[1]: (row[0], row[2], _normalize_sql(row[3]))
            for row in conn.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE (name LIKE ? OR name=? OR "
                "(tbl_name IN ('meal_mutation_operations','meal_mutation_resource_locks',"
                "'meal_mutation_schema_versions') AND type IN ('index','trigger'))) "
                "AND sql IS NOT NULL",
                (_PREFIX + "%", "idx_meal_mutation_locks_operation"),
            )
            if row[1] in _EXPECTED_NAMES
        }
    finally:
        conn.close()


_EXPECTED_CONTRACT = _expected_contract()


def _owned_contract(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    return {
        row[1]: (row[0], row[2], _normalize_sql(row[3]))
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE (name LIKE ? OR name=? OR "
            "(tbl_name IN ('meal_mutation_operations','meal_mutation_resource_locks',"
            "'meal_mutation_schema_versions') AND type IN ('index','trigger'))) "
            "AND sql IS NOT NULL",
            (_PREFIX + "%", "idx_meal_mutation_locks_operation"),
        )
        if not row[1].startswith("sqlite_autoindex_")
    }


def _verify_current_schema(conn: sqlite3.Connection) -> None:
    actual = _owned_contract(conn)
    if actual != _EXPECTED_CONTRACT:
        raise RuntimeError("meal mutation schema contract is malformed or partial")
    rows = conn.execute(
        "SELECT component,version,typeof(version),applied_at "
        "FROM meal_mutation_schema_versions"
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0][0] != _COMPONENT
        or rows[0][1] != _VERSION
        or rows[0][2] != "integer"
        or not isinstance(rows[0][3], str)
        or not rows[0][3]
    ):
        raise RuntimeError("meal mutation schema version contract is malformed")
    if conn.execute("PRAGMA foreign_key_check(meal_mutation_resource_locks)").fetchall():
        raise RuntimeError("meal mutation resource lock foreign key violation")
    _verify_operation_lock_integrity(conn)


def ensure_meal_mutation_schema(conn: sqlite3.Connection) -> None:
    """Install/validate v1 atomically without committing caller-owned work."""

    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("meal mutation schema requires foreign_keys=ON")
    existing = _owned_contract(conn)
    if existing:
        _verify_current_schema(conn)
        return

    savepoint = "meal_mutation_schema_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO meal_mutation_schema_versions(component,version,applied_at) "
            "VALUES (?,?,?)",
            (_COMPONENT, _VERSION, _now_text()),
        )
        _verify_current_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


@dataclass(frozen=True)
class MealMutationBinding:
    operation_id: str
    event_id: str
    owner_user_id: str
    purpose: Literal["swap", "defer"]
    request_id: str
    payload: dict[str, Any]
    spreadsheet_id: str
    worksheet_id: int
    worksheet_name: str
    cells: tuple[dict[str, int], dict[str, int]]
    before: tuple[str, str]
    after: tuple[str, str]


@dataclass(frozen=True)
class ReserveResult:
    kind: str
    operation_id: str = ""
    claim_token: str = ""
    result: Any = None


@dataclass(frozen=True)
class LookupResult:
    kind: str
    operation_id: str = ""
    result: Any = None


@dataclass(frozen=True)
class FinalizeResult:
    """Outcome of a local finalization attempt.

    ``newly_*`` means this call committed the transition.  ``replay_*`` means
    the exact terminal receipt already existed and no local effect was repeated.
    """

    kind: str
    operation_id: str
    result: Any = None


@dataclass(frozen=True)
class ManualReconcileInspection:
    """Owner-bound read-only evidence; the dispatch token is never exposed."""

    operation_id: str
    owner_masked: str
    purpose: str
    request_id: str
    status: str
    payload: dict[str, Any]
    spreadsheet_id: str
    worksheet_id: int
    worksheet_name: str
    cells: tuple[dict[str, int], dict[str, int]]
    before: tuple[str, str]
    after: tuple[str, str]
    unknown_reason: str
    resolution_note: str
    resolved_by: str
    completed_at: str


class MealMutationConflict(RuntimeError):
    """The supplied claim or current persisted lifecycle cannot be finalized."""


@dataclass(frozen=True)
class _PreparedBinding:
    operation_id: str
    event_id: str
    owner_user_id: str
    purpose: str
    request_id: str
    payload_json: str
    payload_sha256: str
    spreadsheet_id: str
    worksheet_id: int
    worksheet_name: str
    cells_json: str
    before_json: str
    after_json: str
    resource_keys: tuple[str, str]


def _required_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if len(value) > 4096:
        raise ValueError(f"{field} is too long")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _prepare_summary_line(value: Any) -> str:
    summary_line = _required_text(value, "summary_line")
    if (
        not summary_line.startswith("\n")
        or "\r" in summary_line
        or summary_line.endswith("\n")
        or any(not line for line in summary_line[1:].split("\n"))
    ):
        raise ValueError(
            "summary_line must be one or more nonempty newline-prefixed complete lines"
        )
    return summary_line


def _summary_contains_complete_record(summary_text: Any, summary_line: str) -> bool:
    if not isinstance(summary_text, str):
        return False
    start = 0
    while True:
        position = summary_text.find(summary_line, start)
        if position < 0:
            return False
        end = position + len(summary_line)
        if end == len(summary_text) or summary_text[end] == "\n":
            return True
        start = position + 1


def _validate_completed_replay_receipt(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    prepared: "_PreparedBinding",
    *,
    summary_line: str,
    approved_by: str,
) -> None:
    """Validate every local surface claimed by a completed operation, read-only."""

    profile = conn.execute(
        "SELECT summary_text FROM health_profile WHERE user_id=?",
        (prepared.owner_user_id,),
    ).fetchone()
    if profile is None or not _summary_contains_complete_record(profile[0], summary_line):
        raise MealMutationConflict("meal mutation completed replay summary differs")
    if prepared.purpose != "defer":
        if approved_by:
            raise ValueError("approved_by is only valid for defer completion")
        return

    request_id = _validated_deferred_request_id(prepared.request_id)
    payload = _decode_canonical_json(prepared.payload_json, "payload_json")
    deferred = conn.execute(
        "SELECT user_id,original_date,original_meal_type,target_date,"
        "target_meal_type,status,approved_at,approved_by "
        "FROM deferred_meals WHERE id=?",
        (request_id,),
    ).fetchone()
    if (
        deferred is None
        or tuple(deferred[0:6])
        != (
            prepared.owner_user_id,
            payload["d1"],
            payload["m1"],
            payload["d2"],
            payload["m2"],
            "completed",
        )
        or deferred[6] != row["completed_at"]
        or deferred[7] != approved_by
    ):
        raise MealMutationConflict("deferred meal replay contract differs")


def _deferred_completed_receipt_from_result(
    result: Any, prepared: "_PreparedBinding"
) -> tuple[str, str]:
    required = {"message", "outcome", "summary_line", "approved_by"}
    if not isinstance(result, dict) or set(result) != required:
        raise RuntimeError("stored deferred completion receipt is invalid")
    payload = _decode_canonical_json(prepared.payload_json, "payload_json")
    expected_message = (
        f"✅ 已將 {payload['d1']}{payload['m1']} 延至 {payload['d2']}{payload['m2']}"
    )
    if result["outcome"] != "completed" or result["message"] != expected_message:
        raise RuntimeError("stored deferred completion receipt is invalid")
    try:
        summary_line = _prepare_summary_line(result["summary_line"])
        approved_by = _required_text(result["approved_by"], "approved_by")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stored deferred completion receipt is invalid") from exc
    return summary_line, approved_by


def _require_terminal_completed_at(value: Any) -> None:
    if not isinstance(value, str):
        raise RuntimeError(
            "meal mutation operation integrity violation: completed_at is not text"
        )
    try:
        parsed = _datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise RuntimeError(
            "meal mutation operation integrity violation: completed_at is not a valid UTC timestamp"
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise RuntimeError(
            "meal mutation operation integrity violation: completed_at is noncanonical"
        )


def _canonical_json(value: Any, field: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be canonical JSON data") from exc


def _prepare_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    required = {"d1", "m1", "d2", "m2"}
    if set(payload) != required:
        raise ValueError("payload must contain exactly d1, m1, d2, and m2")
    for key in sorted(required):
        _required_text(payload[key], f"payload.{key}")
    return _canonical_json(payload, "payload")


def _prepare_binding(binding: MealMutationBinding) -> _PreparedBinding:
    if not isinstance(binding, MealMutationBinding):
        raise TypeError("binding must be MealMutationBinding")
    operation_id = _required_text(binding.operation_id, "operation_id")
    event_id = _required_text(binding.event_id, "event_id")
    owner = _required_text(binding.owner_user_id, "owner_user_id")
    purpose = _required_text(binding.purpose, "purpose")
    if purpose not in {"swap", "defer"}:
        raise ValueError("purpose must be swap or defer")
    request_id = _required_text(binding.request_id, "request_id", allow_empty=True)
    if (purpose == "swap" and request_id) or (purpose == "defer" and not request_id):
        raise ValueError("request_id does not match purpose")
    payload_json = _prepare_payload(binding.payload)
    spreadsheet_id = _required_text(binding.spreadsheet_id, "spreadsheet_id")
    worksheet_name = _required_text(binding.worksheet_name, "worksheet_name")
    if isinstance(binding.worksheet_id, bool) or not isinstance(binding.worksheet_id, int):
        raise TypeError("worksheet_id must be an integer")
    if binding.worksheet_id < 0:
        raise ValueError("worksheet_id must be non-negative")
    if not isinstance(binding.cells, tuple) or len(binding.cells) != 2:
        raise ValueError("cells must be a two-item tuple")
    if not isinstance(binding.before, tuple) or len(binding.before) != 2:
        raise ValueError("before must be a two-item tuple")
    if not isinstance(binding.after, tuple) or len(binding.after) != 2:
        raise ValueError("after must be a two-item tuple")

    triples = []
    for position, cell in enumerate(binding.cells):
        if not isinstance(cell, Mapping) or set(cell) != {"row_idx", "col_idx"}:
            raise ValueError("each cell must contain exactly row_idx and col_idx")
        row, col = cell["row_idx"], cell["col_idx"]
        if isinstance(row, bool) or not isinstance(row, int):
            raise TypeError("row_idx must be an integer")
        if isinstance(col, bool) or not isinstance(col, int):
            raise TypeError("col_idx must be an integer")
        if row <= 0 or col <= 0:
            raise ValueError("cell positions must be positive")
        before = _required_text(binding.before[position], f"before[{position}]", allow_empty=True)
        after = _required_text(binding.after[position], f"after[{position}]", allow_empty=True)
        triples.append(({"row_idx": row, "col_idx": col}, before, after))
    triples.sort(key=lambda item: (item[0]["row_idx"], item[0]["col_idx"]))
    if triples[0][0] == triples[1][0]:
        raise ValueError("cells must be distinct")
    cells = tuple(item[0] for item in triples)
    before = tuple(item[1] for item in triples)
    after = tuple(item[2] for item in triples)
    resource_keys = tuple(
        f"sheet:{spreadsheet_id}:worksheet:{binding.worksheet_id}:r{cell['row_idx']}c{cell['col_idx']}"
        for cell in cells
    )
    return _PreparedBinding(
        operation_id=operation_id,
        event_id=event_id,
        owner_user_id=owner,
        purpose=purpose,
        request_id=request_id,
        payload_json=payload_json,
        payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        spreadsheet_id=spreadsheet_id,
        worksheet_id=binding.worksheet_id,
        worksheet_name=worksheet_name,
        cells_json=_canonical_json(cells, "cells"),
        before_json=_canonical_json(before, "before"),
        after_json=_canonical_json(after, "after"),
        resource_keys=resource_keys,
    )


_BINDING_COLUMNS = (
    "operation_id,event_id,owner_user_id,purpose,request_id,payload_json,"
    "payload_sha256,spreadsheet_id,worksheet_id,worksheet_name,cells_json,"
    "before_json,after_json"
)


def _binding_tuple(prepared: _PreparedBinding) -> tuple[Any, ...]:
    return (
        prepared.operation_id,
        prepared.event_id,
        prepared.owner_user_id,
        prepared.purpose,
        prepared.request_id,
        prepared.payload_json,
        prepared.payload_sha256,
        prepared.spreadsheet_id,
        prepared.worksheet_id,
        prepared.worksheet_name,
        prepared.cells_json,
        prepared.before_json,
        prepared.after_json,
    )


def _decode_canonical_json(text: Any, field: str) -> Any:
    if not isinstance(text, str):
        raise RuntimeError(f"meal mutation operation integrity violation: {field} is not text")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"meal mutation operation integrity violation: {field} is malformed") from exc
    try:
        canonical = _canonical_json(decoded, field)
    except ValueError as exc:
        raise RuntimeError(f"meal mutation operation integrity violation: {field} is invalid") from exc
    if canonical != text:
        raise RuntimeError(f"meal mutation operation integrity violation: {field} is not canonical")
    return decoded


def _prepared_from_persisted_row(row: sqlite3.Row) -> _PreparedBinding:
    payload = _decode_canonical_json(row["payload_json"], "payload_json")
    cells = _decode_canonical_json(row["cells_json"], "cells_json")
    before = _decode_canonical_json(row["before_json"], "before_json")
    after = _decode_canonical_json(row["after_json"], "after_json")
    if not isinstance(payload, dict):
        raise RuntimeError("meal mutation operation integrity violation: payload_json root")
    if not isinstance(cells, list) or not isinstance(before, list) or not isinstance(after, list):
        raise RuntimeError("meal mutation operation integrity violation: snapshot roots")
    try:
        prepared = _prepare_binding(
            MealMutationBinding(
                operation_id=row["operation_id"],
                event_id=row["event_id"],
                owner_user_id=row["owner_user_id"],
                purpose=row["purpose"],
                request_id=row["request_id"],
                payload=payload,
                spreadsheet_id=row["spreadsheet_id"],
                worksheet_id=row["worksheet_id"],
                worksheet_name=row["worksheet_name"],
                cells=tuple(cells),
                before=tuple(before),
                after=tuple(after),
            )
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("meal mutation operation integrity violation: invalid persisted binding") from exc
    actual = tuple(row[column] for column in _BINDING_COLUMNS.split(","))
    if actual != _binding_tuple(prepared):
        raise RuntimeError("meal mutation operation integrity violation: noncanonical persisted binding")
    token = row["claim_token"]
    if (
        not isinstance(token, str)
        or len(token) != 64
        or token != token.lower()
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise RuntimeError("meal mutation operation integrity violation: invalid claim token")
    result = _decode_canonical_json(row["result_json"], "result_json")
    if not isinstance(result, dict):
        raise RuntimeError("meal mutation operation integrity violation: result_json root")
    return prepared


def _validate_status_specific_terminal_receipt(
    conn: sqlite3.Connection, row: sqlite3.Row, prepared: _PreparedBinding
) -> None:
    """Validate persisted manual receipts and their linked projections read-only."""

    status = row["status"]
    result = _decode_canonical_json(row["result_json"], "result_json")
    if not isinstance(result, dict):
        raise RuntimeError("meal mutation operation integrity violation: result_json root")

    if status == "manual_not_applied":
        _unused_summary, expected = _manual_resolution_receipt(
            prepared, "not_applied", row["resolved_by"]
        )
        if row["result_json"] != _canonical_json(expected, "manual result"):
            raise RuntimeError(
                "meal mutation operation integrity violation: manual not-applied receipt"
            )
        return

    if status == "rejected":
        if result.get("outcome") == "manual_not_applied":
            raise RuntimeError(
                "meal mutation operation integrity violation: manual receipt status"
            )
        return

    if status != "completed":
        return

    has_manual_metadata = row["resolution_note"] != "" or row["resolved_by"] != ""
    if has_manual_metadata:
        summary_line, expected = _manual_resolution_receipt(
            prepared, "applied", row["resolved_by"]
        )
        if row["result_json"] != _canonical_json(expected, "manual result"):
            raise RuntimeError(
                "meal mutation operation integrity violation: manual applied receipt"
            )
        _validate_completed_replay_receipt(
            conn,
            row,
            prepared,
            summary_line=summary_line,
            approved_by=row["resolved_by"] if prepared.purpose == "defer" else "",
        )
        return

    # A generated manual result must never become an ordinary terminal merely
    # because its resolution metadata was removed.
    candidate_admin = ""
    if prepared.purpose == "defer" and isinstance(result.get("approved_by"), str):
        candidate_admin = result["approved_by"]
    _manual_summary, manual_expected = _manual_resolution_receipt(
        prepared, "applied", candidate_admin
    )
    if row["result_json"] == _canonical_json(manual_expected, "manual result"):
        raise RuntimeError(
            "meal mutation operation integrity violation: missing manual resolution metadata"
        )
    if result.get("outcome") == "manual_not_applied":
        raise RuntimeError(
            "meal mutation operation integrity violation: manual receipt status"
        )


def _verify_operation_lock_integrity(conn: sqlite3.Connection) -> None:
    """Validate the complete persisted operation/lock graph without repairing it."""

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        operation_rows = conn.execute(
            f"SELECT {_BINDING_COLUMNS},status,claim_token,result_json,completed_at,unknown_reason,"
            "resolution_note,resolved_by FROM meal_mutation_operations ORDER BY operation_id"
        ).fetchall()
        expected_locks: list[tuple[str, str]] = []
        for row in operation_rows:
            prepared = _prepared_from_persisted_row(row)
            if row["status"] == "outcome_unknown" and (
                not isinstance(row["unknown_reason"], str) or not row["unknown_reason"].strip()
            ):
                raise RuntimeError("meal mutation operation integrity violation: unknown_reason")
            if row["status"] in {"reserved", "outcome_unknown"}:
                if row["resolution_note"] != "" or row["resolved_by"] != "":
                    raise RuntimeError(
                        "meal mutation operation integrity violation: premature manual resolution metadata"
                    )
                expected_locks.extend((key, prepared.operation_id) for key in prepared.resource_keys)
            elif row["status"] in {"completed", "rejected"}:
                _require_terminal_completed_at(row["completed_at"])
                has_manual_metadata = row["resolution_note"] != "" or row["resolved_by"] != ""
                if row["status"] == "rejected" and has_manual_metadata:
                    raise RuntimeError(
                        "meal mutation operation integrity violation: rejected manual resolution metadata"
                    )
                if has_manual_metadata and (
                    not isinstance(row["resolution_note"], str)
                    or not row["resolution_note"].strip()
                    or not isinstance(row["resolved_by"], str)
                    or not row["resolved_by"].strip()
                ):
                    raise RuntimeError(
                        "meal mutation operation integrity violation: manual resolution metadata"
                    )
            elif row["status"] == "manual_not_applied":
                _require_terminal_completed_at(row["completed_at"])
                if (
                    not isinstance(row["resolution_note"], str)
                    or not row["resolution_note"].strip()
                    or not isinstance(row["resolved_by"], str)
                    or not row["resolved_by"].strip()
                ):
                    raise RuntimeError(
                        "meal mutation operation integrity violation: manual resolution metadata"
                    )
            else:
                raise RuntimeError("meal mutation operation integrity violation: invalid status")
            if row["status"] in {"completed", "rejected", "manual_not_applied"}:
                _validate_status_specific_terminal_receipt(conn, row, prepared)

        actual_locks = conn.execute(
            "SELECT resource_key,operation_id FROM meal_mutation_resource_locks "
            "ORDER BY resource_key,operation_id"
        ).fetchall()
        actual_pairs = []
        for lock in actual_locks:
            if not isinstance(lock["resource_key"], str) or not isinstance(lock["operation_id"], str):
                raise RuntimeError("meal mutation resource lock integrity violation: invalid type")
            actual_pairs.append((lock["resource_key"], lock["operation_id"]))
        if actual_pairs != sorted(expected_locks):
            raise RuntimeError("meal mutation operation/lock integrity violation")
    finally:
        conn.row_factory = previous_factory


def _decoded_result(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stored meal mutation result is malformed") from exc


def _reserve_replay(
    conn: sqlite3.Connection, row: sqlite3.Row, exact: bool
) -> ReserveResult:
    if not exact:
        return ReserveResult("identity_conflict")
    status = row["status"]
    if status in {"reserved", "outcome_unknown"}:
        return ReserveResult("blocked_unresolved", operation_id=row["operation_id"])
    if status == "completed":
        if row["purpose"] == "defer":
            prepared = _prepared_from_persisted_row(row)
            result = _decode_canonical_json(row["result_json"], "result_json")
            summary_line, approved_by = _deferred_completed_receipt_from_result(
                result, prepared
            )
            _validate_completed_replay_receipt(
                conn,
                row,
                prepared,
                summary_line=summary_line,
                approved_by=approved_by,
            )
        else:
            result = _decoded_result(row["result_json"])
        return ReserveResult(
            "replay_completed", operation_id=row["operation_id"], result=result
        )
    if status in {"rejected", "manual_not_applied"}:
        return ReserveResult(
            "replay_rejected", operation_id=row["operation_id"], result=_decoded_result(row["result_json"])
        )
    raise RuntimeError("stored meal mutation status is invalid")


def meal_mutation_blocks_publication(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    spreadsheet_id: str,
    worksheet_id: int,
    worksheet_name: str,
) -> bool:
    """Return whether an unresolved external mutation owns this publication target."""
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'meal_mutation_%'"
    )}
    if not names:
        return False
    _verify_operation_lock_integrity(conn)
    return conn.execute(
        """SELECT 1 FROM meal_mutation_operations o
              JOIN meal_mutation_resource_locks l ON l.operation_id=o.operation_id
             WHERE o.owner_user_id=? AND o.spreadsheet_id=? AND o.worksheet_id=?
               AND o.worksheet_name=? AND o.status IN ('reserved','outcome_unknown')
             LIMIT 1""",
        (owner_user_id, spreadsheet_id, worksheet_id, worksheet_name),
    ).fetchone() is not None


def reserve_meal_mutation(
    db_path: str | Path,
    binding: MealMutationBinding,
    *,
    publication_slots: Sequence[tuple[object, str]] = (),
) -> ReserveResult:
    """Atomically reserve an immutable operation and both cell resources."""

    prepared = _prepare_binding(binding)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _verify_operation_lock_integrity(conn)
        row = conn.execute(
            f"SELECT {_BINDING_COLUMNS},status,claim_token,result_json,completed_at "
            "FROM meal_mutation_operations WHERE event_id=?",
            (prepared.event_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_BINDING_COLUMNS},status,claim_token,result_json,completed_at "
                "FROM meal_mutation_operations WHERE operation_id=?",
                (prepared.operation_id,),
            ).fetchone()
        if row is not None:
            result = _reserve_replay(
                conn,
                row,
                tuple(row[column] for column in _BINDING_COLUMNS.split(","))
                == _binding_tuple(prepared),
            )
            conn.commit()
            return result
        if publication_slots:
            # Use the dispatch component's exact schema and receipt contract while
            # this BEGIN IMMEDIATE transaction excludes a concurrent publisher.
            from subscription_dispatch_contract import mutation_touches_published_dispatch

            if mutation_touches_published_dispatch(
                conn,
                customer_uid=prepared.owner_user_id,
                workbook_id=prepared.spreadsheet_id,
                worksheet_id=prepared.worksheet_id,
                worksheet_title=prepared.worksheet_name,
                service_slots=publication_slots,
            ):
                conn.commit()
                return ReserveResult("published_blocked")
        placeholders = ",".join("?" for _ in prepared.resource_keys)
        if conn.execute(
            f"SELECT 1 FROM meal_mutation_resource_locks WHERE resource_key IN ({placeholders}) LIMIT 1",
            prepared.resource_keys,
        ).fetchone():
            conn.commit()
            return ReserveResult("resource_blocked")

        claim_token = secrets.token_hex(32)
        now = _now_text()
        conn.execute(
            "INSERT INTO meal_mutation_operations("
            + _BINDING_COLUMNS
            + ",status,claim_token,created_at,updated_at) VALUES ("
            + ",".join("?" for _ in range(17))
            + ")",
            _binding_tuple(prepared) + ("reserved", claim_token, now, now),
        )
        for resource_key in prepared.resource_keys:
            conn.execute(
                "INSERT INTO meal_mutation_resource_locks(resource_key,operation_id,created_at) VALUES (?,?,?)",
                (resource_key, prepared.operation_id, now),
            )
        conn.commit()
        return ReserveResult("claimed", operation_id=prepared.operation_id, claim_token=claim_token)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def lookup_meal_mutation_for_request(
    db_path: str | Path,
    *,
    event_id: str,
    owner_user_id: str,
    purpose: str,
    request_id: str,
    payload: dict[str, Any],
) -> LookupResult:
    """Read replay state using stable request inputs, before fresh Sheet reads."""

    event_id = _required_text(event_id, "event_id")
    owner_user_id = _required_text(owner_user_id, "owner_user_id")
    purpose = _required_text(purpose, "purpose")
    if purpose not in {"swap", "defer"}:
        raise ValueError("purpose must be swap or defer")
    request_id = _required_text(request_id, "request_id", allow_empty=True)
    if (purpose == "swap" and request_id) or (purpose == "defer" and not request_id):
        raise ValueError("request_id does not match purpose")
    payload_json = _prepare_payload(payload)

    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        _verify_operation_lock_integrity(conn)
        row = conn.execute(
            f"SELECT {_BINDING_COLUMNS},status,claim_token,result_json,completed_at "
            "FROM meal_mutation_operations WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            return LookupResult("not_found")
        exact = (
            row["owner_user_id"] == owner_user_id
            and row["purpose"] == purpose
            and row["request_id"] == request_id
            and row["payload_json"] == payload_json
        )
        if not exact:
            return LookupResult("identity_conflict")
        if row["status"] in {"reserved", "outcome_unknown"}:
            return LookupResult("blocked_unresolved", operation_id=row["operation_id"])
        if row["status"] in {"completed", "rejected", "manual_not_applied"}:
            result = _decode_canonical_json(row["result_json"], "result_json")
            if row["status"] == "completed" and row["purpose"] == "defer":
                prepared = _prepared_from_persisted_row(row)
                summary_line, approved_by = _deferred_completed_receipt_from_result(
                    result, prepared
                )
                _validate_completed_replay_receipt(
                    conn,
                    row,
                    prepared,
                    summary_line=summary_line,
                    approved_by=approved_by,
                )
            return LookupResult(
                "stored_result",
                operation_id=row["operation_id"],
                result=result,
            )
        raise RuntimeError("stored meal mutation status is invalid")
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def meal_mutation_resources_blocked(
    db_path: str | Path,
    *,
    spreadsheet_id: str,
    worksheet_id: int,
    cells: tuple[dict[str, int], dict[str, int]],
) -> bool:
    """Read whether either exact cell has a durable unresolved-operation lock."""
    spreadsheet_id = _required_text(spreadsheet_id, "spreadsheet_id")
    if isinstance(worksheet_id, bool) or not isinstance(worksheet_id, int) or worksheet_id <= 0:
        raise ValueError("worksheet_id must be a positive integer")
    if not isinstance(cells, tuple) or len(cells) != 2:
        raise ValueError("cells must contain exactly two resources")
    keys = []
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {"row_idx", "col_idx"}:
            raise ValueError("cell must contain row_idx and col_idx")
        row_idx, col_idx = cell["row_idx"], cell["col_idx"]
        if (
            isinstance(row_idx, bool) or isinstance(col_idx, bool)
            or not isinstance(row_idx, int) or not isinstance(col_idx, int)
            or row_idx <= 0 or col_idx <= 0
        ):
            raise ValueError("cell coordinates must be positive integers")
        keys.append(f"sheet:{spreadsheet_id}:worksheet:{worksheet_id}:r{row_idx}c{col_idx}")
    if len(set(keys)) != 2:
        raise ValueError("cell resources must be distinct")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("BEGIN")
        _verify_operation_lock_integrity(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM meal_mutation_resource_locks WHERE resource_key IN (?,?)",
            tuple(keys),
        ).fetchone()[0]
        return bool(count)
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _prepare_claim_inputs(
    operation_id: Any, claim_token: Any, owner_user_id: Any
) -> tuple[str, str, str]:
    operation_id = _required_text(operation_id, "operation_id")
    owner_user_id = _required_text(owner_user_id, "owner_user_id")
    claim_token = _required_text(claim_token, "claim_token")
    if (
        len(claim_token) != 64
        or claim_token != claim_token.lower()
        or any(character not in "0123456789abcdef" for character in claim_token)
    ):
        raise ValueError("claim_token must be 64 lowercase hexadecimal characters")
    return operation_id, claim_token, owner_user_id


def _prepare_terminal_result(result: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(result, dict):
        raise TypeError("result must be a dict")
    encoded = _canonical_json(result, "result")
    decoded = _decode_canonical_json(encoded, "result")
    if not isinstance(decoded, dict):  # defensive parity with persisted receipt validation
        raise TypeError("result must be a dict")
    return decoded, encoded


def _operation_for_finalization(
    conn: sqlite3.Connection, operation_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_BINDING_COLUMNS},status,claim_token,result_json,unknown_reason,"
        "completed_at,resolution_note,resolved_by FROM meal_mutation_operations "
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone()


def _require_claim(row: sqlite3.Row | None, claim_token: str, owner_user_id: str) -> None:
    # One deliberately non-disclosing error covers absent, foreign-owner, and
    # stale-token claims.
    if (
        row is None
        or row["owner_user_id"] != owner_user_id
        or row["claim_token"] != claim_token
    ):
        raise MealMutationConflict("meal mutation claim does not match")


def _delete_exact_operation_locks(conn: sqlite3.Connection, operation_id: str) -> None:
    deleted = conn.execute(
        "DELETE FROM meal_mutation_resource_locks WHERE operation_id=?",
        (operation_id,),
    ).rowcount
    if deleted != 2:
        raise RuntimeError("meal mutation terminal cleanup did not delete exactly two locks")


def _rollback_on_failure(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def get_claimed_meal_mutation_snapshot(
    db_path: str | Path,
    operation_id: str,
    claim_token: str,
    owner_user_id: str,
) -> MealMutationBinding:
    """Return the immutable reserved snapshot to its exact claim holder only.

    The claim token is an input fence and is intentionally absent from the
    returned value.
    """

    operation_id, claim_token, owner_user_id = _prepare_claim_inputs(
        operation_id, claim_token, owner_user_id
    )
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        _verify_operation_lock_integrity(conn)
        row = _operation_for_finalization(conn, operation_id)
        _require_claim(row, claim_token, owner_user_id)
        assert row is not None
        if row["status"] != "reserved":
            raise MealMutationConflict("meal mutation claim is not reserved")
        prepared = _prepared_from_persisted_row(row)
        return MealMutationBinding(
            operation_id=prepared.operation_id,
            event_id=prepared.event_id,
            owner_user_id=prepared.owner_user_id,
            purpose=prepared.purpose,  # type: ignore[arg-type]
            request_id=prepared.request_id,
            payload=_decode_canonical_json(prepared.payload_json, "payload_json"),
            spreadsheet_id=prepared.spreadsheet_id,
            worksheet_id=prepared.worksheet_id,
            worksheet_name=prepared.worksheet_name,
            cells=tuple(_decode_canonical_json(prepared.cells_json, "cells_json")),  # type: ignore[arg-type]
            before=tuple(_decode_canonical_json(prepared.before_json, "before_json")),  # type: ignore[arg-type]
            after=tuple(_decode_canonical_json(prepared.after_json, "after_json")),  # type: ignore[arg-type]
        )
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def mark_meal_mutation_rejected(
    db_path: str | Path,
    operation_id: str,
    claim_token: str,
    owner_user_id: str,
    result: dict[str, Any],
) -> FinalizeResult:
    """Record confirmed remote rejection and release both resources atomically."""

    operation_id, claim_token, owner_user_id = _prepare_claim_inputs(
        operation_id, claim_token, owner_user_id
    )
    decoded_result, result_json = _prepare_terminal_result(result)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _verify_operation_lock_integrity(conn)
        row = _operation_for_finalization(conn, operation_id)
        _require_claim(row, claim_token, owner_user_id)
        assert row is not None
        if row["status"] == "rejected":
            if row["result_json"] != result_json:
                raise MealMutationConflict("meal mutation rejected replay contract differs")
            conn.commit()
            return FinalizeResult("replay_rejected", operation_id, decoded_result)
        if row["status"] != "reserved":
            raise MealMutationConflict("meal mutation terminal state cannot be overwritten")
        now = _now_text()
        changed = conn.execute(
            "UPDATE meal_mutation_operations SET status='rejected',result_json=?,"
            "updated_at=?,completed_at=? WHERE operation_id=? AND owner_user_id=? "
            "AND claim_token=? AND status='reserved'",
            (result_json, now, now, operation_id, owner_user_id, claim_token),
        ).rowcount
        if changed != 1:
            raise MealMutationConflict("meal mutation rejection claim was lost")
        _delete_exact_operation_locks(conn, operation_id)
        _verify_operation_lock_integrity(conn)
        conn.commit()
        return FinalizeResult("newly_rejected", operation_id, decoded_result)
    except BaseException:
        _rollback_on_failure(conn)
        raise
    finally:
        conn.close()


def mark_meal_mutation_unknown(
    db_path: str | Path,
    operation_id: str,
    claim_token: str,
    owner_user_id: str,
    reason: str,
) -> FinalizeResult:
    """Persist an ambiguous remote outcome without releasing resend fences."""

    operation_id, claim_token, owner_user_id = _prepare_claim_inputs(
        operation_id, claim_token, owner_user_id
    )
    reason = _required_text(reason, "reason")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _verify_operation_lock_integrity(conn)
        row = _operation_for_finalization(conn, operation_id)
        _require_claim(row, claim_token, owner_user_id)
        assert row is not None
        if row["status"] == "outcome_unknown":
            # The first observed reason is evidence.  Retries may acknowledge it
            # but can never replace it with a later worker's interpretation.
            conn.commit()
            return FinalizeResult(
                "replay_unknown", operation_id, {"reason": row["unknown_reason"]}
            )
        if row["status"] != "reserved":
            raise MealMutationConflict("meal mutation terminal state cannot be overwritten")
        now = _now_text()
        changed = conn.execute(
            "UPDATE meal_mutation_operations SET status='outcome_unknown',unknown_reason=?,"
            "updated_at=? WHERE operation_id=? AND owner_user_id=? AND claim_token=? "
            "AND status='reserved'",
            (reason, now, operation_id, owner_user_id, claim_token),
        ).rowcount
        if changed != 1:
            raise MealMutationConflict("meal mutation unknown-outcome claim was lost")
        _verify_operation_lock_integrity(conn)
        conn.commit()
        return FinalizeResult("newly_unknown", operation_id, {"reason": reason})
    except BaseException:
        _rollback_on_failure(conn)
        raise
    finally:
        conn.close()


def _validated_deferred_request_id(request_id: str) -> int:
    if not request_id.isascii() or not request_id.isdecimal():
        raise RuntimeError("persisted deferred request_id is invalid")
    value = int(request_id)
    if value <= 0 or str(value) != request_id:
        raise RuntimeError("persisted deferred request_id is noncanonical")
    return value


def _complete_deferred_request(
    conn: sqlite3.Connection,
    prepared: _PreparedBinding,
    owner_user_id: str,
    approved_by: str,
    completed_at: str,
) -> None:
    if not approved_by:
        raise ValueError("approved_by is required for defer completion")
    request_id = _validated_deferred_request_id(prepared.request_id)
    payload = _decode_canonical_json(prepared.payload_json, "payload_json")
    row = conn.execute(
        "SELECT user_id,original_date,original_meal_type,target_date,target_meal_type,status "
        "FROM deferred_meals WHERE id=?",
        (request_id,),
    ).fetchone()
    expected = (
        owner_user_id,
        payload["d1"],
        payload["m1"],
        payload["d2"],
        payload["m2"],
        "pending",
    )
    if row is None or tuple(row) != expected:
        raise MealMutationConflict("deferred meal request contract differs")
    changed = conn.execute(
        "UPDATE deferred_meals SET status='completed',approved_at=?,approved_by=? "
        "WHERE id=? AND user_id=? AND original_date=? AND original_meal_type=? "
        "AND target_date=? AND target_meal_type=? AND status='pending'",
        (
            completed_at,
            approved_by,
            request_id,
            owner_user_id,
            payload["d1"],
            payload["m1"],
            payload["d2"],
            payload["m2"],
        ),
    ).rowcount
    if changed != 1:
        raise MealMutationConflict("deferred meal request completion claim was lost")


def complete_meal_mutation(
    db_path: str | Path,
    operation_id: str,
    claim_token: str,
    owner_user_id: str,
    *,
    summary_line: str,
    result: dict[str, Any],
    approved_by: str = "",
) -> FinalizeResult:
    """Finalize confirmed remote acceptance and every local effect atomically.

    This function performs no provider I/O.  The coordinator must call it only
    after the remote atomic batch has explicitly reported acceptance.
    """

    operation_id, claim_token, owner_user_id = _prepare_claim_inputs(
        operation_id, claim_token, owner_user_id
    )
    summary_line = _prepare_summary_line(summary_line)
    approved_by = _required_text(approved_by, "approved_by", allow_empty=True)
    decoded_result, result_json = _prepare_terminal_result(result)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _verify_operation_lock_integrity(conn)
        row = _operation_for_finalization(conn, operation_id)
        _require_claim(row, claim_token, owner_user_id)
        assert row is not None
        prepared = _prepared_from_persisted_row(row)
        if row["status"] == "completed":
            if row["result_json"] != result_json:
                raise MealMutationConflict("meal mutation completed replay result differs")
            _validate_completed_replay_receipt(
                conn,
                row,
                prepared,
                summary_line=summary_line,
                approved_by=approved_by,
            )
            conn.commit()
            return FinalizeResult("replay_completed", operation_id, decoded_result)
        if row["status"] != "reserved":
            raise MealMutationConflict("meal mutation terminal state cannot be overwritten")
        if prepared.purpose == "swap" and approved_by:
            raise ValueError("approved_by is only valid for defer completion")

        now = _now_text()
        appended = conn.execute(
            "UPDATE health_profile SET summary_text=COALESCE(summary_text,'') || ? "
            "WHERE user_id=?",
            (summary_line, owner_user_id),
        ).rowcount
        if appended != 1:
            raise MealMutationConflict("health profile for meal mutation is missing")
        if prepared.purpose == "defer":
            _complete_deferred_request(
                conn, prepared, owner_user_id, approved_by, now
            )
        changed = conn.execute(
            "UPDATE meal_mutation_operations SET status='completed',result_json=?,"
            "updated_at=?,completed_at=? WHERE operation_id=? AND owner_user_id=? "
            "AND claim_token=? AND status='reserved'",
            (result_json, now, now, operation_id, owner_user_id, claim_token),
        ).rowcount
        if changed != 1:
            raise MealMutationConflict("meal mutation completion claim was lost")
        _delete_exact_operation_locks(conn, operation_id)
        _verify_operation_lock_integrity(conn)
        conn.commit()
        return FinalizeResult("newly_completed", operation_id, decoded_result)
    except BaseException:
        _rollback_on_failure(conn)
        raise
    finally:
        conn.close()


def _masked_owner(owner_user_id: str) -> str:
    if len(owner_user_id) <= 4:
        return "*" * len(owner_user_id)
    return owner_user_id[:2] + "***" + owner_user_id[-2:]


def inspect_meal_mutation_for_manual_reconcile(
    db_path: str | Path, *, operation_id: str, owner_user_id: str
) -> ManualReconcileInspection:
    """Return immutable owner-bound evidence without exposing the claim token."""

    operation_id = _required_text(operation_id, "operation_id")
    owner_user_id = _required_text(owner_user_id, "owner_user_id")
    if operation_id != operation_id.strip() or owner_user_id != owner_user_id.strip():
        raise ValueError("manual inspection identifiers must be canonical")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        _verify_operation_lock_integrity(conn)
        row = _operation_for_finalization(conn, operation_id)
        if row is None or row["owner_user_id"] != owner_user_id:
            raise MealMutationConflict("meal mutation inspection binding does not match")
        prepared = _prepared_from_persisted_row(row)
        return ManualReconcileInspection(
            operation_id=prepared.operation_id,
            owner_masked=_masked_owner(prepared.owner_user_id),
            purpose=prepared.purpose,
            request_id=prepared.request_id,
            status=row["status"],
            payload=_decode_canonical_json(prepared.payload_json, "payload_json"),
            spreadsheet_id=prepared.spreadsheet_id,
            worksheet_id=prepared.worksheet_id,
            worksheet_name=prepared.worksheet_name,
            cells=tuple(_decode_canonical_json(prepared.cells_json, "cells_json")),  # type: ignore[arg-type]
            before=tuple(_decode_canonical_json(prepared.before_json, "before_json")),  # type: ignore[arg-type]
            after=tuple(_decode_canonical_json(prepared.after_json, "after_json")),  # type: ignore[arg-type]
            unknown_reason=row["unknown_reason"],
            resolution_note=row["resolution_note"],
            resolved_by=row["resolved_by"],
            completed_at=row["completed_at"],
        )
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _manual_resolution_receipt(
    prepared: _PreparedBinding, disposition: str, admin_uid: str
) -> tuple[str, dict[str, Any]]:
    payload = _decode_canonical_json(prepared.payload_json, "payload_json")
    if disposition == "not_applied":
        return "", {
            "message": "人工查核確認遠端變更未套用且不會晚到。",
            "outcome": "manual_not_applied",
        }
    snapshot = f"before={prepared.before_json}；after={prepared.after_json}"
    if prepared.purpose == "defer":
        summary_line = _prepare_summary_line(
            f"\n⏸ 人工查核[{prepared.operation_id}]：確認 "
            f"{payload['d1']}{payload['m1']} 延至 {payload['d2']}{payload['m2']} 已套用；{snapshot}"
        )
        return summary_line, {
            "message": f"✅ 已將 {payload['d1']}{payload['m1']} 延至 {payload['d2']}{payload['m2']}",
            "outcome": "completed",
            "summary_line": summary_line,
            "approved_by": admin_uid,
        }
    summary_line = _prepare_summary_line(
        f"\n🔄 人工查核[{prepared.operation_id}]：確認 "
        f"{payload['d1']}{payload['m1']} 與 {payload['d2']}{payload['m2']} 互換已套用；{snapshot}"
    )
    return summary_line, {
        "message": (
            f"✅ 人工查核確認已將【{payload['d1']}{payload['m1']}】與"
            f"【{payload['d2']}{payload['m2']}】互換。"
        ),
        "outcome": "completed",
        "summary_line": summary_line,
    }


def manually_reconcile_meal_mutation(
    db_path: str | Path,
    *,
    operation_id: str,
    owner_user_id: str,
    disposition: str,
    evidence_note: str,
    admin_uid: str,
    no_outstanding_late_request_confirmed: bool,
) -> FinalizeResult:
    """Resolve an uncertain operation without provider I/O or readback inference."""

    operation_id = _required_text(operation_id, "operation_id")
    owner_user_id = _required_text(owner_user_id, "owner_user_id")
    disposition = _required_text(disposition, "disposition")
    if any(value != value.strip() for value in (operation_id, owner_user_id, disposition)):
        raise ValueError("manual reconciliation identifiers must be canonical")
    if disposition not in {"applied", "not_applied"}:
        raise ValueError("disposition must be applied or not_applied")
    evidence_note = _required_text(evidence_note, "evidence_note")
    admin_uid = _required_text(admin_uid, "admin_uid")
    if evidence_note != evidence_note.strip() or admin_uid != admin_uid.strip():
        raise ValueError("manual reconciliation audit text must be canonical")
    if no_outstanding_late_request_confirmed is not True:
        raise ValueError("explicit confirmation of no outstanding late request is required")

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _verify_operation_lock_integrity(conn)
        row = _operation_for_finalization(conn, operation_id)
        if row is None or row["owner_user_id"] != owner_user_id:
            raise MealMutationConflict("meal mutation manual binding does not match")
        prepared = _prepared_from_persisted_row(row)
        summary_line, decoded_result = _manual_resolution_receipt(
            prepared, disposition, admin_uid
        )
        result_json = _canonical_json(decoded_result, "manual result")
        expected_status = "completed" if disposition == "applied" else "manual_not_applied"

        if row["status"] not in {"reserved", "outcome_unknown"}:
            if (
                row["status"] != expected_status
                or row["resolution_note"] != evidence_note
                or row["resolved_by"] != admin_uid
                or row["result_json"] != result_json
            ):
                raise MealMutationConflict("meal mutation terminal manual replay differs")
            if disposition == "applied":
                _validate_completed_replay_receipt(
                    conn, row, prepared, summary_line=summary_line,
                    approved_by=admin_uid if prepared.purpose == "defer" else "",
                )
                replay_kind = "replay_completed"
            else:
                replay_kind = "replay_manual_not_applied"
            conn.commit()
            return FinalizeResult(replay_kind, operation_id, decoded_result)

        now = _now_text()
        if disposition == "applied":
            appended = conn.execute(
                "UPDATE health_profile SET summary_text=COALESCE(summary_text,'') || ? "
                "WHERE user_id=?",
                (summary_line, owner_user_id),
            ).rowcount
            if appended != 1:
                raise MealMutationConflict("health profile for meal mutation is missing")
            if prepared.purpose == "defer":
                _complete_deferred_request(conn, prepared, owner_user_id, admin_uid, now)

        changed = conn.execute(
            "UPDATE meal_mutation_operations SET status=?,result_json=?,resolution_note=?,"
            "resolved_by=?,updated_at=?,completed_at=? WHERE operation_id=? "
            "AND owner_user_id=? AND status IN ('reserved','outcome_unknown')",
            (
                expected_status, result_json, evidence_note, admin_uid, now, now,
                operation_id, owner_user_id,
            ),
        ).rowcount
        if changed != 1:
            raise MealMutationConflict("meal mutation manual resolution claim was lost")
        _delete_exact_operation_locks(conn, operation_id)
        _verify_operation_lock_integrity(conn)
        conn.commit()
        kind = "newly_completed" if disposition == "applied" else "newly_manual_not_applied"
        return FinalizeResult(kind, operation_id, decoded_result)
    except BaseException:
        _rollback_on_failure(conn)
        raise
    finally:
        conn.close()
