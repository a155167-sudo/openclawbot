import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from vip_health_check import (
    configure_vip_health_check_connection,
    create_first_vip_health_check_case,
    ensure_vip_health_check_schema,
)

TW = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 6, 9, 0, tzinfo=TW)


def _db(tmp_path, *, with_case=True, terminal=False, pending=True, log=True):
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    configure_vip_health_check_connection(conn)
    conn.execute("""CREATE TABLE food_logs(
        log_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,consumed_at TEXT NOT NULL,
        meal_slot TEXT DEFAULT '',nutrition_snapshot_json TEXT NOT NULL,
        confirmation_status TEXT NOT NULL DEFAULT 'confirmed',version INTEGER NOT NULL DEFAULT 1,
        deleted_at TEXT NOT NULL DEFAULT '')""")
    ensure_vip_health_check_schema(conn)
    case_id = None
    if with_case:
        case = create_first_vip_health_check_case(
            conn,user_id="U1",first_vip_activation_id="activation-1",
            activation_event_key="event-1",
            activated_at=datetime(2026, 9, 3, 7, 0, tzinfo=TW),
        )
        case_id = case["case_id"]
        if terminal:
            conn.execute("UPDATE vip_health_check_cases SET status='cancelled' WHERE case_id=?",(case_id,))
    if log:
        conn.execute("INSERT INTO food_logs VALUES (?,?,?,?,?,'confirmed',1,'')",(
            "log-1","U1","2026-09-03T08:00:00+08:00","breakfast",'{"calories_kcal":500}'))
    conn.execute("""CREATE TABLE health_check_refresh_reconciliation(
        case_id TEXT PRIMARY KEY,user_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status='pending'),attempts INTEGER NOT NULL DEFAULT 1,
        last_error TEXT NOT NULL,first_failed_at TEXT NOT NULL,last_failed_at TEXT NOT NULL)""")
    if pending and case_id:
        conn.execute("INSERT INTO health_check_refresh_reconciliation VALUES (?,?,'pending',1,'x',?,?)",
                     (case_id,"U1","2026-09-05T00:00:00+08:00","2026-09-05T00:00:00+08:00"))
    conn.commit(); conn.close()
    return path, case_id


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_both_explicit_flags_required_and_disabled_run_has_zero_db_writes(tmp_path):
    from health_check_recovery_worker import run_once
    path, _ = _db(tmp_path)
    before = _digest(path)
    result = run_once(path, maintenance_enabled=False, worker_enabled=True, now=NOW)
    assert result["state"] == "disabled"
    assert _digest(path) == before
    result = run_once(path, maintenance_enabled=True, worker_enabled=False, now=NOW)
    assert result["state"] == "disabled"
    assert _digest(path) == before


def test_pending_existing_case_refreshes_and_clears_exact_debt(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path)
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, limit=5)
    assert result["succeeded"] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM health_check_refresh_reconciliation").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM vip_health_check_source_refs WHERE case_id=?",(case_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM vip_health_check_cases").fetchone()[0] == 1


def test_terminal_debt_is_preserved_and_classified_blocked_without_refresh(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path, terminal=True)
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW)
    assert result["blocked"] == 1
    assert result["state"] == "blocked"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM health_check_refresh_reconciliation WHERE case_id=?",(case_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT state FROM health_check_recovery_state WHERE case_id=?",(case_id,)).fetchone()[0] == "blocked"


def test_low_frequency_fallback_finds_open_case_when_pending_row_was_lost(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path, pending=False)
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, limit=5)
    assert result["fallback_succeeded"] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM vip_health_check_source_refs WHERE case_id=?",(case_id,)).fetchone()[0] == 1


def test_failure_has_bounded_attempts_and_backoff(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path)
    conn = sqlite3.connect(path); conn.execute("ALTER TABLE food_logs RENAME TO broken_food_logs"); conn.commit(); conn.close()
    first = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=2)
    second = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=2)
    assert first["failed"] == 1
    assert second["deferred"] == 1
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT state,attempts FROM health_check_recovery_state WHERE case_id=?",(case_id,)).fetchone()
    assert row == ("backoff", 1)


def test_dry_run_and_no_case_never_create_or_refresh_case(tmp_path):
    from health_check_recovery_worker import run_once
    path, _ = _db(tmp_path, with_case=False, pending=False)
    before = _digest(path)
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, dry_run=True)
    assert result["state"] == "dry_run"
    assert _digest(path) == before
    live = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW)
    assert live["succeeded"] == 0
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM vip_health_check_cases").fetchone()[0] == 0


def test_status_file_is_atomic_summary_without_error_or_identity(tmp_path):
    from health_check_recovery_worker import run_once
    path, _ = _db(tmp_path)
    status = tmp_path / "status.json"
    run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, status_path=status)
    payload = json.loads(status.read_text())
    assert set(payload) >= {"state","pending_count","oldest_pending_at","error_count","last_run_at"}
    text = status.read_text()
    assert "U1" not in text and "last_error" not in text
    assert not (tmp_path / "status.json.tmp").exists()


def test_fallback_failure_is_backed_off_not_retried_forever(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path, pending=False)
    conn = sqlite3.connect(path); conn.execute("ALTER TABLE food_logs RENAME TO broken_food_logs"); conn.commit(); conn.close()
    first = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=2)
    second = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=2)
    assert first["failed"] == 1
    assert second["failed"] == 0
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT state,attempts FROM health_check_recovery_state WHERE case_id=?",(case_id,)).fetchone() == ("backoff", 1)


def test_late_debt_version_change_is_not_cleared(tmp_path, monkeypatch):
    import health_check_recovery_worker as worker
    path, case_id = _db(tmp_path)
    real = worker.refresh_user_health_check_case
    def refresh_and_replace_debt(conn, **kwargs):
        result = real(conn, **kwargs)
        conn.execute("UPDATE health_check_refresh_reconciliation SET last_failed_at='2026-09-06T08:59:59+08:00' WHERE case_id=?", (case_id,))
        return result
    monkeypatch.setattr(worker, "refresh_user_health_check_case", refresh_and_replace_debt)
    result = worker.run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW)
    assert result["failed"] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM health_check_refresh_reconciliation WHERE case_id=?",(case_id,)).fetchone()[0] == 1


def test_standalone_import_never_imports_server_startup():
    probe = subprocess.run(
        [sys.executable, "-c", "import sys,health_check_recovery_worker; print('server' in sys.modules)"],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "False"


def test_new_debt_generation_restarts_attempts_after_prior_generation_exhausted(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE food_logs RENAME TO broken_food_logs")
    conn.commit(); conn.close()
    first = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=1)
    assert first["state"] == "exhausted"
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE broken_food_logs RENAME TO food_logs")
    conn.execute("UPDATE health_check_refresh_reconciliation SET attempts=attempts+1,last_failed_at=? WHERE case_id=?",
                 ("2026-09-06T09:05:00+08:00", case_id))
    conn.commit(); conn.close()
    second = run_once(path, maintenance_enabled=True, worker_enabled=True,
                      now=NOW + timedelta(minutes=10), max_attempts=1)
    assert second["succeeded"] == 1
    assert second["state"] == "ok"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM health_check_refresh_reconciliation").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM vip_health_check_source_refs WHERE case_id=?", (case_id,)).fetchone()[0] == 1


def test_same_exhausted_debt_generation_is_not_retried(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE food_logs RENAME TO broken_food_logs")
    conn.commit(); conn.close()
    first = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=1)
    second = run_once(path, maintenance_enabled=True, worker_enabled=True,
                      now=NOW + timedelta(hours=1), max_attempts=1)
    assert first["failed"] == 1
    assert second["examined"] == 0
    assert second["state"] == "exhausted"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT state,attempts FROM health_check_recovery_state WHERE case_id=?", (case_id,)).fetchone() == ("exhausted", 1)


def test_failed_pending_run_reports_degraded_not_ok(tmp_path):
    from health_check_recovery_worker import run_once
    path, _ = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE food_logs RENAME TO broken_food_logs")
    conn.commit(); conn.close()
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, max_attempts=2)
    assert result["state"] == "degraded"
    assert result["failed"] == 1
    assert result["pending_count"] == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_malformed_reconciliation_schema_fails_closed_with_coarse_status_and_zero_db_writes(tmp_path, dry_run):
    from health_check_recovery_worker import run_once
    path, _ = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE health_check_refresh_reconciliation")
    conn.execute("CREATE TABLE health_check_refresh_reconciliation(case_id TEXT PRIMARY KEY)")
    conn.commit(); conn.close()
    before = _digest(path)
    status = tmp_path / "status.json"
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW,
                      dry_run=dry_run, status_path=status)
    assert result["state"] == "fatal"
    assert result["error_code"] == "reconciliation_schema"
    assert result["error_count"] == 1
    assert _digest(path) == before
    payload = json.loads(status.read_text())
    assert payload == result
    text = status.read_text()
    assert "U1" not in text
    assert "no such column" not in text
    assert "last_error" not in text


def test_missing_reconciliation_table_still_uses_bounded_existing_case_fallback(tmp_path):
    from health_check_recovery_worker import run_once
    path, case_id = _db(tmp_path, pending=False)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE health_check_refresh_reconciliation")
    conn.commit(); conn.close()
    result = run_once(path, maintenance_enabled=True, worker_enabled=True, now=NOW, limit=1)
    assert result["fallback_succeeded"] == 1
    assert result["examined"] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM vip_health_check_source_refs WHERE case_id=?", (case_id,)).fetchone()[0] == 1


def test_cli_exits_nonzero_immediately_on_fatal_even_in_interval_mode(tmp_path):
    path, _ = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE health_check_refresh_reconciliation")
    conn.execute("CREATE TABLE health_check_refresh_reconciliation(case_id TEXT PRIMARY KEY)")
    conn.commit(); conn.close()
    status = tmp_path / "status.json"
    env = os.environ.copy()
    env["VIP_HEALTH_CHECK_MAINTENANCE_ENABLED"] = "true"
    env["VIP_HEALTH_CHECK_RECOVERY_WORKER_ENABLED"] = "true"
    probe = subprocess.run(
        [sys.executable, "health_check_recovery_worker.py", "--db", str(path),
         "--interval", "9999", "--status-file", str(status)],
        env=env, capture_output=True, text=True, timeout=5,
    )
    assert probe.returncode != 0
    assert json.loads(probe.stdout)["state"] == "fatal"
    assert json.loads(status.read_text())["state"] == "fatal"


def test_cli_boundary_converts_unexpected_exception_to_coarse_fatal_status(tmp_path, monkeypatch, capsys):
    import health_check_recovery_worker as worker
    status = tmp_path / "status.json"
    monkeypatch.setenv("VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", "true")
    monkeypatch.setenv("VIP_HEALTH_CHECK_RECOVERY_WORKER_ENABLED", "true")
    def crash(*args, **kwargs):
        raise RuntimeError("UID-U1 token-secret should never escape")
    monkeypatch.setattr(worker, "run_once", crash)
    assert worker.main(["--db", str(tmp_path / "unused.db"),
                        "--status-file", str(status)]) == 1
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["state"] == "fatal"
    assert payload["error_code"] == "worker_exception"
    assert json.loads(status.read_text()) == payload
    assert "UID-U1" not in stdout and "token-secret" not in stdout
