#!/usr/bin/env python3
"""PID-1 supervisor for one web child and same-container recovery worker.

The worker is started only after the web process is healthy and the shared SQLite
schema is visible. No daemonization/nohup is used. TERM/INT is forwarded to child
process groups; an unexpected enabled-child exit fails the container.
"""
from __future__ import annotations

import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REQUIRED_WORKER_TABLES = {
    "vip_health_check_cases",
    "health_check_refresh_reconciliation",
}


def exact_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def configuration() -> tuple[Path, list[str], list[str] | None]:
    data_dir = Path(os.environ.get("DATA_DIR", "/app/data")).resolve()
    db_path = (data_dir / "user_quota.db").resolve()
    configured_db = os.environ.get("DB_PATH")
    if configured_db and Path(configured_db).resolve() != db_path:
        raise SystemExit("DB_PATH must resolve to DATA_DIR/user_quota.db; refusing split SQLite")

    worker_enabled = exact_true("VIP_HEALTH_CHECK_RECOVERY_WORKER_ENABLED")
    maintenance_enabled = exact_true("VIP_HEALTH_CHECK_MAINTENANCE_ENABLED")
    if worker_enabled and not maintenance_enabled:
        raise SystemExit("worker=true requires maintenance=true")

    web = shlex.split(
        os.environ.get(
            "WEB_COMMAND",
            f"{sys.executable} -m uvicorn server:app --host 0.0.0.0 --port {os.environ.get('PORT', '8000')}",
        )
    )
    worker = None
    if worker_enabled:
        worker = shlex.split(
            os.environ.get(
                "RECOVERY_WORKER_COMMAND",
                f"{sys.executable} health_check_recovery_worker.py --db {db_path} "
                f"--interval {os.environ.get('VIP_HEALTH_CHECK_RECOVERY_INTERVAL_SECONDS', '900')} "
                f"--limit {os.environ.get('VIP_HEALTH_CHECK_RECOVERY_LIMIT', '20')} "
                f"--status-file {data_dir / 'health-check-recovery-status.json'}",
            )
        )
    return db_path, web, worker


def _web_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _worker_schema_ready(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                    tuple(sorted(REQUIRED_WORKER_TABLES)),
                )
            }
        return present == REQUIRED_WORKER_TABLES
    except sqlite3.Error:
        return False


def wait_until_ready(
    web: subprocess.Popen,
    db_path: Path,
    should_stop,
) -> str:
    url = os.environ.get(
        "WEB_READY_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/health"
    )
    try:
        timeout = float(os.environ.get("WEB_READY_TIMEOUT_SECONDS", "120"))
    except ValueError as exc:
        raise SystemExit("WEB_READY_TIMEOUT_SECONDS must be numeric") from exc
    if not 1 <= timeout <= 600:
        raise SystemExit("WEB_READY_TIMEOUT_SECONDS must be between 1 and 600")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop():
            return "stopping"
        code = web.poll()
        if code is not None:
            return f"web_exit:{code}"
        if _web_healthy(url) and _worker_schema_ready(db_path):
            return "ready"
        time.sleep(0.1)
    return "timeout"


def stop(children: dict[str, subprocess.Popen], sig: int, timeout: float = 10.0) -> None:
    for proc in children.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(p.poll() is None for p in children.values()):
        time.sleep(0.05)
    for proc in children.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for proc in children.values():
        proc.wait()


def main() -> int:
    db_path, web_argv, worker_argv = configuration()
    children: dict[str, subprocess.Popen] = {}
    stopping_signal: int | None = None

    def handle(sig: int, _frame: object) -> None:
        nonlocal stopping_signal
        stopping_signal = sig

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    try:
        print("supervisor_start child=web", flush=True)
        children["web"] = subprocess.Popen(web_argv, start_new_session=True)
        if worker_argv is not None:
            readiness = wait_until_ready(
                children["web"], db_path, lambda: stopping_signal is not None
            )
            if readiness == "stopping":
                print(f"supervisor_stop signal={stopping_signal}", flush=True)
                stop(children, stopping_signal or signal.SIGTERM)
                return 0
            if readiness != "ready":
                print(f"supervisor_readiness_failed reason={readiness}", flush=True)
                stop(children, signal.SIGTERM)
                return 1
            print("supervisor_ready web=true sqlite=true", flush=True)
            print("supervisor_start child=recovery-worker", flush=True)
            children["recovery-worker"] = subprocess.Popen(worker_argv, start_new_session=True)

        while True:
            if stopping_signal is not None:
                print(f"supervisor_stop signal={stopping_signal}", flush=True)
                stop(children, stopping_signal)
                return 0
            for name, proc in children.items():
                code = proc.poll()
                if code is not None:
                    print(f"supervisor_unexpected_exit child={name} code={code}", flush=True)
                    stop(children, signal.SIGTERM)
                    return code if code != 0 else 1
            time.sleep(0.1)
    except BaseException:
        stop(children, signal.SIGTERM)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
