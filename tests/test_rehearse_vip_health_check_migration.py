from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rehearsal_migrates_only_online_backup_copy(tmp_path):
    source = tmp_path / "production-backup.db"
    candidate = tmp_path / "migration-rehearsal.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE usage(user_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO usage VALUES ('U1', 'vip')")
    source_hash = _sha256(source)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/rehearse_vip_health_check_migration.py",
            "--source",
            str(source),
            "--candidate",
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["passed"] is True
    assert result["source_unchanged"] is True
    assert result["source_sha256"] == source_hash == _sha256(source)
    assert result["source_checks"] == {
        "integrity_check": "ok",
        "foreign_key_violation_count": 0,
    }
    assert result["candidate_checks"] == result["source_checks"]
    assert result["vip_health_check_table_count"] == 8
    assert result["vip_health_check_tables"] == [
        "vip_health_check_activation_events",
        "vip_health_check_audit_log",
        "vip_health_check_cases",
        "vip_health_check_deliveries",
        "vip_health_check_reports",
        "vip_health_check_reviews",
        "vip_health_check_source_refs",
        "vip_health_check_valid_days",
    ]
    with sqlite3.connect(source) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'vip_health_check_%'"
        ).fetchone()[0] == 0
    with sqlite3.connect(candidate) as conn:
        assert conn.execute(
            "SELECT status FROM usage WHERE user_id='U1'"
        ).fetchone() == ("vip",)


def test_rehearsal_table_set_gate_rejects_missing_and_unexpected_tables():
    from scripts.rehearse_vip_health_check_migration import (
        EXPECTED_VIP_HEALTH_CHECK_TABLES,
        _require_expected_health_check_tables,
    )

    variants = [
        EXPECTED_VIP_HEALTH_CHECK_TABLES - {"vip_health_check_activation_events"},
        EXPECTED_VIP_HEALTH_CHECK_TABLES | {"vip_health_check_unexpected"},
    ]
    for table_names in variants:
        with sqlite3.connect(":memory:") as conn:
            for table_name in table_names:
                conn.execute(f'CREATE TABLE "{table_name}" (value TEXT)')
            with pytest.raises(sqlite3.IntegrityError, match="table set mismatch"):
                _require_expected_health_check_tables(conn)


def test_rehearsal_cli_rejects_unexpected_health_check_table(tmp_path):
    source = tmp_path / "source-with-unexpected.db"
    candidate = tmp_path / "candidate.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE vip_health_check_unexpected(value TEXT)")
    source_hash = _sha256(source)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/rehearse_vip_health_check_migration.py",
            "--source", str(source), "--candidate", str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "passed": False,
        "error_type": "IntegrityError",
    }
    assert _sha256(source) == source_hash


def test_rehearsal_refuses_to_overwrite_existing_candidate(tmp_path):
    source = tmp_path / "source.db"
    candidate = tmp_path / "existing.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE stable(value TEXT)")
    candidate.write_bytes(b"do-not-overwrite")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/rehearse_vip_health_check_migration.py",
            "--source",
            str(source),
            "--candidate",
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "passed": False,
        "error_type": "FileExistsError",
    }
    assert candidate.read_bytes() == b"do-not-overwrite"
