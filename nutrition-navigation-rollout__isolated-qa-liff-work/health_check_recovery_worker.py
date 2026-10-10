"""Bounded recovery for existing VIP health-check projections.

This standalone module deliberately imports only the domain module, never server.py.
It may create notification *intents* through the canonical refresh, but never dispatches
LINE or any other external I/O.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from vip_health_check import configure_vip_health_check_connection, refresh_user_health_check_case

OPEN = ("collecting", "ready_for_review", "needs_more_info")
TW = ZoneInfo("Asia/Taipei")
RECONCILIATION_COLUMNS = {
    "case_id", "user_id", "status", "attempts", "last_error",
    "first_failed_at", "last_failed_at",
}


def _exact_true(value: object) -> bool:
    return str(value or "").strip().lower() == "true"


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.isoformat(timespec="seconds")


def _write_status(path: os.PathLike[str] | str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield True
    except BlockingIOError:
        yield False
    finally:
        handle.close()


def _ensure_worker_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS health_check_recovery_state(
        case_id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK(state IN ('backoff','blocked','exhausted')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_attempt_at TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        observed_last_failed_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(health_check_recovery_state)")}
    if "observed_last_failed_at" not in columns:
        conn.execute("ALTER TABLE health_check_recovery_state ADD COLUMN observed_last_failed_at TEXT NOT NULL DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS health_check_recovery_cursor(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        last_case_id TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL)""")


def _reconciliation_schema(conn: sqlite3.Connection) -> str:
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_refresh_reconciliation'").fetchone()
    if not table:
        return "missing"
    columns = {row[1] for row in conn.execute("PRAGMA table_info(health_check_refresh_reconciliation)")}
    return "valid" if RECONCILIATION_COLUMNS <= columns else "malformed"


def _fatal_summary(*, now_text: str, counters: dict[str, int], error_code: str) -> dict[str, Any]:
    return {"state": "fatal", "last_run_at": now_text, "pending_count": 0,
            "oldest_pending_at": "", "error_count": 1, "error_code": error_code, **counters}


def _summary(conn: sqlite3.Connection, *, now_text: str, state: str | None, counters: dict[str, int]) -> dict[str, Any]:
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_refresh_reconciliation'").fetchone()
    if table:
        pending_count, oldest = conn.execute(
            "SELECT COUNT(*),COALESCE(MIN(first_failed_at),'') FROM health_check_refresh_reconciliation"
        ).fetchone()
    else:
        pending_count, oldest = 0, ""
    state_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_recovery_state'").fetchone()
    state_counts = {"backoff": 0, "blocked": 0, "exhausted": 0}
    if state_table:
        for recovery_state, count in conn.execute(
            "SELECT state,COUNT(*) FROM health_check_recovery_state GROUP BY state"
        ):
            if recovery_state in state_counts:
                state_counts[recovery_state] = int(count)
    errors = sum(state_counts.values())
    if state is None:
        if state_counts["exhausted"]:
            state = "exhausted"
        elif state_counts["blocked"]:
            state = "blocked"
        elif pending_count or state_counts["backoff"] or counters["failed"] or counters["deferred"]:
            state = "degraded"
        else:
            state = "ok"
    return {
        "state": state,
        "last_run_at": now_text,
        "pending_count": int(pending_count),
        "oldest_pending_at": str(oldest or ""),
        "error_count": int(errors),
        **counters,
    }


def run_once(
    db_path: os.PathLike[str] | str,
    *,
    maintenance_enabled: bool,
    worker_enabled: bool,
    now: datetime | None = None,
    dry_run: bool = False,
    limit: int = 20,
    max_attempts: int = 5,
    backoff_seconds: int = 300,
    lock_path: os.PathLike[str] | str | None = None,
    status_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Run one bounded serial pass; never creates cases or performs external I/O."""
    now = now or datetime.now(TW)
    now_text = _iso(now)
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not 1 <= int(max_attempts) <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    counters = {k: 0 for k in ("examined", "succeeded", "fallback_succeeded", "failed", "blocked", "deferred")}

    # Fail closed before opening SQLite: explicit flags off means literally zero DB writes.
    if not (maintenance_enabled and worker_enabled):
        payload = {"state": "disabled", "last_run_at": now_text, "pending_count": 0,
                   "oldest_pending_at": "", "error_count": 0, **counters}
        _write_status(status_path, payload)
        return payload

    db_path = Path(db_path)
    lock = Path(lock_path) if lock_path else Path(str(db_path) + ".health-check-recovery.lock")
    with _exclusive_lock(lock) as acquired:
        if not acquired:
            payload = {"state": "busy", "last_run_at": now_text, "pending_count": 0,
                       "oldest_pending_at": "", "error_count": 0, **counters}
            _write_status(status_path, payload)
            return payload

        # Validate a present debt ledger read-only before any live schema/state write.
        uri = f"file:{db_path.resolve()}?mode=ro"
        try:
            inspection = sqlite3.connect(uri, uri=True)
            try:
                debt_schema = _reconciliation_schema(inspection)
                if debt_schema == "malformed":
                    payload = _fatal_summary(now_text=now_text, counters=counters,
                                             error_code="reconciliation_schema")
                elif dry_run:
                    payload = _summary(inspection, now_text=now_text, state="dry_run", counters=counters)
                else:
                    payload = None
            finally:
                inspection.close()
        except sqlite3.Error:
            payload = _fatal_summary(now_text=now_text, counters=counters,
                                     error_code="database_access")
        if payload is not None:
            _write_status(status_path, payload)
            return payload

        conn = sqlite3.connect(db_path, timeout=5)
        try:
            configure_vip_health_check_connection(conn)
            _ensure_worker_schema(conn)
            conn.commit()
            has_debt = debt_schema == "valid"
            pending: list[tuple[str, str, str]] = []
            if has_debt:
                pending = conn.execute("""SELECT r.case_id,r.user_id,r.last_failed_at
                    FROM health_check_refresh_reconciliation r
                    LEFT JOIN health_check_recovery_state s ON s.case_id=r.case_id
                    WHERE s.case_id IS NULL
                       OR s.observed_last_failed_at<>r.last_failed_at
                       OR (s.observed_last_failed_at=r.last_failed_at AND s.state='backoff'
                           AND s.attempts<? AND s.next_attempt_at<=?)
                    ORDER BY r.first_failed_at,r.case_id LIMIT ?""",
                    (max_attempts, now_text, limit)).fetchall()
                deferred = conn.execute("""SELECT COUNT(*) FROM health_check_refresh_reconciliation r
                    JOIN health_check_recovery_state s ON s.case_id=r.case_id
                    WHERE s.observed_last_failed_at=r.last_failed_at
                      AND s.state='backoff' AND s.next_attempt_at>?""", (now_text,)).fetchone()[0]
                counters["deferred"] += int(deferred)

            processed: set[str] = set()
            for case_id, debt_owner, debt_version in pending:
                counters["examined"] += 1
                processed.add(case_id)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    latest = conn.execute("SELECT user_id,status FROM vip_health_check_cases WHERE case_id=?", (case_id,)).fetchone()
                    if not latest or latest[0] != debt_owner or latest[1] not in OPEN:
                        conn.execute("""INSERT INTO health_check_recovery_state
                            (case_id,state,attempts,next_attempt_at,error_code,observed_last_failed_at,updated_at)
                            VALUES (?,'blocked',0,'','terminal_or_owner_mismatch',?,?)
                            ON CONFLICT(case_id) DO UPDATE SET state='blocked',next_attempt_at='',
                            error_code='terminal_or_owner_mismatch',attempts=0,
                            observed_last_failed_at=excluded.observed_last_failed_at,
                            updated_at=excluded.updated_at""", (case_id, debt_version, now_text))
                        conn.commit(); counters["blocked"] += 1; continue
                    result = refresh_user_health_check_case(conn, user_id=debt_owner, evaluated_at=now)
                    if not result or result.get("case_id") != case_id:
                        raise sqlite3.IntegrityError("refresh target changed")
                    # Debt is cleared only if the exact observed version still owns this task.
                    deleted = conn.execute("""DELETE FROM health_check_refresh_reconciliation
                        WHERE case_id=? AND user_id=? AND last_failed_at=?""",
                        (case_id, debt_owner, debt_version)).rowcount
                    if deleted != 1:
                        raise sqlite3.IntegrityError("reconciliation debt changed")
                    conn.execute("DELETE FROM health_check_recovery_state WHERE case_id=?", (case_id,))
                    conn.commit(); counters["succeeded"] += 1
                except Exception as exc:
                    conn.rollback()
                    prior = conn.execute("SELECT attempts,observed_last_failed_at FROM health_check_recovery_state WHERE case_id=?", (case_id,)).fetchone()
                    attempts = int(prior[0]) + 1 if prior and prior[1] == debt_version else 1
                    state = "exhausted" if attempts >= max_attempts else "backoff"
                    delay = min(backoff_seconds * (2 ** max(0, attempts - 1)), 86400)
                    next_at = "" if state == "exhausted" else _iso(now + timedelta(seconds=delay))
                    conn.execute("""INSERT INTO health_check_recovery_state
                        (case_id,state,attempts,next_attempt_at,error_code,observed_last_failed_at,updated_at) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(case_id) DO UPDATE SET state=excluded.state,attempts=excluded.attempts,
                        next_attempt_at=excluded.next_attempt_at,error_code=excluded.error_code,
                        observed_last_failed_at=excluded.observed_last_failed_at,updated_at=excluded.updated_at""",
                        (case_id, state, attempts, next_at, type(exc).__name__, debt_version, now_text))
                    conn.commit(); counters["failed"] += 1

            remaining = limit - counters["examined"]
            if remaining > 0:
                cursor_row = conn.execute("SELECT last_case_id FROM health_check_recovery_cursor WHERE singleton=1").fetchone()
                cursor = cursor_row[0] if cursor_row else ""
                placeholders = ",".join("?" for _ in OPEN)
                fallback = conn.execute(f"""SELECT c.case_id,c.user_id FROM vip_health_check_cases c
                    LEFT JOIN health_check_recovery_state s ON s.case_id=c.case_id
                    WHERE c.status IN ({placeholders}) AND c.case_id>? AND c.window_started_at<=?
                      AND (s.case_id IS NULL OR (s.state='backoff' AND s.attempts<? AND s.next_attempt_at<=?))
                    ORDER BY c.case_id LIMIT ?""",
                    (*OPEN, cursor, now_text, max_attempts, now_text, remaining)).fetchall()
                if not fallback and cursor:
                    conn.execute("""INSERT INTO health_check_recovery_cursor(singleton,last_case_id,updated_at)
                        VALUES (1,'',?) ON CONFLICT(singleton) DO UPDATE SET last_case_id='',updated_at=excluded.updated_at""", (now_text,))
                    conn.commit()
                for case_id, owner in fallback:
                    if case_id in processed:
                        continue
                    counters["examined"] += 1
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        latest = conn.execute("SELECT user_id,status FROM vip_health_check_cases WHERE case_id=?", (case_id,)).fetchone()
                        if not latest or latest[0] != owner or latest[1] not in OPEN:
                            conn.rollback(); continue
                        result = refresh_user_health_check_case(conn, user_id=owner, evaluated_at=now)
                        if not result or result.get("case_id") != case_id:
                            raise sqlite3.IntegrityError("fallback target changed")
                        conn.execute("""INSERT INTO health_check_recovery_cursor(singleton,last_case_id,updated_at)
                            VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET
                            last_case_id=excluded.last_case_id,updated_at=excluded.updated_at""", (case_id, now_text))
                        conn.execute("DELETE FROM health_check_recovery_state WHERE case_id=?", (case_id,))
                        conn.commit(); counters["fallback_succeeded"] += 1
                    except Exception as exc:
                        conn.rollback()
                        prior = conn.execute("SELECT attempts FROM health_check_recovery_state WHERE case_id=?", (case_id,)).fetchone()
                        attempts = int(prior[0]) + 1 if prior else 1
                        state = "exhausted" if attempts >= max_attempts else "backoff"
                        delay = min(backoff_seconds * (2 ** max(0, attempts - 1)), 86400)
                        next_at = "" if state == "exhausted" else _iso(now + timedelta(seconds=delay))
                        conn.execute("""INSERT INTO health_check_recovery_state
                            (case_id,state,attempts,next_attempt_at,error_code,observed_last_failed_at,updated_at) VALUES (?,?,?,?,?,'',?)
                            ON CONFLICT(case_id) DO UPDATE SET state=excluded.state,attempts=excluded.attempts,
                            next_attempt_at=excluded.next_attempt_at,error_code=excluded.error_code,
                            observed_last_failed_at='',updated_at=excluded.updated_at""",
                            (case_id, state, attempts, next_at, type(exc).__name__, now_text))
                        conn.commit(); counters["failed"] += 1
            payload = _summary(conn, now_text=now_text, state=None, counters=counters)
        finally:
            conn.close()
    _write_status(status_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Existing-case health-check reconciliation worker")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "nutrition_bot.db"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--status-file", default=os.environ.get("VIP_HEALTH_CHECK_RECOVERY_STATUS_FILE", ""))
    args = parser.parse_args(argv)
    enabled = _exact_true(os.environ.get("VIP_HEALTH_CHECK_RECOVERY_WORKER_ENABLED"))
    maintenance = _exact_true(os.environ.get("VIP_HEALTH_CHECK_MAINTENANCE_ENABLED"))
    while True:
        try:
            result = run_once(args.db, maintenance_enabled=maintenance, worker_enabled=enabled,
                              dry_run=args.dry_run, limit=args.limit, status_path=args.status_file)
        except Exception:
            counters = {k: 0 for k in ("examined", "succeeded", "fallback_succeeded", "failed", "blocked", "deferred")}
            result = _fatal_summary(now_text=_iso(datetime.now(TW)), counters=counters,
                                    error_code="worker_exception")
            _write_status(args.status_file, result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        if result["state"] == "fatal":
            return 1
        if args.once or result["state"] in {"disabled", "dry_run"}:
            return 0
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
