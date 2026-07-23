import math
import sqlite3
from datetime import datetime, timedelta

import pytest

from meal_photo_system import (
    build_meal_photo_confirmation_bubble,
    ensure_meal_photo_schema,
    get_meal_photo_draft,
    build_meal_photo_estimate_bubble,
    apply_meal_photo_action,
    daily_pending_meal_photo_count,
    next_meal_photo_step,
    meal_photo_step_options,
    save_meal_photo_draft,
    normalize_meal_photo_payload,
)


def sample_payload(**overrides):
    payload = {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": [
            {"name": "高麗菜", "category": "vegetable", "confidence": 0.98},
            {"name": "青花菜", "category": "vegetable", "confidence": 0.97},
        ],
        "uncertain_items": ["上方棕色主菜種類不明"],
        "starch_visibility": "not_visible",
        "oil_sauce_status": "unknown",
        "observed_at": "2026-07-22T22:37:00+08:00",
        "observed_at_confidence": 0.99,
    }
    payload.update(overrides)
    return payload


def flatten_text(node):
    if isinstance(node, dict):
        text = [str(node.get("text", ""))]
        for value in node.values():
            text.extend(flatten_text(value))
        return text
    if isinstance(node, list):
        result = []
        for value in node:
            result.extend(flatten_text(value))
        return result
    return []


def test_food_photo_normalizer_preserves_unknown_instead_of_zero():
    normalized = normalize_meal_photo_payload(
        sample_payload(
            calories_kcal=0,
            protein_g=0,
            starch_exchange=0,
        )
    )

    assert normalized["visible_items"][0]["name"] == "高麗菜"
    assert normalized["starch_visibility"] == "not_visible"
    assert "calories_kcal" not in normalized
    assert "protein_g" not in normalized
    assert "starch_exchange" not in normalized


def test_food_photo_normalizer_rejects_hostile_or_implausible_values():
    with pytest.raises(ValueError):
        normalize_meal_photo_payload(sample_payload(visible_items="高麗菜"))
    with pytest.raises(ValueError):
        normalize_meal_photo_payload(
            sample_payload(visible_items=[{"name": "菜", "category": "vegetable", "confidence": math.nan}])
        )
    with pytest.raises(ValueError):
        normalize_meal_photo_payload(sample_payload(starch_visibility="none"))


def test_confirmation_card_explicitly_distinguishes_not_visible_from_zero():
    bubble = build_meal_photo_confirmation_bubble(
        sample_payload(), token="abc123def456", consumed_at="2026-07-22T22:37:00+08:00"
    )
    text = "\n".join(flatten_text(bubble))

    assert "餐點照片辨識｜待你確認" in text
    assert "高麗菜、青花菜" in text
    assert "上方棕色主菜種類不明" in text
    assert "主食：NA（待確認；畫面未見不代表沒有吃）" in text
    assert "蛋白質食物：NA（待確認" in text
    assert "水果／奶類／其他未入鏡：NA（待確認）" in text
    assert "烹調用油／醬汁：NA（待確認；無法判定）" in text
    assert "熱量與交換份：NA（尚未估算）" in text
    assert "主食：0份" not in text
    assert "蛋白質：0份" not in text
    actions = str(bubble)
    assert "mp:v1:abc123def456:1:start" in actions
    assert "mp:v1:abc123def456:1:cancel" in actions
    assert "'type': 'postback'" in actions


def test_meal_photo_draft_is_durable_idempotent_and_user_owned(tmp_path):
    db = tmp_path / "meal-photo.db"
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn,
            user_id="U1",
            source_message_id="M1",
            payload=sample_payload(),
            source_image_ref="nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
            consumed_at="2026-07-22T22:37:00+08:00",
            consumed_time_source="photo_timestamp",
        )
        replay = save_meal_photo_draft(
            conn,
            user_id="U1",
            source_message_id="M1",
            payload=sample_payload(uncertain_items=["不同的重送內容不得覆寫原草稿"]),
            source_image_ref="nutrition-image:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg",
            consumed_at="2026-07-22T22:40:00+08:00",
            consumed_time_source="line_timestamp",
        )
        assert replay == token
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["source_image_ref"].endswith("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg")
        assert draft["consumed_at"] == "2026-07-22T22:37:00+08:00"
        assert draft["created_at"].endswith("+08:00")
        assert draft["expires_at"].endswith("+08:00")
        assert datetime.fromisoformat(draft["expires_at"]) - datetime.fromisoformat(draft["created_at"]) == timedelta(hours=24)
        assert draft["payload"]["uncertain_items"] == ["上方棕色主菜種類不明"]
        with pytest.raises(ValueError):
            get_meal_photo_draft(conn, user_id="U2", token=token)


def _apply_answer(conn, token, field, value, *, event_id):
    draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    return apply_meal_photo_action(
        conn, event_id=event_id, user_id="U1", token=token,
        expected_version=draft["version"], action="answer", field=field, value=value,
    )["draft"]


def test_meal_photo_answers_are_whitelisted_and_unknown_is_not_zero(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        draft = _apply_answer(
            conn, token, "scope", "visible_only", event_id="ANSWER-SCOPE"
        )
        assert draft["answers"]["scope"] == "visible_only"
        assert draft["answers"]["protein_type"] is None
        with pytest.raises(ValueError):
            apply_meal_photo_action(
                conn, event_id="BAD-VALUE", user_id="U1", token=token,
                expected_version=draft["version"], action="answer",
                field="protein_type", value="0",
            )
        with pytest.raises(ValueError):
            apply_meal_photo_action(
                conn, event_id="BAD-FIELD", user_id="U1", token=token,
                expected_version=draft["version"], action="answer",
                field="calories_kcal", value="200",
            )
        with pytest.raises(ValueError, match="步驟不符"):
            apply_meal_photo_action(
                conn, event_id="SKIP-STEP", user_id="U1", token=token,
                expected_version=draft["version"], action="answer",
                field="starch_portion", value="none",
            )


def test_cancelling_meal_photo_scrubs_payload_and_preserves_retryable_image_ref(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn,
            user_id="U1",
            source_message_id="M1",
            payload=sample_payload(),
            source_image_ref="nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        )
        cancelled = apply_meal_photo_action(
            conn, event_id="CANCEL", user_id="U1", token=token,
            expected_version=1, action="cancel",
        )
        assert cancelled["result"]["kind"] == "cancel"
        assert cancelled["result"]["source_image_ref"].endswith(".jpg")
        row = conn.execute(
            "SELECT status,observed_payload_json,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
        assert row == ("cancelled", "{}", "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg")
        replay = apply_meal_photo_action(
            conn, event_id="CANCEL", user_id="U1", token=token,
            expected_version=1, action="cancel",
        )
        assert replay["replayed"] is True


def _answer_all(conn, token, *, unknown=False):
    values = {
        "scope": "visible_only",
        "protein_type": "unknown" if unknown else "chicken",
        "protein_portion": "unknown" if unknown else "one_palm",
        "starch_portion": "unseen_unknown" if unknown else "none",
        "vegetable_portion": "unknown" if unknown else "two_bowl",
        "cooking_oil": "unknown" if unknown else "light",
        "sauce_level": "unknown" if unknown else "half",
    }
    draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    for index, (field, value) in enumerate(values.items(), start=1):
        draft = _apply_answer(
            conn, token, field, value, event_id=f"ANSWER-{index}-{token}"
        )
    return draft


def test_button_state_machine_collects_every_required_confirmation(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        expected_steps = [
            "scope", "protein_type", "protein_portion", "starch_portion",
            "vegetable_portion", "cooking_oil", "sauce_level",
        ]
        choices = {
            "scope": "visible_only", "protein_type": "chicken",
            "protein_portion": "one_palm", "starch_portion": "none",
            "vegetable_portion": "two_bowl", "cooking_oil": "light",
            "sauce_level": "half",
        }
        for index, expected in enumerate(expected_steps, start=1):
            assert next_meal_photo_step(draft) == expected
            options = meal_photo_step_options(token, expected, version=draft["version"])
            assert options and all(
                option["data"].startswith(f"mp:v1:{token}:{draft['version']}:answer:{expected}:")
                for option in options
            )
            draft = _apply_answer(
                conn, token, expected, choices[expected], event_id=f"STATE-{index}"
            )
        assert next_meal_photo_step(draft) == "complete"


def test_estimate_uses_ranges_and_keeps_unknown_as_na(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        result = _answer_all(conn, token)
        assert result["status"] == "estimated"
        assert result["estimate"]["starch_exchange"] == {"min": 0.0, "max": 0.0, "basis": "user_confirmed_none"}
        assert result["estimate"]["protein_total_exchange"]["min"] > 0
        assert result["estimate"]["vegetable_exchange"]["max"] >= result["estimate"]["vegetable_exchange"]["min"]
        assert result["estimate"]["calories_kcal"] is None
        assert result["estimate"]["formal_status"] == "pending_review_not_counted"

        bubble = build_meal_photo_estimate_bubble(result)
        text = "\n".join(flatten_text(bubble))
        assert "照片估算｜尚未計入正式份量" in text
        assert "熱量：NA" in text
        assert "主食：0份（使用者確認沒有）" in text
        assert "待營養師審核" in text


def test_unknown_answers_render_na_and_never_zero(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        result = _answer_all(conn, token, unknown=True)
        assert result["estimate"]["starch_exchange"] is None
        assert result["estimate"]["protein_total_exchange"] is None
        assert result["estimate"]["vegetable_exchange"] is None
        text = "\n".join(flatten_text(build_meal_photo_estimate_bubble(result)))
        assert "主食：NA（待確認）" in text
        assert "蛋白質食物：NA（待確認）" in text
        assert "蔬菜：NA（待確認）" in text
        assert "主食：0份" not in text


def test_daily_pending_count_is_user_date_scoped_and_excludes_cancelled(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload(),
            consumed_at="2026-07-22T23:59:00+08:00",
        )
        other_day = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M2", payload=sample_payload(),
            consumed_at="2026-07-23T00:01:00+08:00",
        )
        save_meal_photo_draft(
            conn, user_id="U2", source_message_id="M3", payload=sample_payload(),
            consumed_at="2026-07-22T12:00:00+08:00",
        )
        cancelled = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M4", payload=sample_payload(),
            consumed_at="2026-07-22T12:00:00+08:00",
        )
        apply_meal_photo_action(
            conn, event_id="CANCEL-DAILY", user_id="U1", token=cancelled,
            expected_version=1, action="cancel",
        )
        expired = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M5", payload=sample_payload(),
            consumed_at="2026-07-22T12:30:00+08:00",
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (expired,),
        )
        conn.commit()

        assert daily_pending_meal_photo_count(conn, user_id="U1", date_iso="2026-07-22") == 1
        expired_row = conn.execute(
            "SELECT status,observed_payload_json FROM pending_meal_photo_drafts WHERE token=?",
            (expired,),
        ).fetchone()
        assert expired_row == ("expired", "{}")
        assert daily_pending_meal_photo_count(conn, user_id="U1", date_iso="2026-07-23") == 1
        assert token != other_day


def test_durable_action_event_replays_exact_result_and_rejects_collision(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        first = apply_meal_photo_action(
            conn, event_id="EVT1", user_id="U1", token=token,
            expected_version=1, action="answer", field="scope", value="visible_only",
        )
        assert first["replayed"] is False
        assert first["result"] == {"kind": "question", "step": "protein_type", "version": 2}
        replay = apply_meal_photo_action(
            conn, event_id="EVT1", user_id="U1", token=token,
            expected_version=1, action="answer", field="scope", value="visible_only",
        )
        assert replay["replayed"] is True
        assert replay["result"] == first["result"]
        assert replay["draft"]["version"] == 2
        with pytest.raises(ValueError, match="事件識別碼衝突"):
            apply_meal_photo_action(
                conn, event_id="EVT1", user_id="U2", token=token,
                expected_version=1, action="answer", field="scope", value="unknown",
            )
        with pytest.raises(ValueError, match="畫面已更新"):
            apply_meal_photo_action(
                conn, event_id="EVT2", user_id="U1", token=token,
                expected_version=1, action="answer", field="protein_type", value="chicken",
            )


def test_durable_actions_finalize_estimate_atomically(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        version = 1
        answers = (
            ("scope", "visible_only"), ("protein_type", "none"),
            ("starch_portion", "none"), ("vegetable_portion", "two_bowl"),
            ("cooking_oil", "unknown"), ("sauce_level", "unknown"),
        )
        result = None
        for index, (field, value) in enumerate(answers, 1):
            applied = apply_meal_photo_action(
                conn, event_id=f"EVT{index}", user_id="U1", token=token,
                expected_version=version, action="answer", field=field, value=value,
            )
            result = applied["result"]
            version = result["version"]
        assert result["kind"] == "estimate"
        assert result["version"] == 7
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["status"] == "estimated"
        assert draft["estimate"]["protein_total_exchange"] == {
            "min": 0.0, "max": 0.0, "basis": "user_confirmed_none"
        }
        event_count = conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0]
        assert event_count == len(answers)


def test_action_rolls_back_draft_when_event_insert_fails(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        conn.execute(
            """CREATE TRIGGER fail_meal_event BEFORE INSERT ON meal_photo_events
               BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
            apply_meal_photo_action(
                conn, event_id="ROLLBACK-1", user_id="U1", token=token,
                expected_version=1, action="answer", field="scope", value="visible_only",
            )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["version"] == 1
        assert draft["status"] == "awaiting_confirmation"
        assert draft["answers"]["scope"] is None
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_meal_event")
        conn.commit()
        applied = apply_meal_photo_action(
            conn, event_id="ROLLBACK-1", user_id="U1", token=token,
            expected_version=1, action="answer", field="scope", value="visible_only",
        )
        assert applied["draft"]["version"] == 2


def test_two_connections_cannot_apply_same_stale_version(tmp_path):
    db = tmp_path / "meal-photo.db"
    with sqlite3.connect(db) as setup:
        token = save_meal_photo_draft(
            setup, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
    with sqlite3.connect(db, timeout=5) as first, sqlite3.connect(db, timeout=5) as second:
        assert get_meal_photo_draft(first, user_id="U1", token=token)["version"] == 1
        assert get_meal_photo_draft(second, user_id="U1", token=token)["version"] == 1
        apply_meal_photo_action(
            first, event_id="RACE-A", user_id="U1", token=token,
            expected_version=1, action="answer", field="scope", value="visible_only",
        )
        with pytest.raises(ValueError, match="畫面已更新"):
            apply_meal_photo_action(
                second, event_id="RACE-B", user_id="U1", token=token,
                expected_version=1, action="answer", field="scope", value="has_unseen",
            )
        final = get_meal_photo_draft(second, user_id="U1", token=token)
        assert final["version"] == 2
        assert final["answers"]["scope"] == "visible_only"


def test_schema_v1_migrates_to_versioned_events_without_dropping_drafts(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-v1.db") as conn:
        conn.executescript(
            """
            CREATE TABLE meal_photo_schema_versions (
                component TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO meal_photo_schema_versions VALUES('meal_photo_system',1,'old');
            CREATE TABLE pending_meal_photo_drafts (
                token TEXT PRIMARY KEY,user_id TEXT NOT NULL,source_message_id TEXT NOT NULL DEFAULT '',
                source_image_ref TEXT NOT NULL DEFAULT '',observed_payload_json TEXT NOT NULL,
                answers_json TEXT NOT NULL DEFAULT '{}',estimate_json TEXT NOT NULL DEFAULT '{}',
                meal_slot TEXT NOT NULL DEFAULT '',consumed_at TEXT NOT NULL DEFAULT '',
                consumed_time_source TEXT NOT NULL DEFAULT 'line_timestamp',status TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at TEXT NOT NULL,
                retired_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO pending_meal_photo_drafts VALUES(
                'abc123def456','U1','M1','','{}','{}','{}','','2026-07-22T12:00:00+08:00',
                'line_timestamp','cancelled','old','old','2099-01-01T00:00:00+08:00','old'
            );
            """
        )
        ensure_meal_photo_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_meal_photo_drafts)")}
        version = conn.execute(
            "SELECT version FROM meal_photo_schema_versions WHERE component='meal_photo_system'"
        ).fetchone()[0]
        draft = conn.execute(
            "SELECT user_id,source_message_id,version FROM pending_meal_photo_drafts WHERE token='abc123def456'"
        ).fetchone()
        event_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meal_photo_events'"
        ).fetchone()
    assert "version" in columns
    assert version == 2
    assert draft == ("U1", "M1", 1)
    assert event_table == ("meal_photo_events",)


def test_durable_cancel_scrubs_content_but_retains_image_reference_for_delete(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ref = "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload(),
            source_image_ref=ref,
        )
        first = apply_meal_photo_action(
            conn, event_id="CANCEL1", user_id="U1", token=token,
            expected_version=1, action="cancel",
        )
        assert first["result"]["source_image_ref"] == ref
        assert first["draft"]["status"] == "cancelled"
        assert first["draft"]["payload"] == {}
        assert first["draft"]["source_image_ref"] == ref
        replay = apply_meal_photo_action(
            conn, event_id="CANCEL1", user_id="U1", token=token,
            expected_version=1, action="cancel",
        )
        assert replay["replayed"] is True
        assert replay["result"] == first["result"]
