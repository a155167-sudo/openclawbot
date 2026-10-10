"""Authorized HTTP adapter for versioned dietitian health-check drafts.

This module owns no authentication. The API authenticates first, then calls this
adapter, which opens one write transaction and derives all authoritative state
from SQLite rather than client claims.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3

from vip_health_check import (
    configure_vip_health_check_connection,
    ensure_dietitian_health_check_draft_schema,
    health_check_source_token,
    probe_current_health_check_source_manifest,
    save_health_check_review,
)


class DraftNotFound(ValueError):
    """The requested case does not exist."""


class DraftConflict(ValueError):
    """The case, source snapshot, review version, or operation is not writable."""


def _canonical_payload_hash(fields: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _completed_result(value: str) -> dict[str, object]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise sqlite3.IntegrityError("invalid stored draft operation result") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"review_id", "review_version", "status", "created"}
        or not isinstance(result["review_id"], str)
        or not result["review_id"]
        or isinstance(result["review_version"], bool)
        or not isinstance(result["review_version"], int)
        or result["review_version"] < 1
        or result["status"] != "draft"
        or not isinstance(result["created"], bool)
    ):
        raise sqlite3.IntegrityError("invalid stored draft operation result")
    return {**result, "created": False}


def create_health_check_draft_saver(
    database_path: str | Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    failure_injector: Callable[[str, sqlite3.Connection], None] | None = None,
):
    """Create a transaction-scoped adapter around the existing domain writer."""
    path = str(database_path)
    inject = failure_injector or (lambda _stage, _conn: None)
    with sqlite3.connect(path) as conn:
        configure_vip_health_check_connection(conn)
        ensure_dietitian_health_check_draft_schema(conn)
        conn.commit()

    def save(
        case_id: str,
        fields: Mapping[str, str],
        expected_source_token: str,
        expected_review_version: int,
        request_id: str,
        actor_id: str,
    ) -> dict[str, object]:
        actor_id = str(actor_id or "").strip()
        if not actor_id:
            raise DraftConflict("verified actor is required")
        payload_hash = _canonical_payload_hash(fields)
        with sqlite3.connect(path, timeout=10.0) as conn:
            configure_vip_health_check_connection(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = conn.execute(
                    """SELECT actor_id,payload_hash,expected_source_token,
                              expected_review_version,status,result_json
                       FROM dietitian_health_check_draft_operations
                       WHERE case_id=? AND request_id=?""",
                    (case_id, request_id),
                ).fetchone()
                binding = (
                    actor_id, payload_hash, expected_source_token,
                    expected_review_version,
                )
                if operation is not None:
                    if tuple(operation[:4]) != binding or operation[4] != "completed":
                        raise DraftConflict("request id binding conflict")
                    result = _completed_result(str(operation[5]))
                    conn.rollback()
                    return result

                case = conn.execute(
                    """SELECT status,source_manifest_hash
                       FROM vip_health_check_cases WHERE case_id=?""",
                    (case_id,),
                ).fetchone()
                if case is None:
                    raise DraftNotFound("case not found")
                if case[0] not in {"ready_for_review", "needs_more_info"}:
                    raise DraftConflict("case state is not draft-writable")
                current_token = health_check_source_token(
                    conn, case_id=case_id, manifest_hash=str(case[1])
                )
                if current_token != expected_source_token:
                    raise DraftConflict("case source version is stale")

                evaluated_at = now()
                authoritative = probe_current_health_check_source_manifest(
                    conn, case_id=case_id, evaluated_at=evaluated_at
                )
                if (
                    str(authoritative["source_manifest_hash"]) != str(case[1])
                    or authoritative["status"] != case[0]
                ):
                    raise DraftConflict("canonical case sources changed")

                latest = conn.execute(
                    """SELECT review_version FROM vip_health_check_reviews
                       WHERE case_id=? ORDER BY review_version DESC LIMIT 1""",
                    (case_id,),
                ).fetchone()
                latest_version = 0 if latest is None else int(latest[0])
                if latest_version != expected_review_version:
                    raise DraftConflict("review version is stale")

                created_at = evaluated_at.isoformat()
                conn.execute(
                    """INSERT INTO dietitian_health_check_draft_operations
                       (case_id,request_id,actor_id,payload_hash,expected_source_token,
                        expected_review_version,status,result_json,created_at,completed_at)
                       VALUES (?,?,?,?,?,?,'pending','',?,'')""",
                    (
                        case_id, request_id, actor_id, payload_hash,
                        expected_source_token, expected_review_version, created_at,
                    ),
                )
                inject("operation_reserved", conn)
                try:
                    result = save_health_check_review(
                        conn,
                        case_id=case_id,
                        ai_observations={},
                        review=dict(fields),
                        suggested_values={},
                        limitations="",
                        source_manifest_hash=str(case[1]),
                        saved_at=evaluated_at,
                    )
                except ValueError as exc:
                    raise DraftConflict("draft changed") from exc
                inject("domain_written", conn)
                result_json = json.dumps(
                    result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                changed = conn.execute(
                    """UPDATE dietitian_health_check_draft_operations
                       SET status='completed',result_json=?,completed_at=?
                       WHERE case_id=? AND request_id=? AND status='pending'""",
                    (result_json, created_at, case_id, request_id),
                )
                if changed.rowcount != 1:
                    raise sqlite3.IntegrityError("draft operation completion failed")
                inject("result_stored", conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    return save
