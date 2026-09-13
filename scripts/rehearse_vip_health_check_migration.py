#!/usr/bin/env python3
"""Rehearse the VIP health-check migration against a SQLite copy, never the source."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vip_health_check import ensure_vip_health_check_schema


EXPECTED_VIP_HEALTH_CHECK_TABLES = frozenset({
    "vip_health_check_activation_events",
    "vip_health_check_audit_log",
    "vip_health_check_cases",
    "vip_health_check_deliveries",
    "vip_health_check_reports",
    "vip_health_check_reviews",
    "vip_health_check_source_refs",
    "vip_health_check_valid_days",
})
SHIPPED_AUXILIARY_TABLES = frozenset({"vip_health_check_notifications"})
PRESERVED_DOMAIN_TABLES = (
    EXPECTED_VIP_HEALTH_CHECK_TABLES
    | SHIPPED_AUXILIARY_TABLES
    | {"dietitian_coaching_orders"}
)


def _require_expected_health_check_tables(conn: sqlite3.Connection) -> set[str]:
    actual = {
        row[0]
        for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name LIKE 'vip_health_check_%'"""
        )
    }
    accepted = {
        EXPECTED_VIP_HEALTH_CHECK_TABLES,
        EXPECTED_VIP_HEALTH_CHECK_TABLES | SHIPPED_AUXILIARY_TABLES,
    }
    if frozenset(actual) not in accepted:
        raise sqlite3.IntegrityError(
            "candidate VIP health-check table set mismatch: "
            f"missing={sorted(EXPECTED_VIP_HEALTH_CHECK_TABLES - actual)!r}, "
            f"unexpected={sorted(actual - EXPECTED_VIP_HEALTH_CHECK_TABLES - SHIPPED_AUXILIARY_TABLES)!r}"
        )
    return actual


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checks(conn: sqlite3.Connection) -> dict[str, object]:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
    }


def _fingerprint_existing_health_check_rows(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, object]]:
    """Hash complete row contents without emitting customer/report values."""
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    fingerprints: dict[str, dict[str, object]] = {}
    for table_name in sorted(PRESERVED_DOMAIN_TABLES & existing):
        columns = [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table_name}")')]
        rows = []
        for row in conn.execute(f'SELECT * FROM "{table_name}"'):
            normalized = [
                {"bytes_hex": value.hex()} if isinstance(value, bytes) else value
                for value in row
            ]
            rows.append(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
        rows.sort()
        payload = json.dumps(
            {"columns": columns, "rows": rows},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprints[table_name] = {
            "row_count": len(rows),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return fingerprints


def rehearse(source: Path, candidate: Path) -> dict[str, object]:
    source = source.resolve()
    candidate = candidate.resolve()
    if source == candidate:
        raise ValueError("candidate path must differ from source")
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if candidate.exists():
        raise FileExistsError(f"candidate path already exists: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True)

    source_hash_before = _sha256(source)
    source_uri = source.as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        before = _checks(source_conn)
        if before != {"integrity_check": "ok", "foreign_key_violation_count": 0}:
            raise sqlite3.IntegrityError(f"source database checks failed: {before!r}")
        source_row_fingerprints = _fingerprint_existing_health_check_rows(source_conn)
        with sqlite3.connect(candidate) as candidate_conn:
            source_conn.backup(candidate_conn)

    with sqlite3.connect(candidate) as candidate_conn:
        ensure_vip_health_check_schema(candidate_conn)
        ensure_vip_health_check_schema(candidate_conn)
        candidate_conn.commit()
        after = _checks(candidate_conn)
        table_names = _require_expected_health_check_tables(candidate_conn)
        candidate_row_fingerprints = {
            table_name: fingerprint
            for table_name, fingerprint in _fingerprint_existing_health_check_rows(
                candidate_conn
            ).items()
            if table_name in source_row_fingerprints
        }
    if after != {"integrity_check": "ok", "foreign_key_violation_count": 0}:
        raise sqlite3.IntegrityError(f"candidate database checks failed: {after!r}")
    if candidate_row_fingerprints != source_row_fingerprints:
        raise sqlite3.IntegrityError("VIP health-check row contents changed during migration")

    source_hash_after = _sha256(source)
    if source_hash_after != source_hash_before:
        raise RuntimeError("source database changed during rehearsal")
    return {
        "source_sha256": source_hash_after,
        "source_unchanged": True,
        "source_checks": before,
        "candidate_checks": after,
        "preserved_row_fingerprints": candidate_row_fingerprints,
        "vip_health_check_table_count": len(table_names),
        "vip_health_check_tables": sorted(table_names),
        "candidate_sha256": _sha256(candidate),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = rehearse(args.source, args.candidate)
    except Exception as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"passed": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
