import copy
import hashlib
import json
import sqlite3

import pytest

import nutrition_system
from meal_photo_system import (
    apply_meal_photo_action,
    cancel_meal_photo_revision_draft,
    create_meal_photo_revision_draft,
    get_meal_photo_draft,
    save_meal_photo_draft,
)
from nutrition_system import (
    confirm_user_meal_photo_revision,
    user_confirmed_meal_photo_estimate_is_valid,
    user_confirmed_meal_photo_trust_projection,
)
from test_meal_photo_system import ai_estimated_payload


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _ensure_revision_test_schema(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
    for name, definition in {
        "version": "INTEGER NOT NULL DEFAULT 1",
        "deleted_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE food_logs ADD COLUMN {name} {definition}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_food_log_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            log_id TEXT NOT NULL,
            action TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()


def _confirmed_log(conn, user_id="U1"):
    token = save_meal_photo_draft(
        conn, user_id=user_id, source_message_id=f"PHOTO-{user_id}",
        payload=ai_estimated_payload(), source_image_ref="nutrition-image:original.jpg",
        meal_slot="午餐", consumed_at="2026-09-13T12:00:00+08:00",
        workflow_version="user_confirmed_ai_nutrition_v2",
    )
    draft = get_meal_photo_draft(conn, user_id=user_id, token=token)
    confirmed = apply_meal_photo_action(
        conn, event_id=f"CONFIRM-{user_id}", user_id=user_id, token=token,
        expected_version=draft["version"], action="confirm_estimate",
    )
    _ensure_revision_test_schema(conn)
    return token, confirmed["result"]["log_id"]


def _revised_estimate(conn, log_id, *, calories=510.0, protein=31.0):
    estimate = json.loads(conn.execute(
        "SELECT exchange_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
    ).fetchone()[0])
    estimate["calories_kcal"] = calories
    estimate["protein_g"] = protein
    estimate["calories_kcal_range"] = {
        "min": calories - 60, "max": calories + 70, "basis": "ai_vision_estimate_range_v1",
    }
    estimate["protein_g_range"] = {
        "min": protein - 4, "max": protein + 5, "basis": "ai_vision_estimate_range_v1",
    }
    estimate["estimate_items"] = [
        {"name": "測試修正版餐點", "portion": "單元測試 mock", "calories_kcal": calories, "protein_g": protein}
    ]
    return estimate


def _revision_draft(conn, log_id, *, from_version=1, request="白飯改半碗", calories=510.0, protein=31.0):
    return create_meal_photo_revision_draft(
        conn, user_id="U1", log_id=log_id, from_version=from_version,
        request_text=request, estimate=_revised_estimate(
            conn, log_id, calories=calories, protein=protein,
        ),
    )


def test_revision_confirm_v1_to_v2_to_v3_keeps_genesis_immutable_and_validates_full_chain(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(nutrition_system, "utcish_now", lambda: "2026-09-13T12:00:00+08:00")
    with sqlite3.connect(tmp_path / "revision-chain.db") as conn:
        original_token, log_id = _confirmed_log(conn)
        original_log = conn.execute(
            "SELECT trust_payload_json,trust_hash,food_id,consumed_at FROM food_logs WHERE log_id=?",
            (log_id,),
        ).fetchone()
        original_draft = conn.execute(
            "SELECT * FROM pending_meal_photo_drafts WHERE token=?", (original_token,)
        ).fetchone()
        original_event = conn.execute(
            "SELECT * FROM meal_photo_events WHERE event_id='CONFIRM-U1'"
        ).fetchone()

        draft2 = _revision_draft(conn, log_id)
        result2 = confirm_user_meal_photo_revision(
            conn, event_id="Z-EVENT", user_id="U1", log_id=log_id,
            from_version=1, draft_token=draft2["token"],
        )
        draft3 = _revision_draft(
            conn, log_id, from_version=2, request="再加一顆蛋", calories=590.0, protein=38.0,
        )
        result3 = confirm_user_meal_photo_revision(
            conn, event_id="A-EVENT", user_id="U1", log_id=log_id,
            from_version=2, draft_token=draft3["token"],
        )

        assert (result2["from_version"], result2["to_version"]) == (1, 2)
        assert (result3["from_version"], result3["to_version"]) == (2, 3)
        assert result2["previous_revision_hash"] == original_log[1]
        assert result3["previous_revision_hash"] == result2["revision_hash"]
        assert conn.execute(
            "SELECT trust_payload_json,trust_hash,food_id,consumed_at,version FROM food_logs WHERE log_id=?",
            (log_id,),
        ).fetchone() == (*original_log, 3)
        assert conn.execute(
            "SELECT * FROM pending_meal_photo_drafts WHERE token=?", (original_token,)
        ).fetchone() == original_draft
        assert conn.execute(
            "SELECT * FROM meal_photo_events WHERE event_id='CONFIRM-U1'"
        ).fetchone() == original_event
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        projection = user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )
        assert projection["integrity_status"] == "verified"
        assert projection["log_version"] == 3
        assert projection["effective_revision_hash"] == result3["revision_hash"]
        assert projection["nutrition"] == {"calories_kcal": 590.0, "protein_g": 38.0}


@pytest.mark.parametrize("bad_from,bad_to", [(1, 3), (3, 4), ("2", 3), (2, True)])
def test_revision_reader_rejects_branch_gap_and_untyped_versions(tmp_path, bad_from, bad_to):
    with sqlite3.connect(tmp_path / f"revision-invalid-chain-{bad_from}-{bad_to}.db") as conn:
        _, log_id = _confirmed_log(conn)
        draft2 = _revision_draft(conn, log_id)
        confirm_user_meal_photo_revision(
            conn, event_id="CHAIN-2", user_id="U1", log_id=log_id,
            from_version=1, draft_token=draft2["token"],
        )
        draft3 = _revision_draft(
            conn, log_id, from_version=2, request="再加蛋", calories=590, protein=38,
        )
        confirm_user_meal_photo_revision(
            conn, event_id="CHAIN-3", user_id="U1", log_id=log_id,
            from_version=2, draft_token=draft3["token"],
        )
        envelope = json.loads(conn.execute(
            "SELECT result_json FROM daily_food_log_events WHERE event_id='CHAIN-3'"
        ).fetchone()[0])
        envelope["from_version"] = bad_from
        envelope["to_version"] = bad_to
        envelope["revision_hash"] = _canonical_hash({
            key: value for key, value in envelope.items() if key != "revision_hash"
        })
        conn.execute("DROP TRIGGER daily_food_revision_events_no_update")
        conn.execute(
            "UPDATE daily_food_log_events SET result_json=? WHERE event_id='CHAIN-3'",
            (json.dumps(envelope, ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()

        assert not user_confirmed_meal_photo_estimate_is_valid(conn, log_id)


def test_revision_confirmation_is_cas_idempotent_and_preserves_processing_outbox(tmp_path):
    with sqlite3.connect(tmp_path / "revision-cas.db") as conn:
        _, log_id = _confirmed_log(conn)
        draft = _revision_draft(conn, log_id)
        conn.execute(
            """UPDATE nutrition_sheet_outbox SET status='processing',attempts=4,last_error='old',
               claimed_at='lease-time',lease_owner='worker-1',resync_required=0
               WHERE entity_type='food_log' AND entity_id=?""", (log_id,),
        )
        conn.commit()
        other = create_meal_photo_revision_draft(
            conn, user_id="U1", log_id=log_id, from_version=1,
            request_text="競態草稿", estimate=_revised_estimate(conn, log_id, calories=520, protein=32),
        )
        first = confirm_user_meal_photo_revision(
            conn, event_id="REVISION-REPLAY", user_id="U1", log_id=log_id,
            from_version=1, draft_token=draft["token"],
        )
        replay = confirm_user_meal_photo_revision(
            conn, event_id="REVISION-REPLAY", user_id="U1", log_id=log_id,
            from_version=1, draft_token=draft["token"],
        )
        assert replay == {**first, "replayed": True}
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE action='confirm_ai_revision'"
        ).fetchone()[0] == 1
        assert conn.execute(
            """SELECT status,attempts,last_error,claimed_at,lease_owner,resync_required
               FROM nutrition_sheet_outbox WHERE entity_type='food_log' AND entity_id=?""",
            (log_id,),
        ).fetchone() == ("processing", 4, "old", "lease-time", "worker-1", 1)

        with pytest.raises(ValueError, match="已更新"):
            confirm_user_meal_photo_revision(
                conn, event_id="REVISION-STALE", user_id="U1", log_id=log_id,
                from_version=1, draft_token=other["token"],
            )
        assert conn.execute("SELECT version FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0] == 2


def test_revision_cancel_changes_only_revision_draft(tmp_path):
    with sqlite3.connect(tmp_path / "revision-cancel.db") as conn:
        _, log_id = _confirmed_log(conn)
        before = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
        draft = _revision_draft(conn, log_id)
        cancelled = cancel_meal_photo_revision_draft(
            conn, user_id="U1", token=draft["token"], expected_version=draft["version"],
        )
        assert cancelled["status"] == "cancelled"
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE action='confirm_ai_revision'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("attack", ["revision_hash", "previous_revision_hash", "source", "owner", "after_state"])
def test_revision_reader_rejects_chain_tamper(tmp_path, attack):
    with sqlite3.connect(tmp_path / f"revision-tamper-{attack}.db") as conn:
        _, log_id = _confirmed_log(conn)
        draft = _revision_draft(conn, log_id)
        confirm_user_meal_photo_revision(
            conn, event_id="REVISION-TAMPER", user_id="U1", log_id=log_id,
            from_version=1, draft_token=draft["token"],
        )
        envelope = json.loads(conn.execute(
            "SELECT result_json FROM daily_food_log_events WHERE event_id='REVISION-TAMPER'"
        ).fetchone()[0])
        if attack == "revision_hash":
            envelope["revision_hash"] = "0" * 64
        elif attack == "previous_revision_hash":
            envelope["previous_revision_hash"] = "1" * 64
        elif attack == "source":
            envelope["estimate_provenance"]["source"] = "fabricated"
            envelope["revision_hash"] = _canonical_hash({k: v for k, v in envelope.items() if k != "revision_hash"})
        elif attack == "owner":
            envelope["user_id"] = "U2"
            envelope["revision_hash"] = _canonical_hash({k: v for k, v in envelope.items() if k != "revision_hash"})
        else:
            envelope["after_state"]["nutrition_snapshot"]["calories_kcal"] = 1
            envelope["revision_hash"] = _canonical_hash({k: v for k, v in envelope.items() if k != "revision_hash"})
        conn.execute("DROP TRIGGER daily_food_revision_events_no_update")
        conn.execute(
            "UPDATE daily_food_log_events SET result_json=? WHERE event_id='REVISION-TAMPER'",
            (json.dumps(envelope, ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()
        assert not user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        assert user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )["integrity_status"] == "integrity_verification_failed"


def test_revision_event_is_append_only_and_late_outbox_failure_rolls_back_everything(tmp_path):
    with sqlite3.connect(tmp_path / "revision-atomic.db") as conn:
        _, log_id = _confirmed_log(conn)
        draft = _revision_draft(conn, log_id)
        before_log = conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
        before_draft = conn.execute(
            "SELECT * FROM pending_meal_photo_drafts WHERE token=?", (draft["token"],)
        ).fetchone()
        conn.execute("""CREATE TRIGGER reject_revision_outbox BEFORE UPDATE ON nutrition_sheet_outbox
                        BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END""")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
            confirm_user_meal_photo_revision(
                conn, event_id="REVISION-ROLLBACK", user_id="U1", log_id=log_id,
                from_version=1, draft_token=draft["token"],
            )
        assert conn.execute("SELECT * FROM food_logs WHERE log_id=?", (log_id,)).fetchone() == before_log
        assert conn.execute(
            "SELECT * FROM pending_meal_photo_drafts WHERE token=?", (draft["token"],)
        ).fetchone() == before_draft
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE event_id='REVISION-ROLLBACK'"
        ).fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_revision_outbox")
        result = confirm_user_meal_photo_revision(
            conn, event_id="REVISION-ROLLBACK", user_id="U1", log_id=log_id,
            from_version=1, draft_token=draft["token"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE daily_food_log_events SET result_json='{}' WHERE event_id='REVISION-ROLLBACK'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM daily_food_log_events WHERE event_id='REVISION-ROLLBACK'")
        assert result["to_version"] == 2


def test_revision_rejects_foreign_owner_and_deleted_log_without_side_effects(tmp_path):
    with sqlite3.connect(tmp_path / "revision-owner-delete.db") as conn:
        _, log_id = _confirmed_log(conn)
        with pytest.raises(ValueError, match="找不到"):
            create_meal_photo_revision_draft(
                conn, user_id="U2", log_id=log_id, from_version=1,
                request_text="竄改", estimate=_revised_estimate(conn, log_id),
            )
        draft = _revision_draft(conn, log_id)
        conn.execute(
            "UPDATE food_logs SET deleted_at='2026-09-13T13:00:00+08:00',confirmation_status='deleted' WHERE log_id=?",
            (log_id,),
        )
        conn.commit()
        with pytest.raises(ValueError, match="找不到"):
            confirm_user_meal_photo_revision(
                conn, event_id="REVISION-DELETED", user_id="U1", log_id=log_id,
                from_version=1, draft_token=draft["token"],
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE action='confirm_ai_revision'"
        ).fetchone()[0] == 0
