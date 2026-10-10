import hashlib
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import server
from dietitian_health_check_api import load_health_check_detail, load_health_check_image
from meal_photo_system import (
    apply_meal_photo_action,
    create_meal_photo_revision_draft,
    get_meal_photo_draft,
    save_meal_photo_draft,
)
from nutrition_system import daily_food_summary, user_confirmed_meal_photo_trust_projection
from test_dietitian_health_check_image import _jpeg
from test_meal_photo_system import ai_estimated_payload
from vip_health_check import (
    configure_vip_health_check_connection,
    create_first_vip_health_check_case,
    ensure_vip_health_check_schema,
    refresh_user_health_check_case,
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_revision_wrapper_replay_refreshes_real_consumers_without_duplicate_writes(
    tmp_path, monkeypatch,
):
    db = tmp_path / "revision-consumers.db"
    image_root = tmp_path / "nutrition_images"
    image_root.mkdir()
    image_leaf = "a" * 32 + ".jpg"
    image_ref = "nutrition-image:" + image_leaf
    (image_root / image_leaf).write_bytes(_jpeg())
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    server.init_db()
    now = server.tw_now().replace(microsecond=0)

    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="U1", first_vip_activation_id="revision-activation",
            activation_event_key="revision-activation-event",
            activated_at=now - server.timedelta(minutes=5),
        )
        conn.execute(
            "INSERT OR IGNORE INTO health_profile "
            "(user_id,today_extra_cal,today_extra_pro,today_food_items,today_date,tdee,protein) "
            "VALUES ('U1',0,0,'',?,2000,100)",
            (now.date().isoformat(),),
        )
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="REVISION-PHOTO",
            payload=ai_estimated_payload(), source_image_ref=image_ref,
            meal_slot="午餐", consumed_at=now.isoformat(),
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        confirmed = apply_meal_photo_action(
            conn, event_id="REVISION-GENESIS", user_id="U1", token=token,
            expected_version=draft["version"], action="confirm_estimate",
        )
        log_id = confirmed["result"]["log_id"]
        refresh_user_health_check_case(conn, user_id="U1", evaluated_at=now)
        original = conn.execute(
            "SELECT trust_payload_json,trust_hash FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()
        estimate = json.loads(conn.execute(
            "SELECT exchange_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0])
        estimate.update({
            "calories_kcal": 510.0,
            "protein_g": 31.0,
            "fat_g": None,
            "carbohydrate_g": None,
            "calories_kcal_range": {"min": 450.0, "max": 580.0, "basis": "ai_vision_estimate_range_v1"},
            "protein_g_range": {"min": 27.0, "max": 36.0, "basis": "ai_vision_estimate_range_v1"},
            "estimate_items": [{
                "name": "修正版餐點", "portion": "半碗飯", "calories_kcal": 510.0,
                "protein_g": 31.0,
            }],
        })
        revision = create_meal_photo_revision_draft(
            conn, user_id="U1", log_id=log_id, from_version=1,
            request_text="白飯改半碗", estimate=estimate,
        )
        before_manifest = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0]
        conn.execute("""CREATE TRIGGER reject_revision_refresh
            BEFORE UPDATE ON vip_health_check_cases
            BEGIN SELECT RAISE(ABORT, 'temporary revision refresh failure'); END""")
        conn.commit()

    first = server.confirm_meal_photo_revision(
        event_id="REVISION-CONSUMERS", user_id="U1", log_id=log_id,
        from_version=1, draft_token=revision["token"],
    )
    assert first["replayed"] is False
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT version FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0] == before_manifest
        conn.execute("DROP TRIGGER reject_revision_refresh")
        committed = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("food_logs", "daily_food_log_events", "nutrition_sheet_outbox")
        }
        conn.commit()

    replay = server.confirm_meal_photo_revision(
        event_id="REVISION-CONSUMERS", user_id="U1", log_id=log_id,
        from_version=1, draft_token=revision["token"],
    )
    assert replay == {**first, "replayed": True}

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert {
            table: [tuple(row) for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()]
            for table in ("food_logs", "daily_food_log_events", "nutrition_sheet_outbox")
        } == committed
        projection = user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )
        canonical_row = conn.execute(
            "SELECT consumed_at,meal_slot,version FROM food_logs WHERE log_id=?",
            (log_id,),
        ).fetchone()
        ref = conn.execute(
            "SELECT food_log_version,source_hash FROM vip_health_check_source_refs "
            "WHERE case_id=? AND food_log_id=?", (case["case_id"], log_id),
        ).fetchone()
        expected_source_payload = {
            "schema_version": "vip_health_check_source_v2",
            "food_log_id": str(log_id),
            "food_log_version": int(canonical_row["version"]),
            "nutrition_snapshot_json": _canonical(projection["nutrition"]),
            "local_date": datetime.fromisoformat(
                canonical_row["consumed_at"]
            ).astimezone(ZoneInfo("Asia/Taipei")).date().isoformat(),
            "normalized_meal_slot": (
                str(canonical_row["meal_slot"] or "").strip() or "unspecified"
            ),
            "trust_binding": str(projection["effective_revision_hash"] or ""),
        }
        expected_source_hash = hashlib.sha256(
            _canonical(expected_source_payload).encode("utf-8")
        ).hexdigest()
        assert tuple(ref) == (2, expected_source_hash)
        summary = daily_food_summary(
            conn, user_id="U1", date_iso=now.date().isoformat()
        )
        detail = load_health_check_detail(conn, case_id=case["case_id"])
        hp = conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'"
        ).fetchone()
        assert original == tuple(conn.execute(
            "SELECT trust_payload_json,trust_hash FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone())
        assert summary["estimated_totals"] == {"calories_kcal": 510.0, "protein_g": 31.0}
        assert tuple(hp) == (510.0, 31.0)
        source = detail["source_logs"][0]
        assert source["food_log_version"] == 2
        assert source["nutrition_snapshot"] == {"calories_kcal": 510.0, "protein_g": 31.0}
        assert source["estimate_schema_version"] == "meal-photo-user-confirmation-v2"
        assert source["estimate"]["fat_g"] is None
        assert source["estimate"]["carbohydrate_g"] is None
        # Image access intentionally remains limited to the existing viewable-case states.
        conn.execute(
            "UPDATE vip_health_check_cases SET status='ready_for_review' WHERE case_id=?",
            (case["case_id"],),
        )
        preview = load_health_check_image(
            conn, case_id=case["case_id"], log_id=log_id, image_root=image_root,
        )
        assert preview is not None and preview.data.startswith(b"\xff\xd8\xff")

    ledger = server.get_daily_food_ledger("U1", now.date().isoformat())
    item = ledger["items"][0]
    assert item["version"] == 2
    assert item["nutrition"]["calories_kcal"] == 510.0
    assert item["nutrition"]["protein_g"] == 31.0
    assert item["nutrition"].get("fat_g") is None
    assert item["nutrition"].get("carbohydrate_g") is None
    assert {"fat_g", "carbohydrate_g"} <= ledger["unknown_fields"]

    rows = []
    class Sheet:
        def find(self, *_args, **_kwargs):
            return None
        def append_row(self, values, **_kwargs):
            rows.append(values)
    monkeypatch.setattr(server, "_nutrition_ws", lambda _title: Sheet())
    server._sync_food_log_outbox(log_id)
    assert rows[-1][9:13] == [510.0, 31.0, "", ""]
    assert rows[-1][29:] == ["user_confirmed_ai_estimate", "meal-photo-user-confirmation-v2"]
