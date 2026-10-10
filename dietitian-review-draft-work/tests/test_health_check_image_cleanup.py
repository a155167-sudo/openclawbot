from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from meal_photo_system import apply_meal_photo_action, save_meal_photo_draft
from nutrition_system import (
    confirm_pending_label,
    ensure_nutrition_schema,
    save_pending_label,
    user_confirmed_meal_photo_estimate_is_valid,
)
from vip_health_check import (
    approve_health_check_review,
    configure_vip_health_check_connection,
    ensure_vip_health_check_schema,
    record_health_check_delivery_attempt,
    refresh_case_source_manifest,
    save_health_check_review,
)


def _payload():
    return {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": [{"name": "高麗菜", "category": "vegetable", "confidence": 0.98}],
        "uncertain_items": ["主菜不明"],
        "starch_visibility": "not_visible",
        "oil_sauce_status": "unknown",
        "observed_at": "2026-09-01T12:00:00+08:00",
        "observed_at_confidence": 0.9,
    }


def _label_payload(name):
    return {
        "status": "success", "image_type": "nutrition_label", "product_name": name,
        "brand": "fixture", "barcode": "", "package_amount": 100,
        "package_unit": "g", "servings_per_package": 1,
        "per_serving": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 10},
        "per_100": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 10},
        "confidence": 0.99,
    }


def _create_confirmed_photo(conn, root: Path, *, suffix="a", message_id="M1", meal_slot="午餐"):
    filename = suffix * 32 + ".jpg"
    ref = "nutrition-image:" + filename
    (root / filename).write_bytes(b"private-image")
    token = save_meal_photo_draft(
        conn, user_id="U1", source_message_id=message_id, payload=_payload(),
        source_image_ref=ref, consumed_at="2026-09-01T12:00:00+08:00", meal_slot=meal_slot,
    )
    for version, (field, value) in enumerate((
        ("scope", "visible_only"), ("protein_type", "chicken"),
        ("protein_portion", "one_palm"), ("protein_more", "done"),
        ("starch_portion", "one_bowl"), ("vegetable_portion", "one_bowl"),
        ("cooking_oil", "unknown"), ("sauce_level", "unknown"),
    ), start=1):
        apply_meal_photo_action(
            conn, event_id=f"{message_id}-answer-{version}", user_id="U1", token=token,
            expected_version=version, action="answer", field=field, value=value,
        )
    confirmed = apply_meal_photo_action(
        conn, event_id=f"{message_id}-confirm", user_id="U1", token=token,
        expected_version=9, action="confirm_estimate",
    )
    log_id = confirmed["result"]["log_id"]
    assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
    food_id = conn.execute("SELECT food_id FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0]
    return ref, token, log_id, food_id


def _build_case(
    tmp_path: Path, *, case_status="delivered", delivery_status="delivered",
    second_photo: bool = False, second_non_photo: bool = False,
):
    db = tmp_path / "cleanup.db"
    root = tmp_path / "nutrition_images"
    root.mkdir()
    conn = sqlite3.connect(db)
    configure_vip_health_check_connection(conn)
    ensure_nutrition_schema(conn)
    food_log_columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
    if "version" not in food_log_columns:
        conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    if "deleted_at" not in food_log_columns:
        conn.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    ensure_vip_health_check_schema(conn)
    ref, token, log_id, food_id = _create_confirmed_photo(conn, root)
    if second_photo:
        second_ref, second_token, second_log, second_food = _create_confirmed_photo(
            conn, root, suffix="b", message_id="M2", meal_slot="晚餐"
        )
        if second_non_photo:
            conn.execute(
                "UPDATE food_logs SET source_image_ref='',trust_type='' WHERE log_id=?",
                (second_log,),
            )
            conn.execute(
                "UPDATE food_catalog SET original_image_ref='',source_type='manual_food' WHERE food_id=?",
                (second_food,),
            )
            conn.execute(
                "UPDATE pending_meal_photo_drafts SET source_image_ref='' WHERE token=?",
                (second_token,),
            )
    for index, (consumed_at, meal_slot) in enumerate((
        ("2026-09-01T08:00:00+08:00", "早餐"),
        ("2026-09-02T08:00:00+08:00", "早餐"),
        ("2026-09-02T12:00:00+08:00", "午餐"),
        ("2026-09-03T08:00:00+08:00", "早餐"),
        ("2026-09-03T12:00:00+08:00", "午餐"),
    )):
        label_token = save_pending_label(
            conn, user_id="U1", payload=_label_payload(f"fixture-{index}"),
            consumed_at=consumed_at, meal_slot=meal_slot,
        )
        confirm_pending_label(
            conn, token=label_token, user_id="U1", plan_link_status="no_plan"
        )
    now = "2026-09-04T12:00:00+08:00"
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,status,valid_day_count,source_manifest_hash,
            submitted_at,report_published_at,created_at,updated_at)
           VALUES ('case-1','U1','first_vip_baseline_check','a1','e1',?,?,'collecting',0,'','','',?,?)""",
        ("2026-09-01T00:00:00+08:00", "2026-09-08T00:00:00+08:00", now, now),
    )
    refreshed = refresh_case_source_manifest(
        conn, case_id="case-1", evaluated_at=datetime.fromisoformat(now)
    )
    manifest = str(refreshed["source_manifest_hash"])
    assert len(manifest) == 64
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_valid_days").fetchone()[0] == 3
    conn.execute("UPDATE vip_health_check_cases SET status='ready_for_review' WHERE case_id='case-1'")
    draft = save_health_check_review(
        conn,
        case_id="case-1",
        ai_observations={},
        review={},
        suggested_values={},
        limitations="fixture",
        source_manifest_hash=manifest,
        saved_at=datetime.fromisoformat(now),
    )
    review_id = str(draft["review_id"])
    review_version = draft["review_version"]
    assert isinstance(review_version, int)
    approved = approve_health_check_review(
        conn,
        case_id="case-1",
        review_id=review_id,
        expected_version=review_version,
        approved_by="D1",
        approved_at=datetime.fromisoformat(now),
        report={
            "good": "good",
            "priority": "priority",
            "next_7_days": "next",
            "limitations": "fixture",
        },
    )
    if delivery_status != "pending":
        record_health_check_delivery_attempt(
            conn,
            delivery_key=str(approved["delivery_key"]),
            succeeded=delivery_status == "delivered",
            error="fixture failure" if delivery_status == "failed" else "",
            attempted_at=datetime.fromisoformat(now),
        )
    conn.execute("UPDATE vip_health_check_cases SET status=? WHERE case_id='case-1'", (case_status,))
    conn.commit()
    return conn, db, root, ref, token, log_id, food_id


def _all_image_refs(conn):
    return {
        "draft": tuple(row[0] for row in conn.execute(
            "SELECT source_image_ref FROM pending_meal_photo_drafts ORDER BY token"
        )),
        "log": tuple(row[0] for row in conn.execute(
            """SELECT fl.source_image_ref FROM food_logs fl
               JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fc.source_type='user_meal_photo' ORDER BY fl.log_id"""
        )),
        "food": tuple(row[0] for row in conn.execute(
            """SELECT original_image_ref FROM food_catalog
               WHERE source_type='user_meal_photo' ORDER BY food_id"""
        )),
    }


def _cleanup(db, root, **kwargs):
    from health_check_image_cleanup import cleanup_delivered_health_check_images
    return cleanup_delivered_health_check_images(str(db), str(root), **kwargs)


def test_committed_exact_delivery_deletes_and_clears_three_refs_without_changing_trust(tmp_path):
    conn, db, root, ref, token, log_id, food_id = _build_case(tmp_path)
    before = conn.execute(
        "SELECT trust_type,trust_payload_json,trust_hash,exchange_snapshot_json,version FROM food_logs"
    ).fetchone()
    report_before = conn.execute("SELECT * FROM vip_health_check_reports").fetchone()
    event_before = conn.execute("SELECT * FROM meal_photo_events WHERE event_id='M1-confirm'").fetchone()
    result = _cleanup(db, root, case_id="case-1")
    after = conn.execute(
        "SELECT trust_type,trust_payload_json,trust_hash,exchange_snapshot_json,version,source_image_ref FROM food_logs"
    ).fetchone()
    assert result == {"deleted": 1, "missing": 0, "blocked": 0, "retry_pending": 0}
    assert not any(root.iterdir())
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (token,)).fetchone()[0] == ""
    assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (food_id,)).fetchone()[0] == ""
    assert tuple(after[:5]) == tuple(before) and after[5] == ""
    assert conn.execute("SELECT * FROM vip_health_check_reports").fetchone() == report_before
    assert conn.execute("SELECT * FROM meal_photo_events WHERE event_id='M1-confirm'").fetchone() == event_before
    assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
    assert dict(conn.execute("SELECT entity_type,status FROM nutrition_sheet_outbox")) == {
        "food": "pending", "food_log": "pending"
    }
    conn.close()


@pytest.mark.parametrize("delivery_status", ["pending", "failed"])
def test_pending_or_failed_delivery_deletes_nothing(tmp_path, delivery_status):
    conn, db, root, ref, *_ = _build_case(
        tmp_path, case_status="approved_pending_delivery" if delivery_status == "pending" else "delivery_failed",
        delivery_status=delivery_status,
    )
    assert _cleanup(db, root, case_id="case-1")["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    conn.close()


def test_case_delivered_without_exact_committed_delivery_deletes_nothing(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path, case_status="delivered", delivery_status="pending")
    assert _cleanup(db, root, case_id="case-1")["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    conn.close()


def test_missing_image_reference_holder_schema_fails_closed(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute("DROP TABLE pending_nutrition_logs")
    conn.commit()

    result = _cleanup(db, root, case_id="case-1")

    assert result == {"deleted": 0, "missing": 0, "blocked": 0, "retry_pending": 0}
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert _all_image_refs(conn) == {"draft": (ref,), "log": (ref,), "food": (ref,)}
    conn.close()


def test_unapproved_review_with_forged_delivered_states_deletes_nothing(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute("UPDATE vip_health_check_reviews SET status='draft',approved_by='',approved_at=''")
    conn.commit()
    assert _cleanup(db, root, case_id="case-1")["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    conn.close()


def test_shared_active_other_owner_reference_blocks_target_cleanup(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute(
        """INSERT INTO pending_meal_photo_drafts
           (token,user_id,source_message_id,source_image_ref,observed_payload_json,created_at,updated_at,expires_at)
           VALUES ('other-token','U2','M2',?,'{}','now','now','2099-01-01')""",
        (ref,),
    )
    conn.commit()
    result = _cleanup(db, root, case_id="case-1")
    assert result["blocked"] == 6
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    conn.close()


@pytest.mark.parametrize("label_owner", ["U1", "U2"])
def test_active_nutrition_label_reference_blocks_cleanup_for_any_owner(tmp_path, label_owner):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute(
        """INSERT INTO pending_nutrition_logs
           (token,user_id,label_payload_json,source_image_ref,status,created_at,expires_at)
           VALUES ('label-draft',?,'{}',?,'pending','now','2099-01-01')""",
        (label_owner, ref),
    )
    conn.commit()
    before_refs = {
        **_all_image_refs(conn),
        "label": tuple(row[0] for row in conn.execute(
            "SELECT source_image_ref FROM pending_nutrition_logs ORDER BY token"
        )),
    }
    before_outbox = tuple(conn.execute(
        "SELECT * FROM nutrition_sheet_outbox ORDER BY outbox_id"
    ).fetchall())

    result = _cleanup(db, root, case_id="case-1")

    assert result == {"deleted": 0, "missing": 0, "blocked": 6, "retry_pending": 0}
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert {
        **_all_image_refs(conn),
        "label": tuple(row[0] for row in conn.execute(
            "SELECT source_image_ref FROM pending_nutrition_logs ORDER BY token"
        )),
    } == before_refs
    assert tuple(conn.execute(
        "SELECT * FROM nutrition_sheet_outbox ORDER BY outbox_id"
    ).fetchall()) == before_outbox
    conn.close()


def test_unlink_failure_keeps_all_refs_then_retry_clears(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    def fail(_root, _ref):
        raise OSError("busy")
    first = _cleanup(db, root, case_id="case-1", unlinker=fail)
    assert first["retry_pending"] == 1
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    second = _cleanup(db, root, case_id="case-1")
    assert second["deleted"] == 1
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ""
    conn.close()


def test_missing_image_root_keeps_refs_and_outbox_then_retries_after_restore(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    before_refs = _all_image_refs(conn)
    before_outbox = tuple(conn.execute(
        "SELECT * FROM nutrition_sheet_outbox ORDER BY outbox_id"
    ).fetchall())
    moved_root = tmp_path / "nutrition_images_temporarily_moved"
    root.rename(moved_root)

    first = _cleanup(db, root, case_id="case-1")

    assert first == {"deleted": 0, "missing": 0, "blocked": 0, "retry_pending": 1}
    assert (moved_root / ref.removeprefix("nutrition-image:")).exists()
    assert _all_image_refs(conn) == before_refs
    assert tuple(conn.execute(
        "SELECT * FROM nutrition_sheet_outbox ORDER BY outbox_id"
    ).fetchall()) == before_outbox

    moved_root.rename(root)
    second = _cleanup(db, root, case_id="case-1")

    assert second == {"deleted": 1, "missing": 0, "blocked": 0, "retry_pending": 0}
    assert not (root / ref.removeprefix("nutrition-image:")).exists()
    assert _all_image_refs(conn) == {"draft": ("",), "log": ("",), "food": ("",)}
    conn.close()


def test_missing_file_converges_refs_to_empty(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    os.unlink(root / ref.removeprefix("nutrition-image:"))
    result = _cleanup(db, root, case_id="case-1")
    assert result["missing"] == 1
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts").fetchone()[0] == ""
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ""
    assert conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0] == ""
    conn.close()


def test_case_scoped_cleanup_does_not_touch_another_case(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    # A second eligible case with no image source proves the selector does not broaden housekeeping.
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
           VALUES ('case-2','U2','first_vip_baseline_check','a2','e2','s','e','delivered',0,'n','n')"""
    )
    conn.commit()
    result = _cleanup(db, root, case_id="case-2")
    assert result["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    conn.close()


def test_same_log_used_by_undelivered_case_blocks_cleanup(tmp_path):
    conn, db, root, ref, _token, log_id, _food_id = _build_case(tmp_path)
    now = "2026-09-02T12:00:00+08:00"
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
           VALUES ('case-2','U2','first_vip_baseline_check','a2','e2','s','e','collecting',0,?,?)""",
        (now, now),
    )
    source = conn.execute(
        "SELECT food_log_version,local_date,included_reason,source_hash,created_at FROM vip_health_check_source_refs WHERE case_id='case-1'"
    ).fetchone()
    conn.execute("INSERT INTO vip_health_check_source_refs VALUES ('case-2',?,?,?,?,?,?)", (log_id, *source))
    conn.commit()
    assert _cleanup(db, root, case_id="case-1")["blocked"] == 6
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    conn.close()


def test_integrity_tamper_blocks_cleanup_without_mutating_trust(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute("UPDATE vip_health_check_source_refs SET source_hash=?", ("0" * 64,))
    conn.commit()
    assert _cleanup(db, root, case_id="case-1")["blocked"] == 6
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    conn.close()


def test_added_valid_day_after_approval_preserves_file_and_all_refs(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute(
        """INSERT INTO vip_health_check_valid_days
           (case_id,local_date,rule_version,qualifying_meal_count,completeness_status,evaluated_at)
           VALUES ('case-1','2026-09-04','draft-confirmed-meals-v1',0,'incomplete',
                   '2026-09-04T12:00:00+08:00')"""
    )
    conn.commit()

    assert _cleanup(db, root, case_id="case-1")["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts").fetchone()[0] == ref
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    assert conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0] == ref
    conn.close()


def test_added_canonical_source_ref_after_approval_preserves_every_file_and_ref(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    old_manifest = conn.execute(
        "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id='case-1'"
    ).fetchone()[0]
    second_ref, *_ = _create_confirmed_photo(
        conn, root, suffix="b", message_id="M2", meal_slot="晚餐"
    )
    conn.execute("UPDATE vip_health_check_cases SET status='collecting' WHERE case_id='case-1'")
    refresh_case_source_manifest(
        conn,
        case_id="case-1",
        evaluated_at=datetime.fromisoformat("2026-09-02T12:00:00+08:00"),
    )
    conn.execute(
        "UPDATE vip_health_check_cases SET status='delivered',source_manifest_hash=? WHERE case_id='case-1'",
        (old_manifest,),
    )
    conn.commit()

    assert _cleanup(db, root, case_id="case-1")["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert (root / second_ref.removeprefix("nutrition-image:")).exists()
    assert {row[0] for row in conn.execute("SELECT source_image_ref FROM food_logs WHERE source_image_ref<>''")} == {
        ref, second_ref
    }
    assert {row[0] for row in conn.execute("SELECT original_image_ref FROM food_catalog WHERE original_image_ref<>''")} == {
        ref, second_ref
    }
    conn.close()


def test_review_manifest_tamper_preserves_file_and_all_refs(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    conn.execute("UPDATE vip_health_check_reviews SET source_manifest_hash=?", ("0" * 64,))
    conn.commit()

    assert _cleanup(db, root, case_id="case-1")["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts").fetchone()[0] == ref
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    assert conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0] == ref
    conn.close()


def test_symlink_leaf_is_not_followed_or_cleared(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    image = root / ref.removeprefix("nutrition-image:")
    image.unlink()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"must-survive")
    image.symlink_to(outside)
    assert _cleanup(db, root, case_id="case-1")["blocked"] == 1
    assert outside.read_bytes() == b"must-survive"
    assert image.is_symlink()
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    conn.close()


def test_existing_90_day_cleanup_preserves_undelivered_health_check_evidence(tmp_path, monkeypatch):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    conn, db, root, ref, *_ = _build_case(
        tmp_path, case_status="approved_pending_delivery", delivery_status="pending"
    )
    conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00'")
    conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    server.cleanup_nutrition_images()
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts").fetchone()[0] == ref
    assert conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0] == ref
    assert conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0] == ref
    assert user_confirmed_meal_photo_estimate_is_valid(
        conn,
        conn.execute(
            "SELECT log_id FROM food_logs WHERE source_image_ref=?", (ref,)
        ).fetchone()[0],
    )
    conn.close()


def test_existing_scheduler_cleanup_retries_committed_delivered_case(tmp_path, monkeypatch):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    conn, db, root, ref, *_ = _build_case(tmp_path)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    server.cleanup_nutrition_images()
    assert not (root / ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts").fetchone()[0] == ""
    conn.close()


def test_scheduler_stale_temp_cleanup_does_not_follow_root_symlink(tmp_path, monkeypatch):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server
    from meal_photo_system import ensure_meal_photo_schema

    db = tmp_path / "cleanup.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        ensure_meal_photo_schema(conn)
    external = tmp_path / "external"
    external.mkdir()
    temp_name = "a" * 32 + ".jpg.0123456789abcdef.tmp"
    sentinel = external / temp_name
    sentinel.write_bytes(b"must-survive")
    old = server.tw_now().timestamp() - 7200
    os.utime(sentinel, (old, old))
    (tmp_path / "nutrition_images").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))

    server.cleanup_nutrition_images()

    assert sentinel.read_bytes() == b"must-survive"


def test_stale_temp_cleanup_uses_exact_writer_grammar_and_nofollow_leaf_rules(tmp_path):
    from health_check_image_cleanup import cleanup_stale_nutrition_image_temps

    root = tmp_path / "nutrition_images"
    root.mkdir()
    now = 2_000_000_000.0
    old = now - 7200
    fresh = now - 60
    valid_old = root / ("a" * 32 + ".jpg.0123456789abcdef.tmp")
    valid_fresh = root / ("b" * 32 + ".png.fedcba9876543210.tmp")
    non_app = root / ("c" * 32 + ".webp.not-a-token.tmp")
    outside = tmp_path / "external-sentinel"
    outside.write_bytes(b"must-survive")
    leaf_symlink = root / ("d" * 32 + ".webp.0011223344556677.tmp")
    non_regular = root / ("e" * 32 + ".jpg.8899aabbccddeeff.tmp")
    valid_old.write_bytes(b"old-app-temp")
    valid_fresh.write_bytes(b"fresh-app-temp")
    non_app.write_bytes(b"unowned-temp")
    leaf_symlink.symlink_to(outside)
    non_regular.mkdir()
    for path, timestamp in ((valid_old, old), (valid_fresh, fresh), (non_app, old), (non_regular, old)):
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)

    deleted = cleanup_stale_nutrition_image_temps(root, now_timestamp=now)

    assert deleted == 1
    assert not valid_old.exists()
    assert valid_fresh.read_bytes() == b"fresh-app-temp"
    assert non_app.read_bytes() == b"unowned-temp"
    assert leaf_symlink.is_symlink()
    assert outside.read_bytes() == b"must-survive"
    assert non_regular.is_dir()


def test_stale_temp_cleanup_missing_root_is_noop(tmp_path):
    from health_check_image_cleanup import cleanup_stale_nutrition_image_temps

    assert cleanup_stale_nutrition_image_temps(
        tmp_path / "missing", now_timestamp=2_000_000_000.0
    ) == 0


@pytest.mark.parametrize("mutation", [
    "UPDATE vip_health_check_source_refs SET food_log_version=food_log_version+1 WHERE food_log_id=(SELECT food_log_id FROM vip_health_check_source_refs WHERE food_log_id IN (SELECT log_id FROM food_logs WHERE source_image_ref<>'') ORDER BY food_log_id DESC LIMIT 1)",
    "UPDATE vip_health_check_source_refs SET source_hash='" + "0" * 64 + "' WHERE food_log_id=(SELECT food_log_id FROM vip_health_check_source_refs WHERE food_log_id IN (SELECT log_id FROM food_logs WHERE source_image_ref<>'') ORDER BY food_log_id DESC LIMIT 1)",
    "UPDATE food_logs SET trust_type='' WHERE log_id=(SELECT food_log_id FROM vip_health_check_source_refs WHERE food_log_id IN (SELECT log_id FROM food_logs WHERE source_image_ref<>'') ORDER BY food_log_id DESC LIMIT 1)",
    "UPDATE food_logs SET deleted_at='tampered' WHERE log_id=(SELECT food_log_id FROM vip_health_check_source_refs WHERE food_log_id IN (SELECT log_id FROM food_logs WHERE source_image_ref<>'') ORDER BY food_log_id DESC LIMIT 1)",
    "UPDATE food_logs SET confirmation_status='draft' WHERE log_id=(SELECT food_log_id FROM vip_health_check_source_refs WHERE food_log_id IN (SELECT log_id FROM food_logs WHERE source_image_ref<>'') ORDER BY food_log_id DESC LIMIT 1)",
    "UPDATE food_catalog SET owner_user_id='U2' WHERE food_id=(SELECT food_id FROM food_logs WHERE log_id=(SELECT food_log_id FROM vip_health_check_source_refs WHERE food_log_id IN (SELECT log_id FROM food_logs WHERE source_image_ref<>'') ORDER BY food_log_id DESC LIMIT 1))",
])
def test_second_source_tamper_blocks_entire_case_before_any_unlink(tmp_path, mutation):
    conn, db, root, *_ = _build_case(tmp_path, second_photo=True)
    before = _all_image_refs(conn)
    files = {path.name for path in root.iterdir()}
    conn.execute(mutation)
    conn.commit()

    result = _cleanup(db, root, case_id="case-1")

    assert result == {"deleted": 0, "missing": 0, "blocked": 7, "retry_pending": 0}
    assert {path.name for path in root.iterdir()} == files
    assert _all_image_refs(conn) == before
    conn.close()


def test_two_source_case_is_preflighted_then_cleanup_retry_uses_canonical_truth(tmp_path):
    conn, db, root, *_ = _build_case(tmp_path, second_photo=True)

    first = _cleanup(db, root, case_id="case-1")
    second = _cleanup(db, root, case_id="case-1")

    assert first == {"deleted": 2, "missing": 0, "blocked": 0, "retry_pending": 0}
    assert second == {"deleted": 0, "missing": 0, "blocked": 0, "retry_pending": 0}
    assert not any(root.iterdir())
    assert _all_image_refs(conn) == {"draft": ("", ""), "log": ("", ""), "food": ("", "")}
    conn.close()


def test_non_photo_canonical_source_is_validated_without_requiring_photo_relationships(tmp_path):
    conn, db, root, *_ = _build_case(
        tmp_path, second_photo=True, second_non_photo=True
    )
    second_ref = "nutrition-image:" + "b" * 32 + ".jpg"

    result = _cleanup(db, root, case_id="case-1")

    assert result == {"deleted": 1, "missing": 0, "blocked": 0, "retry_pending": 0}
    assert (root / second_ref.removeprefix("nutrition-image:")).exists()
    conn.close()


@pytest.mark.parametrize(
    ("case_status", "delivery_status", "storage_state"),
    [
        ("approved_pending_delivery", "pending", "regular"),
        ("approved_pending_delivery", "failed", "regular"),
        ("delivered", "delivered", "regular"),
        ("approved_pending_delivery", "pending", "root_symlink"),
        ("approved_pending_delivery", "pending", "root_missing"),
        ("approved_pending_delivery", "pending", "leaf_symlink"),
        ("approved_pending_delivery", "pending", "leaf_missing"),
    ],
)
def test_scheduler_retention_gates_every_unlink_before_shared_reference_cleanup(
    tmp_path, monkeypatch, case_status, delivery_status, storage_state
):
    """One real scheduler run covers health, label, meal-draft, and 90-day unlink paths."""
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    conn, db, root, shared_ref, canonical_token, log_id, food_id = _build_case(
        tmp_path, case_status=case_status, delivery_status=delivery_status
    )
    conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?", (log_id,))

    label_token = save_pending_label(
        conn,
        user_id="label-owner",
        payload={
            "status": "success", "image_type": "nutrition_label",
            "product_name": "共享標示", "brand": "測試", "barcode": "",
            "package_amount": 100, "package_unit": "g", "servings_per_package": 1,
            "per_serving": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
            "per_100": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
            "confidence": 0.99,
        },
        source_image_ref=shared_ref,
    )
    conn.execute(
        "UPDATE pending_nutrition_logs SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
        (label_token,),
    )
    shared_meal_token = save_meal_photo_draft(
        conn, user_id="meal-owner", source_message_id="shared-expired-meal",
        payload=_payload(), source_image_ref=shared_ref,
    )
    conn.execute(
        "UPDATE pending_meal_photo_drafts SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
        (shared_meal_token,),
    )

    standalone_label_ref = "nutrition-image:" + "c" * 32 + ".jpg"
    (root / standalone_label_ref.removeprefix("nutrition-image:")).write_bytes(b"label")
    standalone_label_token = save_pending_label(
        conn, user_id="label-only", payload={
            "status": "success", "image_type": "nutrition_label",
            "product_name": "獨立標示", "brand": "測試", "barcode": "",
            "package_amount": 100, "package_unit": "g", "servings_per_package": 1,
            "per_serving": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
            "per_100": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
            "confidence": 0.99,
        }, source_image_ref=standalone_label_ref,
    )
    conn.execute(
        "UPDATE pending_nutrition_logs SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
        (standalone_label_token,),
    )

    standalone_meal_ref = "nutrition-image:" + "d" * 32 + ".jpg"
    (root / standalone_meal_ref.removeprefix("nutrition-image:")).write_bytes(b"meal")
    standalone_meal_token = save_meal_photo_draft(
        conn, user_id="meal-only", source_message_id="standalone-expired-meal",
        payload=_payload(), source_image_ref=standalone_meal_ref,
    )
    conn.execute(
        "UPDATE pending_meal_photo_drafts SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
        (standalone_meal_token,),
    )

    old_ref, old_token, old_log_id, old_food_id = _create_confirmed_photo(
        conn, root, suffix="e", message_id="old-independent", meal_slot="早餐"
    )
    conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?", (old_log_id,))
    conn.execute("UPDATE pending_meal_photo_drafts SET source_image_ref='' WHERE token=?", (old_token,))
    conn.commit()

    external = tmp_path / "external-images"
    if storage_state in {"root_symlink", "root_missing"}:
        root.rename(external)
        if storage_state == "root_symlink":
            root.symlink_to(external, target_is_directory=True)
    elif storage_state == "leaf_symlink":
        external.mkdir()
        for image_ref in (standalone_label_ref, standalone_meal_ref, old_ref):
            leaf = root / image_ref.removeprefix("nutrition-image:")
            leaf.rename(external / leaf.name)
            leaf.symlink_to(external / leaf.name)
    elif storage_state == "leaf_missing":
        for image_ref in (standalone_label_ref, standalone_meal_ref, old_ref):
            (root / image_ref.removeprefix("nutrition-image:")).unlink()

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    server.cleanup_nutrition_images()

    if storage_state in {"root_symlink", "root_missing", "leaf_symlink"}:
        protected_refs = (
            (shared_ref, standalone_label_ref, standalone_meal_ref, old_ref)
            if storage_state != "leaf_symlink"
            else (standalone_label_ref, standalone_meal_ref, old_ref)
        )
        for image_ref in protected_refs:
            assert (external / image_ref.removeprefix("nutrition-image:")).exists()
        assert conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (standalone_label_token,)).fetchone()[0] == standalone_label_ref
        assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (standalone_meal_token,)).fetchone()[0] == standalone_meal_ref
        assert conn.execute("SELECT source_image_ref FROM food_logs WHERE log_id=?", (old_log_id,)).fetchone()[0] == old_ref
        assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (old_food_id,)).fetchone()[0] == old_ref
        if storage_state == "leaf_symlink":
            for image_ref in protected_refs:
                assert (root / image_ref.removeprefix("nutrition-image:")).is_symlink()
        conn.close()
        return

    assert (root / shared_ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (label_token,)).fetchone()[0] == shared_ref
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (shared_meal_token,)).fetchone()[0] == shared_ref
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (canonical_token,)).fetchone()[0] == shared_ref
    assert conn.execute("SELECT source_image_ref FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0] == shared_ref
    assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (food_id,)).fetchone()[0] == shared_ref

    assert not (root / standalone_label_ref.removeprefix("nutrition-image:")).exists()
    assert not (root / standalone_meal_ref.removeprefix("nutrition-image:")).exists()
    assert not (root / old_ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (standalone_label_token,)).fetchone()[0] == ""
    assert conn.execute("SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (standalone_meal_token,)).fetchone()[0] == ""
    assert conn.execute("SELECT source_image_ref FROM food_logs WHERE log_id=?", (old_log_id,)).fetchone()[0] == ""
    assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (old_food_id,)).fetchone()[0] == ""
    conn.close()


def test_scheduler_retention_fails_closed_when_reference_holder_schema_is_missing(tmp_path, monkeypatch):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    conn, db, root, ref, *_ = _build_case(
        tmp_path, case_status="approved_pending_delivery", delivery_status="pending"
    )
    conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00'")
    standalone_ref = "nutrition-image:" + "f" * 32 + ".jpg"
    (root / standalone_ref.removeprefix("nutrition-image:")).write_bytes(b"standalone")
    standalone_token = save_pending_label(
        conn, user_id="standalone", payload={
            "status": "success", "image_type": "nutrition_label",
            "product_name": "缺 schema", "brand": "測試", "barcode": "",
            "package_amount": 100, "package_unit": "g", "servings_per_package": 1,
            "per_serving": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
            "per_100": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
            "confidence": 0.99,
        }, source_image_ref=standalone_ref,
    )
    conn.execute(
        "UPDATE pending_nutrition_logs SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
        (standalone_token,),
    )
    conn.execute("DROP TABLE vip_health_check_source_refs")
    conn.commit()
    before = _all_image_refs(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))

    server.cleanup_nutrition_images()

    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert (root / standalone_ref.removeprefix("nutrition-image:")).exists()
    assert conn.execute(
        "SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (standalone_token,)
    ).fetchone()[0] == standalone_ref
    assert _all_image_refs(conn) == before
    conn.close()


def test_reference_holder_contract_distinguishes_legacy_partial_and_complete_health_schema(tmp_path):
    from health_check_image_cleanup import image_reference_holder_schema_is_complete
    from meal_photo_system import ensure_meal_photo_schema

    legacy = sqlite3.connect(tmp_path / "legacy.db")
    ensure_nutrition_schema(legacy)
    ensure_meal_photo_schema(legacy)
    assert image_reference_holder_schema_is_complete(legacy) is True

    partial = sqlite3.connect(tmp_path / "partial.db")
    ensure_nutrition_schema(partial)
    ensure_meal_photo_schema(partial)
    partial.execute("CREATE TABLE vip_health_check_cases(case_id TEXT, user_id TEXT)")
    partial.execute("CREATE TABLE vip_health_check_source_refs(case_id TEXT, food_log_id TEXT)")
    assert image_reference_holder_schema_is_complete(partial) is False

    complete = sqlite3.connect(tmp_path / "complete.db")
    ensure_nutrition_schema(complete)
    ensure_meal_photo_schema(complete)
    complete.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    complete.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    configure_vip_health_check_connection(complete)
    ensure_vip_health_check_schema(complete)
    assert image_reference_holder_schema_is_complete(complete) is True
    legacy.close(); partial.close(); complete.close()


@pytest.mark.parametrize("lane", ["label", "meal"])
def test_expired_candidate_restored_active_before_lock_is_not_unlinked(tmp_path, monkeypatch, lane):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server
    from meal_photo_system import ensure_meal_photo_schema

    db = tmp_path / f"{lane}-toctou.db"
    root = tmp_path / "nutrition_images"; root.mkdir()
    ref = "nutrition-image:" + ("a" if lane == "label" else "b") * 32 + ".jpg"
    path = root / ref.removeprefix("nutrition-image:"); path.write_bytes(b"candidate")
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn); ensure_meal_photo_schema(conn)
        if lane == "label":
            token = save_pending_label(conn, user_id="U_LABEL", payload={
                "status": "success", "image_type": "nutrition_label",
                "product_name": "交錯", "brand": "測試", "barcode": "",
                "package_amount": 100, "package_unit": "g", "servings_per_package": 1,
                "per_serving": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
                "per_100": {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbohydrate_g": 8},
                "confidence": 0.99,
            }, source_image_ref=ref)
            conn.execute("UPDATE pending_nutrition_logs SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (token,))
        else:
            token = save_meal_photo_draft(conn, user_id="U_MEAL", source_message_id="toctou", payload=_payload(), source_image_ref=ref)
            conn.execute("UPDATE pending_meal_photo_drafts SET status='expired',expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (token,))
        conn.commit()

    fired = []
    def restore(candidate_lane, candidate_key):
        if candidate_lane != lane or fired: return
        fired.append(candidate_key)
        table = "pending_nutrition_logs" if lane == "label" else "pending_meal_photo_drafts"
        active = "pending" if lane == "label" else "awaiting_confirmation"
        with sqlite3.connect(db) as writer:
            writer.execute(f"UPDATE {table} SET status=?,expires_at='2999-01-01T00:00:00+08:00' WHERE token=?", (active, token))
            writer.commit()

    monkeypatch.setattr(server, "DB_PATH", str(db)); monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    server.cleanup_nutrition_images(_before_candidate_lock=restore)

    assert fired == [token]
    assert path.read_bytes() == b"candidate"
    with sqlite3.connect(db) as conn:
        table = "pending_nutrition_logs" if lane == "label" else "pending_meal_photo_drafts"
        stored = conn.execute(f"SELECT status,source_image_ref FROM {table} WHERE token=?", (token,)).fetchone()
    assert stored == (("pending" if lane == "label" else "awaiting_confirmation"), ref)


@pytest.mark.parametrize("mutation", ["renewed", "ref_switched", "owner_changed", "version_changed"])
def test_90_day_candidate_changed_before_lock_keeps_old_file_and_current_row(tmp_path, monkeypatch, mutation):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    db = tmp_path / "cleanup.db"
    root = tmp_path / "nutrition_images"; root.mkdir()
    conn = sqlite3.connect(db)
    ensure_nutrition_schema(conn)
    conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    old_ref, token, log_id, food_id = _create_confirmed_photo(conn, root)
    conn.execute("UPDATE pending_meal_photo_drafts SET source_image_ref='' WHERE token=?", (token,))
    conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?", (log_id,))
    conn.commit(); conn.close()
    new_ref = "nutrition-image:" + "f" * 32 + ".jpg"

    def mutate(candidate_lane, candidate_key):
        if candidate_lane != "90day" or candidate_key != log_id: return
        with sqlite3.connect(db) as writer:
            if mutation == "renewed":
                writer.execute("UPDATE food_logs SET created_at='2999-01-01T00:00:00+08:00' WHERE log_id=?", (log_id,))
            else:
                if mutation == "ref_switched":
                    writer.execute("UPDATE food_logs SET source_image_ref=? WHERE log_id=?", (new_ref, log_id))
                    writer.execute("UPDATE food_catalog SET original_image_ref=? WHERE food_id=?", (new_ref, food_id))
                elif mutation == "owner_changed":
                    writer.execute("UPDATE food_catalog SET owner_user_id='U_OTHER' WHERE food_id=?", (food_id,))
                else:
                    writer.execute("UPDATE food_logs SET version=version+1 WHERE log_id=?", (log_id,))
            writer.commit()

    monkeypatch.setattr(server, "DB_PATH", str(db)); monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    server.cleanup_nutrition_images(_before_candidate_lock=mutate)

    assert (root / old_ref.removeprefix("nutrition-image:")).read_bytes() == b"private-image"
    with sqlite3.connect(db) as check:
        row = check.execute("SELECT created_at,source_image_ref FROM food_logs WHERE log_id=?", (log_id,)).fetchone()
    if mutation == "renewed":
        assert row == ("2999-01-01T00:00:00+08:00", old_ref)
    elif mutation == "ref_switched":
        assert row == ("2000-01-01T00:00:00+08:00", new_ref)
    else:
        assert row == ("2000-01-01T00:00:00+08:00", old_ref)


def test_delivered_three_day_case_with_tampered_valid_day_count_deletes_nothing(tmp_path):
    conn, db, root, ref, *_ = _build_case(tmp_path)
    assert conn.execute(
        "SELECT valid_day_count FROM vip_health_check_cases WHERE case_id='case-1'"
    ).fetchone()[0] == 3
    conn.execute("UPDATE vip_health_check_cases SET valid_day_count=2 WHERE case_id='case-1'")
    conn.commit()
    before = _all_image_refs(conn)

    result = _cleanup(db, root, case_id="case-1")

    assert result["deleted"] == 0
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    assert _all_image_refs(conn) == before
    conn.close()


def _build_isolated_90_day_candidate(tmp_path, monkeypatch):
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    db = tmp_path / "cleanup-90day.db"
    root = tmp_path / "nutrition_images"
    root.mkdir()
    conn = sqlite3.connect(db)
    ensure_nutrition_schema(conn)
    conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    ref, token, log_id, food_id = _create_confirmed_photo(conn, root)
    conn.execute("UPDATE pending_meal_photo_drafts SET source_image_ref='' WHERE token=?", (token,))
    conn.execute(
        "UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?", (log_id,)
    )
    conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    return conn, root, ref, log_id, food_id, server


def test_90_day_same_owner_private_catalog_deletes_isolated_image(tmp_path, monkeypatch):
    conn, root, ref, log_id, food_id, server = _build_isolated_90_day_candidate(
        tmp_path, monkeypatch
    )
    server.cleanup_nutrition_images()
    assert conn.execute("SELECT source_image_ref FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0] == ""
    assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (food_id,)).fetchone()[0] == ""
    assert not (root / ref.removeprefix("nutrition-image:")).exists()
    conn.close()


def test_90_day_cross_owner_catalog_preserves_file_and_both_refs(tmp_path, monkeypatch):
    conn, root, ref, log_id, food_id, server = _build_isolated_90_day_candidate(
        tmp_path, monkeypatch
    )
    conn.execute("UPDATE food_catalog SET owner_user_id='U_OTHER' WHERE food_id=?", (food_id,))
    conn.commit()
    server.cleanup_nutrition_images()
    assert conn.execute("SELECT source_image_ref FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0] == ref
    assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (food_id,)).fetchone()[0] == ref
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    conn.close()


def test_90_day_public_catalog_preserves_shared_original_image(tmp_path, monkeypatch):
    conn, root, ref, log_id, food_id, server = _build_isolated_90_day_candidate(
        tmp_path, monkeypatch
    )
    conn.execute("UPDATE food_catalog SET visibility='public' WHERE food_id=?", (food_id,))
    conn.commit()
    server.cleanup_nutrition_images()
    assert conn.execute("SELECT source_image_ref FROM food_logs WHERE log_id=?", (log_id,)).fetchone()[0] == ref
    assert conn.execute("SELECT original_image_ref FROM food_catalog WHERE food_id=?", (food_id,)).fetchone()[0] == ref
    assert (root / ref.removeprefix("nutrition-image:")).exists()
    conn.close()


@pytest.mark.parametrize("visibility", ["public", "shared", "unknown"])
def test_scheduler_delivered_case_preserves_same_owner_nonprivate_catalog_image(
    tmp_path, monkeypatch, visibility
):
    """Delivered-health must not exempt a catalog image that is not proven private."""
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")
    import server

    conn, db, root, ref, token, log_id, food_id = _build_case(tmp_path)
    conn.execute(
        "UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?",
        (log_id,),
    )
    conn.execute(
        "UPDATE food_catalog SET visibility=? WHERE food_id=?", (visibility, food_id)
    )
    conn.commit()
    before_refs = _all_image_refs(conn)
    before_outbox = tuple(conn.execute(
        "SELECT * FROM nutrition_sheet_outbox ORDER BY outbox_id"
    ).fetchall())
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))

    server.cleanup_nutrition_images()

    assert (root / ref.removeprefix("nutrition-image:")).read_bytes() == b"private-image"
    assert _all_image_refs(conn) == before_refs
    assert conn.execute(
        "SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (token,)
    ).fetchone()[0] == ref
    assert tuple(conn.execute(
        "SELECT * FROM nutrition_sheet_outbox ORDER BY outbox_id"
    ).fetchall()) == before_outbox
    conn.close()


@pytest.mark.parametrize(
    ("visibility", "protected"),
    [("private", False), ("public", True), ("shared", True), ("unknown", True), (None, True)],
)
def test_catalog_self_exclusion_requires_same_owner_private_visibility(
    tmp_path, visibility, protected
):
    from health_check_image_cleanup import nutrition_image_reference_is_protected

    ref = "nutrition-image:" + "a" * 32 + ".jpg"
    with sqlite3.connect(tmp_path / "holder-gate.db") as conn:
        conn.executescript(
            """
            CREATE TABLE pending_meal_photo_drafts(token TEXT, source_image_ref TEXT);
            CREATE TABLE pending_nutrition_logs(token TEXT, source_image_ref TEXT);
            CREATE TABLE food_logs(log_id TEXT, source_image_ref TEXT);
            CREATE TABLE food_catalog(
                food_id TEXT, owner_user_id TEXT, visibility TEXT,
                original_image_ref TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO food_catalog VALUES ('food-1','U1',?,?)", (visibility, ref)
        )

        assert nutrition_image_reference_is_protected(
            conn, ref, food_id="food-1", food_owner_id="U1"
        ) is protected
