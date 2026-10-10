#!/usr/bin/env python3
"""Offline rehearsal of candidate nutrition v7 over the pinned Git predecessor."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from nutrition_system import ensure_nutrition_schema


SOURCE_COMMIT = "a46b9c6c89f4ecdd0b7c22f5f767f5663a24aa05"
DEFAULT_SOURCE_GIT_DIR = Path("/home/win-xi/.hermes/workspace/openclawbot/.git")


def _git_bytes(git_dir: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *arguments],
        check=True,
        capture_output=True,
    ).stdout


def _load_predecessor_nutrition(source: bytes, directory: Path) -> ModuleType:
    module_path = directory / "nutrition_system.py"
    module_path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("pinned_v6_nutrition_system", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load pinned predecessor nutrition module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_predecessor_ledger_schema(source: bytes, predecessor: ModuleType):
    tree = ast.parse(source, filename=f"{SOURCE_COMMIT}:server.py")
    function = next(
        (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "ensure_daily_food_ledger_schema"
        ),
        None,
    )
    if function is None:
        raise RuntimeError("pinned predecessor ledger schema generator not found")
    namespace = {
        "sqlite3": sqlite3,
        "ensure_nutrition_schema": predecessor.ensure_nutrition_schema,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE_COMMIT), "exec"), namespace)
    return namespace["ensure_daily_food_ledger_schema"]


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def rehearse(source_git_dir: Path) -> dict[str, object]:
    resolved_commit = _git_bytes(
        source_git_dir, "rev-parse", f"{SOURCE_COMMIT}^{{commit}}",
    ).decode().strip()
    if resolved_commit != SOURCE_COMMIT:
        raise RuntimeError("source Git repository did not resolve the pinned predecessor commit")
    source_tree = _git_bytes(
        source_git_dir, "rev-parse", f"{SOURCE_COMMIT}^{{tree}}",
    ).decode().strip()
    nutrition_source = _git_bytes(
        source_git_dir, "show", f"{SOURCE_COMMIT}:nutrition_system.py",
    )
    server_source = _git_bytes(
        source_git_dir, "show", f"{SOURCE_COMMIT}:server.py",
    )

    with tempfile.TemporaryDirectory(prefix="meal-slot-v7-git-predecessor-") as directory:
        temp_dir = Path(directory)
        predecessor = _load_predecessor_nutrition(nutrition_source, temp_dir)
        predecessor_ledger_schema = _load_predecessor_ledger_schema(server_source, predecessor)
        db_path = temp_dir / "git-generated-v6.db"
        with sqlite3.connect(db_path) as conn:
            predecessor_ledger_schema(conn)
            predecessor_version = conn.execute(
                "SELECT version FROM nutrition_schema_versions WHERE component='nutrition_system'"
            ).fetchone()[0]
            if predecessor_version != 6:
                raise AssertionError(f"unexpected predecessor marker: {predecessor_version}")
            conn.execute(
                "INSERT INTO daily_food_log_events VALUES (?,?,?,?,?,?)",
                ("PRE-V7-EVENT", "synthetic-user", "synthetic-log", "legacy_probe", "{}", "v6"),
            )
            conn.commit()

            ensure_nutrition_schema(conn)
            first = schema_fingerprint(conn)
            ensure_nutrition_schema(conn)
            second = schema_fingerprint(conn)
            assert first == second
            assert conn.execute(
                "SELECT version FROM nutrition_schema_versions WHERE component='nutrition_system'"
            ).fetchone() == (7,)
            preexisting_event_retained = conn.execute(
                "SELECT action,result_json,created_at FROM daily_food_log_events WHERE event_id=?",
                ("PRE-V7-EVENT",),
            ).fetchone() == ("legacy_probe", "{}", "v6")
            assert preexisting_event_retained

            trigger_names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            expected_triggers = {
                "daily_food_revision_events_no_update",
                "daily_food_revision_events_no_delete",
                "daily_food_meal_slot_revision_events_no_update",
                "daily_food_meal_slot_revision_events_no_delete",
            }
            assert expected_triggers <= trigger_names
            append_only_probes = 0
            for action in ("confirm_ai_revision", "confirm_meal_slot_revision"):
                event_id = f"probe-{action}"
                conn.execute(
                    "INSERT INTO daily_food_log_events VALUES (?,?,?,?,?,?)",
                    (event_id, "synthetic-user", "synthetic-log", action, "{}", "synthetic-time"),
                )
                for statement in (
                    "UPDATE daily_food_log_events SET result_json='[]' WHERE event_id=?",
                    "DELETE FROM daily_food_log_events WHERE event_id=?",
                ):
                    try:
                        conn.execute(statement, (event_id,))
                    except sqlite3.IntegrityError as exc:
                        assert "append-only" in str(exc)
                        append_only_probes += 1
                    else:
                        raise AssertionError(
                            f"append-only probe unexpectedly succeeded: {action}"
                        )
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            assert integrity == "ok"
            assert foreign_keys == []

    return {
        "passed": True,
        "fixture": "git-generated-predecessor-v6",
        "source_commit": resolved_commit,
        "source_tree": source_tree,
        "nutrition_source_sha256": hashlib.sha256(nutrition_source).hexdigest(),
        "server_source_sha256": hashlib.sha256(server_source).hexdigest(),
        "predecessor_generators": [
            "nutrition_system.ensure_nutrition_schema",
            "server.ensure_daily_food_ledger_schema",
        ],
        "predecessor_version": predecessor_version,
        "version": 7,
        "preexisting_event_retained": preexisting_event_retained,
        "trigger_count": len(expected_triggers),
        "append_only_probes": append_only_probes,
        "idempotent_schema_sha256": second,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "production_services_used": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-git-dir",
        type=Path,
        default=DEFAULT_SOURCE_GIT_DIR,
        help="read-only Git object database containing the pinned predecessor commit",
    )
    arguments = parser.parse_args()
    print(json.dumps(rehearse(arguments.source_git_dir), sort_keys=True))


if __name__ == "__main__":
    main()
