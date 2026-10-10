"""Delete private meal photos only after an exact, committed health-check delivery."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from typing import Callable

from dietitian_health_check_api import (
    _source_hash_matches,
    _validated_case_source_refs,
    _validated_days_for_case,
    _validate_case_day_semantics,
    _validate_case_source_semantics,
)
from nutrition_system import user_confirmed_meal_photo_estimate_is_valid

_IMAGE_REF = re.compile(r"nutrition-image:([0-9a-f]{32}\.(?:jpg|png|webp))\Z")
_IMAGE_TEMP_NAME = re.compile(
    r"[0-9a-f]{32}\.(?:jpg|png|webp)\.[0-9a-f]{16}\.tmp\Z"
)
_MAX_IMAGE_TEMP_BYTES = 10 * 1024 * 1024

# Complete inventory of persisted opaque nutrition-image locator holders.
_IMAGE_REFERENCE_HOLDERS = {
    "pending_meal_photo_drafts": "source_image_ref",
    "pending_nutrition_logs": "source_image_ref",
    "food_logs": "source_image_ref",
    "food_catalog": "original_image_ref",
}


def image_reference_holder_schema_is_complete(conn: sqlite3.Connection) -> bool:
    """Fail closed for partial health schema while allowing complete legacy databases."""
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "pending_meal_photo_drafts": {"source_image_ref", "token"},
        "pending_nutrition_logs": {"source_image_ref", "token"},
        "food_logs": {"source_image_ref", "log_id"},
        "food_catalog": {
            "original_image_ref", "food_id", "owner_user_id", "visibility",
        },
    }
    health_contract = {
        "vip_health_check_cases": {
            "case_id", "user_id", "status", "valid_day_count", "window_started_at",
            "window_ends_at", "source_manifest_hash",
        },
        "vip_health_check_source_refs": {
            "case_id", "food_log_id", "food_log_version", "local_date", "source_hash",
        },
        "vip_health_check_valid_days": {
            "case_id", "local_date", "rule_version", "qualifying_meal_count",
            "completeness_status", "evaluated_at",
        },
        "vip_health_check_reviews": {
            "review_id", "case_id", "status", "source_manifest_hash", "approved_by",
            "approved_at",
        },
        "vip_health_check_reports": {
            "report_id", "case_id", "review_id", "report_kind", "source_manifest_hash",
        },
        "vip_health_check_deliveries": {
            "report_id", "user_id", "status", "delivered_at",
        },
    }
    # A database predating this feature has no health tables and remains supported.  Once
    # any canonical health table exists, every table/column consumed by retention proof is
    # required; otherwise an interrupted/partial migration must fail closed.
    if set(health_contract) & tables:
        required.update(health_contract)
    return all(
        table in tables
        and columns.issubset({
            str(info[1]) for info in conn.execute(f'PRAGMA table_info("{table}")')
        })
        for table, columns in required.items()
    )


def nutrition_image_reference_is_protected(
    conn: sqlite3.Connection,
    image_ref: str,
    *,
    nutrition_token: str | None = None,
    meal_token: str | None = None,
    food_log_id: str | None = None,
    food_id: str | None = None,
    food_owner_id: str | None = None,
    health_case_id: str | None = None,
) -> bool:
    """Return true when another authoritative holder still needs this locator."""
    if not image_reference_holder_schema_is_complete(conn):
        return True
    excluded_food_id = None
    if food_id is not None and food_owner_id is not None:
        catalog = conn.execute(
            """SELECT owner_user_id,visibility,original_image_ref
               FROM food_catalog WHERE food_id=?""",
            (food_id,),
        ).fetchone()
        if (
            catalog is not None
            and catalog[0] == food_owner_id
            and catalog[1] == "private"
            and catalog[2] == image_ref
        ):
            excluded_food_id = food_id
    checks = (
        (
            "SELECT 1 FROM pending_nutrition_logs WHERE source_image_ref=? AND (? IS NULL OR token<>?) LIMIT 1",
            (image_ref, nutrition_token, nutrition_token),
        ),
        (
            "SELECT 1 FROM pending_meal_photo_drafts WHERE source_image_ref=? AND (? IS NULL OR token<>?) LIMIT 1",
            (image_ref, meal_token, meal_token),
        ),
        (
            "SELECT 1 FROM food_logs WHERE source_image_ref=? AND (? IS NULL OR log_id<>?) LIMIT 1",
            (image_ref, food_log_id, food_log_id),
        ),
        (
            "SELECT 1 FROM food_catalog WHERE original_image_ref=? AND (? IS NULL OR food_id<>?) LIMIT 1",
            (image_ref, excluded_food_id, excluded_food_id),
        ),
    )
    if any(conn.execute(sql, params).fetchone() for sql, params in checks):
        return True
    has_health_refs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vip_health_check_source_refs'"
    ).fetchone()
    if food_log_id is not None and has_health_refs and conn.execute(
        """SELECT 1 FROM vip_health_check_source_refs
           WHERE food_log_id=? AND (? IS NULL OR case_id<>?) LIMIT 1""",
        (food_log_id, health_case_id, health_case_id),
    ).fetchone():
        return True
    return False


class CleanupBlocked(RuntimeError):
    """The persisted relationships do not prove that this image may be retired."""


def cleanup_stale_nutrition_image_temps(
    image_root: str, *, now_timestamp: float, stale_after_seconds: float = 3600
) -> int:
    """Delete only stale regular files matching the writer's exact temp grammar."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(image_root, flags)
    except OSError:
        return 0
    deleted = 0
    try:
        for filename in os.listdir(root_fd):
            if _IMAGE_TEMP_NAME.fullmatch(filename) is None:
                continue
            try:
                metadata = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not 0 <= metadata.st_size <= _MAX_IMAGE_TEMP_BYTES
                    or now_timestamp - metadata.st_mtime <= stale_after_seconds
                ):
                    continue
                os.unlink(filename, dir_fd=root_fd)
                deleted += 1
            except OSError:
                continue
        return deleted
    finally:
        os.close(root_fd)


def safe_unlink_nutrition_image(image_root: str, image_ref: str) -> str:
    """Unlink one regular leaf through a no-follow directory descriptor."""
    match = _IMAGE_REF.fullmatch(str(image_ref or ""))
    if match is None:
        raise CleanupBlocked("invalid opaque image reference")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(image_root, flags)
    try:
        try:
            metadata = os.stat(match.group(1), dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        if not stat.S_ISREG(metadata.st_mode):
            raise CleanupBlocked("image leaf is not a regular file")
        os.unlink(match.group(1), dir_fd=root_fd)
        return "deleted"
    finally:
        os.close(root_fd)


def _queue_outbox(conn: sqlite3.Connection, entity_type: str, entity_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO nutrition_sheet_outbox
           (outbox_id,entity_type,entity_id,status,attempts,last_error,claimed_at,
            lease_owner,resync_required,created_at,synced_at)
           VALUES (?, ?, ?, 'pending', 0, '', '', '', 0, ?, '')
           ON CONFLICT(entity_type,entity_id) DO UPDATE SET
             status=CASE WHEN status='processing' THEN status ELSE 'pending' END,
             resync_required=CASE WHEN status='processing' THEN 1 ELSE resync_required END,
             claimed_at=CASE WHEN status='processing' THEN claimed_at ELSE '' END,
             lease_owner=CASE WHEN status='processing' THEN lease_owner ELSE '' END,
             synced_at=CASE WHEN status='processing' THEN synced_at ELSE '' END""",
        ("outbox_" + secrets.token_hex(8), entity_type, entity_id, now),
    )


def _case_manifest_is_proven(conn: sqlite3.Connection, case_id: str) -> bool:
    """Recompute the complete canonical manifest and bind its approved delivery."""
    rows = conn.execute(
        """SELECT c.case_id,c.user_id,c.status,c.valid_day_count,
                  c.window_started_at,c.window_ends_at,
                  c.source_manifest_hash AS case_manifest,
                  r.source_manifest_hash AS report_manifest,
                  review.source_manifest_hash AS review_manifest
           FROM vip_health_check_cases c
           JOIN vip_health_check_reports r ON r.case_id=c.case_id
           JOIN vip_health_check_reviews review ON review.review_id=r.review_id
                                                AND review.case_id=r.case_id
           JOIN vip_health_check_deliveries d ON d.report_id=r.report_id
           WHERE c.case_id=? AND c.status='delivered'
             AND r.report_kind='baseline_3day'
             AND review.status='approved'
             AND review.approved_by<>'' AND review.approved_at<>''
             AND d.user_id=c.user_id AND d.status='delivered' AND d.delivered_at<>''""",
        (case_id,),
    ).fetchall()
    for row in rows:
        manifest = row["case_manifest"]
        if manifest != row["report_manifest"] or manifest != row["review_manifest"]:
            continue
        try:
            validated_days = _validated_days_for_case(conn, case_id=case_id)
            _validate_case_day_semantics(row, validated_days)
            _validated_case_source_refs(
                conn,
                case=row,
                validated_days=validated_days,
                stored_manifest=manifest,
            )
            _validate_case_source_semantics(
                conn, case=row, validated_days=validated_days
            )
        except (sqlite3.Error, TypeError, ValueError):
            continue
        return True
    return False


def _candidate_log_ids(conn: sqlite3.Connection, case_id: str) -> list[str]:
    if not _case_manifest_is_proven(conn, case_id):
        return []
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT sr.food_log_id
               FROM vip_health_check_source_refs sr
               JOIN vip_health_check_cases c ON c.case_id=sr.case_id
               WHERE sr.case_id=? AND c.status='delivered'
                 AND EXISTS (
                   SELECT 1 FROM vip_health_check_reports r
                   JOIN vip_health_check_reviews review ON review.review_id=r.review_id
                                                    AND review.case_id=r.case_id
                   JOIN vip_health_check_deliveries d ON d.report_id=r.report_id
                   WHERE r.case_id=c.case_id
                     AND r.report_kind='baseline_3day'
                     AND r.source_manifest_hash=c.source_manifest_hash
                     AND review.status='approved'
                     AND review.approved_by<>'' AND review.approved_at<>''
                     AND d.user_id=c.user_id
                     AND d.status='delivered'
                     AND d.delivered_at<>''
                 )
               ORDER BY sr.food_log_id""",
            (case_id,),
        )
    ]


def _load_proven_candidate(
    conn: sqlite3.Connection, case_id: str, log_id: str
) -> sqlite3.Row | None:
    if not _case_manifest_is_proven(conn, case_id):
        return None
    rows = conn.execute(
        """SELECT c.user_id AS case_user_id,c.status AS case_status,
                  c.source_manifest_hash AS case_manifest,
                  sr.food_log_id,sr.food_log_version,sr.source_hash,sr.local_date,
                  fl.log_id,fl.user_id AS log_user_id,fl.food_id,fl.version,
                  fl.consumed_at,fl.meal_slot,fl.nutrition_snapshot_json,fl.source_image_ref,
                  fl.confirmation_status,fl.deleted_at,fl.trust_type,fl.trust_hash,
                  fl.exchange_snapshot_json,
                  fc.owner_user_id AS catalog_owner_user_id,
                  fc.visibility AS catalog_visibility,
                  fc.source_type AS catalog_source_type,
                  fc.original_image_ref AS catalog_image_ref,
                  d.token AS draft_token,d.user_id AS draft_user_id,
                  d.source_image_ref AS draft_image_ref,d.status AS draft_status,
                  d.version AS draft_version,d.workflow_version,
                  d.confirmed_log_id,d.approved_log_id,d.confirmed_by
           FROM vip_health_check_cases c
           JOIN vip_health_check_source_refs sr ON sr.case_id=c.case_id
           JOIN food_logs fl ON fl.log_id=sr.food_log_id
           JOIN food_catalog fc ON fc.food_id=fl.food_id
           JOIN pending_meal_photo_drafts d
             ON d.user_id=c.user_id AND d.source_image_ref=fl.source_image_ref
            AND (d.confirmed_log_id=fl.log_id OR d.approved_log_id=fl.log_id)
           WHERE c.case_id=? AND sr.food_log_id=?
             AND c.status='delivered'
             AND EXISTS (
               SELECT 1 FROM vip_health_check_reports r
               JOIN vip_health_check_reviews review ON review.review_id=r.review_id
                                                AND review.case_id=r.case_id
               JOIN vip_health_check_deliveries delivery ON delivery.report_id=r.report_id
               WHERE r.case_id=c.case_id
                 AND r.report_kind='baseline_3day'
                 AND r.source_manifest_hash=c.source_manifest_hash
                 AND review.status='approved'
                 AND review.approved_by<>'' AND review.approved_at<>''
                 AND delivery.user_id=c.user_id
                 AND delivery.status='delivered'
                 AND delivery.delivered_at<>''
             )""",
        (case_id, log_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    image_ref = str(row["source_image_ref"] or "")
    if (
        not image_ref
        or _IMAGE_REF.fullmatch(image_ref) is None
        or row["case_user_id"] != row["log_user_id"]
        or row["case_user_id"] != row["draft_user_id"]
        or row["case_user_id"] != row["catalog_owner_user_id"]
        or row["catalog_visibility"] != "private"
        or row["food_log_id"] != row["log_id"]
        or row["food_log_version"] != row["version"]
        or row["confirmation_status"] != "confirmed"
        or str(row["deleted_at"] or "")
        or row["catalog_source_type"] != "user_meal_photo"
        or image_ref != row["draft_image_ref"]
        or image_ref != row["catalog_image_ref"]
        or not _source_hash_matches(conn, row)
    ):
        return None
    if row["workflow_version"] in {
        "user_confirmed_ai_estimate_v1", "user_confirmed_ai_nutrition_v2"
    }:
        if (
            row["draft_status"] != "user_confirmed"
            or row["confirmed_log_id"] != log_id
            or row["confirmed_by"] != row["case_user_id"]
            or not user_confirmed_meal_photo_estimate_is_valid(
                conn, log_id,
                expected_user_id=row["case_user_id"],
                expected_draft_token=row["draft_token"],
                expected_draft_version=row["draft_version"],
            )
        ):
            return None
    elif row["workflow_version"] == "expert_review_v1":
        if row["draft_status"] != "approved" or row["approved_log_id"] != log_id:
            return None
    else:
        return None
    return row


def _has_unsafe_shared_reference(conn: sqlite3.Connection, row: sqlite3.Row, case_id: str) -> bool:
    return nutrition_image_reference_is_protected(
        conn,
        row["source_image_ref"],
        meal_token=row["draft_token"],
        food_log_id=row["log_id"],
        food_id=row["food_id"],
        food_owner_id=row["case_user_id"],
        health_case_id=case_id,
    )


def _preflight_case(conn: sqlite3.Connection, case_id: str) -> tuple[list[sqlite3.Row], int] | None:
    """Validate every canonical source before selecting image candidates."""
    if not _case_manifest_is_proven(conn, case_id):
        return None
    sources = conn.execute(
        """SELECT sr.food_log_id,fl.source_image_ref
           FROM vip_health_check_source_refs sr
           JOIN vip_health_check_cases c ON c.case_id=sr.case_id
           JOIN food_logs fl ON fl.log_id=sr.food_log_id
                            AND fl.user_id=c.user_id
                            AND fl.version=sr.food_log_version
           WHERE sr.case_id=? ORDER BY sr.food_log_id""",
        (case_id,),
    ).fetchall()
    candidates: list[sqlite3.Row] = []
    for source in sources:
        # Canonical validation applies to all sources; photo eligibility does not.
        if not str(source["source_image_ref"] or ""):
            continue
        candidate = _load_proven_candidate(conn, case_id, str(source["food_log_id"]))
        if candidate is None or _has_unsafe_shared_reference(conn, candidate, case_id):
            return None
        candidates.append(candidate)
    return candidates, len(sources)


def _cleanup_case(
    db_path: str, image_root: str, case_id: str,
    unlinker: Callable[[str, str], str],
) -> list[str]:
    """Preflight a whole case under one write lock before the first unlink."""
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            preflight = _preflight_case(conn, case_id)
            if preflight is None:
                count = int(conn.execute(
                    "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
                    (case_id,),
                ).fetchone()[0])
                conn.rollback()
                return ["blocked"] * count
            candidates, _ = preflight
            outcomes: list[str] = []
            for row in candidates:
                try:
                    outcome = unlinker(image_root, row["source_image_ref"])
                except CleanupBlocked:
                    conn.rollback()
                    return ["blocked"] * len(candidates)
                except OSError:
                    conn.rollback()
                    return ["retry_pending"] * len(candidates)
                if outcome not in {"deleted", "missing"}:
                    conn.rollback()
                    return ["retry_pending"] * len(candidates)
                changed_draft = conn.execute(
                    """UPDATE pending_meal_photo_drafts SET source_image_ref=''
                       WHERE token=? AND user_id=? AND source_image_ref=?
                         AND status IN ('user_confirmed','approved')""",
                    (row["draft_token"], row["case_user_id"], row["source_image_ref"]),
                ).rowcount
                changed_log = conn.execute(
                    """UPDATE food_logs SET source_image_ref=''
                       WHERE log_id=? AND user_id=? AND version=? AND source_image_ref=?""",
                    (row["log_id"], row["case_user_id"], row["version"], row["source_image_ref"]),
                ).rowcount
                changed_food = conn.execute(
                    """UPDATE food_catalog SET original_image_ref=''
                       WHERE food_id=? AND owner_user_id=? AND original_image_ref=?""",
                    (row["food_id"], row["case_user_id"], row["source_image_ref"]),
                ).rowcount
                if (changed_draft, changed_log, changed_food) != (1, 1, 1):
                    raise sqlite3.IntegrityError("image reference CAS did not converge")
                _queue_outbox(conn, "food", row["food_id"])
                _queue_outbox(conn, "food_log", row["log_id"])
                outcomes.append(outcome)
            conn.commit()
            return outcomes
        except Exception:
            conn.rollback()
            raise


def _cleanup_one(
    db_path: str,
    image_root: str,
    case_id: str,
    log_id: str,
    unlinker: Callable[[str, str], str],
) -> str:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _load_proven_candidate(conn, case_id, log_id)
            if row is None or _has_unsafe_shared_reference(conn, row, case_id):
                conn.rollback()
                return "blocked"
            try:
                outcome = unlinker(image_root, row["source_image_ref"])
            except CleanupBlocked:
                conn.rollback()
                return "blocked"
            except OSError:
                conn.rollback()
                return "retry_pending"
            if outcome not in {"deleted", "missing"}:
                conn.rollback()
                return "retry_pending"
            changed_draft = conn.execute(
                """UPDATE pending_meal_photo_drafts SET source_image_ref=''
                   WHERE token=? AND user_id=? AND source_image_ref=?
                     AND status IN ('user_confirmed','approved')""",
                (row["draft_token"], row["case_user_id"], row["source_image_ref"]),
            ).rowcount
            changed_log = conn.execute(
                """UPDATE food_logs SET source_image_ref=''
                   WHERE log_id=? AND user_id=? AND version=? AND source_image_ref=?""",
                (row["log_id"], row["case_user_id"], row["version"], row["source_image_ref"]),
            ).rowcount
            changed_food = conn.execute(
                """UPDATE food_catalog SET original_image_ref=''
                   WHERE food_id=? AND owner_user_id=? AND original_image_ref=?""",
                (row["food_id"], row["case_user_id"], row["source_image_ref"]),
            ).rowcount
            if (changed_draft, changed_log, changed_food) != (1, 1, 1):
                raise sqlite3.IntegrityError("image reference CAS did not converge")
            _queue_outbox(conn, "food", row["food_id"])
            _queue_outbox(conn, "food_log", row["log_id"])
            conn.commit()
            return outcome
        except Exception:
            conn.rollback()
            raise


def cleanup_delivered_health_check_images(
    db_path: str,
    image_root: str,
    *,
    case_id: str | None = None,
    unlinker: Callable[[str, str], str] = safe_unlink_nutrition_image,
) -> dict[str, int]:
    """Retry cleanup from committed delivery state; never accepts caller success as proof."""
    counts = {"deleted": 0, "missing": 0, "blocked": 0, "retry_pending": 0}
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {
            "vip_health_check_cases", "vip_health_check_source_refs",
            "vip_health_check_valid_days",
            "vip_health_check_reviews", "vip_health_check_reports",
            "vip_health_check_deliveries",
            "food_logs", "food_catalog", "pending_meal_photo_drafts",
            "nutrition_sheet_outbox",
        }
        holder_schema_is_complete = image_reference_holder_schema_is_complete(conn)
        if not required.issubset(tables) or not holder_schema_is_complete:
            return counts
        if case_id is None:
            case_ids = [
                str(row[0])
                for row in conn.execute(
                    """SELECT DISTINCT c.case_id
                       FROM vip_health_check_cases c
                       JOIN vip_health_check_reports r ON r.case_id=c.case_id
                       JOIN vip_health_check_deliveries d ON d.report_id=r.report_id
                       WHERE c.status='delivered' AND d.status='delivered'
                         AND d.user_id=c.user_id AND d.delivered_at<>''"""
                )
            ]
        else:
            case_ids = [str(case_id)]
        conn.execute("BEGIN IMMEDIATE")
        try:
            work: list[tuple[str, str]] = []
            for candidate_case in case_ids:
                candidate_ids = _candidate_log_ids(conn, candidate_case)
                if candidate_ids:
                    work.extend((candidate_case, log_id) for log_id in candidate_ids)
                elif not _case_manifest_is_proven(conn, candidate_case):
                    counts["blocked"] += int(conn.execute(
                        "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
                        (candidate_case,),
                    ).fetchone()[0])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    for candidate_case in dict.fromkeys(item[0] for item in work):
        for outcome in _cleanup_case(db_path, image_root, candidate_case, unlinker):
            counts[outcome] += 1
    return counts
