import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from meal_photo_system import ensure_meal_photo_schema
from nutrition_system import (
    _ensure_food_log_revision_contract,
    confirm_user_meal_photo_revision,
    ensure_nutrition_schema,
    user_confirmed_meal_photo_trust_projection,
)
from test_food_log_revisions import _confirmed_log, _revision_draft


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
SOURCE_GIT_DIR = Path("/home/win-xi/.hermes/workspace/openclawbot/.git")


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _append_legal_slot_revision(conn, *, log_id, event_id, new_slot):
    """Persist one contract-valid metadata edge without invoking the not-yet-built writer."""
    projection = user_confirmed_meal_photo_trust_projection(
        conn, log_id, "user_confirmed_ai_estimate",
    )
    assert projection["integrity_status"] == "verified"
    state = projection["state"] if "state" in projection else {
        "food_id": conn.execute("SELECT food_id FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0],
        "consumed_at": conn.execute("SELECT consumed_at FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0],
        "meal_slot": conn.execute("SELECT meal_slot FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0],
        "consumed_servings": conn.execute("SELECT consumed_servings FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0],
        "exchange_snapshot": projection["estimate"],
        "nutrition_snapshot": projection["nutrition"],
    }
    before_slot = state["meal_slot"]
    assert before_slot != new_slot
    from_version = projection["log_version"]
    envelope = {
        "schema_version": "confirmed-food-log-meal-slot-revision-v1",
        "event_id": event_id,
        "user_id": "U1",
        "log_id": log_id,
        "from_version": from_version,
        "to_version": from_version + 1,
        "original_confirmation_hash": projection["original_confirmation_hash"],
        "previous_revision_hash": projection["effective_revision_hash"],
        "before_state_hash": _canonical_hash(state),
        "before_meal_slot": before_slot,
        "after_meal_slot": new_slot,
        "reason_code": "user_corrected_meal_slot",
        "created_at": "2026-09-14T07:00:00+08:00",
    }
    envelope["revision_hash"] = _canonical_hash(envelope)
    conn.execute(
        """INSERT INTO daily_food_log_events
           (event_id,user_id,log_id,action,result_json,created_at)
           VALUES (?,?,?,'confirm_meal_slot_revision',?,?)""",
        (event_id, "U1", log_id, json.dumps(envelope, ensure_ascii=False, sort_keys=True), envelope["created_at"]),
    )
    conn.execute(
        "UPDATE food_logs SET meal_slot=?,version=version+1 WHERE log_id=? AND version=?",
        (new_slot, log_id, from_version),
    )
    conn.commit()
    return envelope


def _trigger_sql(conn):
    return dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall())


def test_literal_v6_migrates_to_v7_twice_and_both_typed_events_are_append_only(tmp_path):
    db = tmp_path / "literal-v6.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE nutrition_schema_versions (
                component TEXT PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL
            );
            INSERT INTO nutrition_schema_versions VALUES ('nutrition_system', 6, 'v6');
            CREATE TABLE daily_food_log_events (
                event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, log_id TEXT NOT NULL,
                action TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            CREATE TRIGGER daily_food_revision_events_no_update
            BEFORE UPDATE ON daily_food_log_events
            WHEN OLD.action='confirm_ai_revision'
            BEGIN SELECT RAISE(ABORT, 'confirm_ai_revision events are append-only'); END;
            CREATE TRIGGER daily_food_revision_events_no_delete
            BEFORE DELETE ON daily_food_log_events
            WHEN OLD.action='confirm_ai_revision'
            BEGIN SELECT RAISE(ABORT, 'confirm_ai_revision events are append-only'); END;
        """)
        ensure_nutrition_schema(conn)
        assert conn.execute(
            "SELECT version FROM nutrition_schema_versions WHERE component='nutrition_system'"
        ).fetchone() == (7,)
        first = _trigger_sql(conn)
        assert set(first) >= {
            "daily_food_revision_events_no_update",
            "daily_food_revision_events_no_delete",
            "daily_food_meal_slot_revision_events_no_update",
            "daily_food_meal_slot_revision_events_no_delete",
        }
        ensure_nutrition_schema(conn)
        assert _trigger_sql(conn) == first

        for action in ("confirm_ai_revision", "confirm_meal_slot_revision"):
            event_id = f"EVENT-{action}"
            conn.execute(
                "INSERT INTO daily_food_log_events VALUES (?,?,?,?,?,?)",
                (event_id, "U1", "LOG1", action, "{}", "now"),
            )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE daily_food_log_events SET result_json='[]' WHERE event_id=?", (event_id,))
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM daily_food_log_events WHERE event_id=?", (event_id,))


def test_rehearsal_uses_pinned_git_predecessor_schema_generator():
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_ROOT / "scripts/rehearse_meal_slot_revision_v7.py"),
            "--source-git-dir",
            str(SOURCE_GIT_DIR),
        ],
        cwd=RUNTIME_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["passed"] is True
    assert evidence["fixture"] == "git-generated-predecessor-v6"
    assert evidence["source_commit"] == "a46b9c6c89f4ecdd0b7c22f5f767f5663a24aa05"
    assert evidence["predecessor_version"] == 6
    assert evidence["version"] == 7
    assert evidence["preexisting_event_retained"] is True


def test_v7_marker_does_not_hide_tampered_trigger(tmp_path):
    with sqlite3.connect(tmp_path / "tampered-v7.db") as conn:
        ensure_meal_photo_schema(conn)
        ensure_nutrition_schema(conn)
        conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        conn.execute("DROP TRIGGER daily_food_meal_slot_revision_events_no_update")
        conn.execute("""CREATE TRIGGER daily_food_meal_slot_revision_events_no_update
                        BEFORE UPDATE ON daily_food_log_events
                        WHEN OLD.action='confirm_ai_revision'
                        BEGIN SELECT RAISE(ABORT, 'wrong action'); END""")
        conn.commit()
        with pytest.raises(RuntimeError, match="trigger contract"):
            ensure_nutrition_schema(conn)
        with pytest.raises(RuntimeError, match="trigger contract"):
            _ensure_food_log_revision_contract(conn)


@pytest.mark.parametrize("trigger_name", (
    "daily_food_revision_events_no_update",
    "daily_food_revision_events_no_delete",
    "daily_food_meal_slot_revision_events_no_update",
    "daily_food_meal_slot_revision_events_no_delete",
))
@pytest.mark.parametrize("tamper", (
    "action_literal_case",
    "literal_internal_whitespace",
    "escaped_quote_literal",
    "when_clause",
))
def test_v7_trigger_verification_rejects_literal_and_when_tampering(
    tmp_path, trigger_name, tamper,
):
    with sqlite3.connect(tmp_path / f"{tamper}-{trigger_name}.db") as conn:
        ensure_nutrition_schema(conn)
        original = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()[0]
        action = (
            "confirm_meal_slot_revision"
            if "meal_slot" in trigger_name else "confirm_ai_revision"
        )
        if tamper == "action_literal_case":
            changed = original.replace(f"'{action}'", f"'{action.upper()}'", 1)
        elif tamper == "literal_internal_whitespace":
            changed = original.replace(" events are append-only'", " events  are append-only'", 1)
        elif tamper == "escaped_quote_literal":
            changed = original.replace(" events are append-only'", " events ''are append-only'", 1)
        else:
            changed = original.replace(f"OLD.action='{action}'", f"OLD.action='{action}_tampered'", 1)
        assert changed != original
        conn.execute(f'DROP TRIGGER "{trigger_name}"')
        conn.execute(changed)
        conn.commit()

        with pytest.raises(RuntimeError, match=trigger_name):
            ensure_nutrition_schema(conn)


def test_mixed_typed_chain_uses_genesis_then_alternating_edges(tmp_path):
    with sqlite3.connect(tmp_path / "mixed.db") as conn:
        _, log_id = _confirmed_log(conn)
        genesis = json.loads(conn.execute(
            "SELECT trust_payload_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0])

        slot2 = _append_legal_slot_revision(conn, log_id=log_id, event_id="Z-SLOT-2", new_slot="晚餐")
        draft3 = _revision_draft(conn, log_id, from_version=2, calories=520, protein=32)
        nutrition3 = confirm_user_meal_photo_revision(
            conn, event_id="A-NUTRITION-3", user_id="U1", log_id=log_id,
            from_version=2, draft_token=draft3["token"],
        )
        slot4 = _append_legal_slot_revision(conn, log_id=log_id, event_id="Z-SLOT-4", new_slot="點心")
        draft5 = _revision_draft(conn, log_id, from_version=4, calories=610, protein=41)
        nutrition5 = confirm_user_meal_photo_revision(
            conn, event_id="A-NUTRITION-5", user_id="U1", log_id=log_id,
            from_version=4, draft_token=draft5["token"],
        )

        projection = user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )
        assert projection["integrity_status"] == "verified"
        assert projection["log_version"] == 5
        assert projection["effective_revision_hash"] == nutrition5["revision_hash"]
        assert projection["original_confirmation_hash"] == _canonical_hash(genesis)
        assert conn.execute(
            "SELECT meal_slot,consumed_at,version FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone() == ("點心", genesis["consumed_at"], 5)
        assert slot2["previous_revision_hash"] == projection["original_confirmation_hash"]
        assert nutrition3["previous_revision_hash"] == slot2["revision_hash"]
        assert slot4["previous_revision_hash"] == nutrition3["revision_hash"]
        assert nutrition5["previous_revision_hash"] == slot4["revision_hash"]


def test_reader_rejects_unknown_revision_action_and_current_row_drift(tmp_path):
    with sqlite3.connect(tmp_path / "unknown.db") as conn:
        _, log_id = _confirmed_log(conn)
        conn.execute(
            "INSERT INTO daily_food_log_events VALUES (?,?,?,?,?,?)",
            ("UNKNOWN", "U1", log_id, "confirm_future_revision", "{}", "now"),
        )
        conn.commit()
        assert user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )["integrity_status"] == "integrity_verification_failed"

    with sqlite3.connect(tmp_path / "drift.db") as conn:
        _, log_id = _confirmed_log(conn)
        _append_legal_slot_revision(conn, log_id=log_id, event_id="SLOT", new_slot="晚餐")
        conn.execute("UPDATE food_logs SET consumed_at='2026-09-15T01:00:00+08:00' WHERE log_id=?", (log_id,))
        conn.commit()
        assert user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )["integrity_status"] == "integrity_verification_failed"
