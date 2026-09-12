import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nutrition_system import (
    _confirmed_result,
    daily_consumed_totals,
    daily_food_summary,
    exchange_approval_hash,
    exchange_approval_payload_is_valid,
    insert_approved_meal_photo_log,
    user_confirmed_meal_photo_estimate_is_valid,
    user_confirmed_meal_photo_food_trust_projection,
    user_confirmed_meal_photo_trust_projection,
)
from meal_photo_system import (
    build_meal_photo_confirmation_bubble,
    ensure_meal_photo_schema,
    get_meal_photo_draft,
    get_meal_photo_draft_for_admin,
    list_pending_meal_photo_reviews,
    build_meal_photo_estimate_bubble,
    apply_meal_photo_action,
    apply_meal_photo_review_action,
    daily_pending_meal_photo_count,
    next_meal_photo_review_step,
    meal_photo_review_options,
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
    assert "高麗菜" in text
    assert "青花菜" in text
    assert "上方棕色主菜種類不明" in text
    assert "主食：NA（待確認；畫面未見不代表沒有吃）" in text
    assert "蛋白質食物：NA（待確認" in text
    assert "烹調用油／醬汁：NA（待確認；無法判定）" in text
    assert "熱量與交換份：NA（尚未估算）" in text
    assert "主食：0份" not in text
    assert "蛋白質：0份" not in text
    actions = str(bubble)
    assert "mp:v1:abc123def456:1:start" in actions
    assert "mp:v1:abc123def456:1:cancel" in actions
    assert "mp:v1:abc123def456:1:request_add" in actions
    assert "'label': '移除'" in actions
    assert "'label': '❌'" not in actions
    assert "'type': 'postback'" in actions


def test_meal_photo_item_edit_is_versioned_and_persists_in_observed_payload(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-edit.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="EDIT-1", payload=sample_payload()
        )

        removed = apply_meal_photo_action(
            conn, event_id="REMOVE-1", user_id="U1", token=token,
            expected_version=1, action="remove_item", value="青花菜",
        )
        assert removed["result"] == {"kind": "updated", "version": 2}
        assert [item["name"] for item in removed["draft"]["payload"]["visible_items"]] == ["高麗菜"]

        waiting = apply_meal_photo_action(
            conn, event_id="REQUEST-ADD-1", user_id="U1", token=token,
            expected_version=2, action="request_add",
        )
        assert waiting["result"] == {"kind": "ask_item_name", "version": 3}
        assert waiting["draft"]["status"] == "awaiting_item_name"

        added = apply_meal_photo_action(
            conn, event_id="ADD-1", user_id="U1", token=token,
            expected_version=3, action="add_item", field="protein", value="雞胸肉",
        )
        assert added["result"] == {"kind": "updated", "version": 4}
        assert added["draft"]["status"] == "awaiting_confirmation"
        assert [item["name"] for item in added["draft"]["payload"]["visible_items"]] == ["高麗菜", "雞胸肉"]
        assert added["draft"]["payload"]["visible_items"][-1]["category"] == "protein"


def test_remove_item_only_removes_one_matching_duplicate(tmp_path):
    payload = sample_payload()
    payload["visible_items"] = [
        {"name": "雞胸肉", "category": "protein", "confidence": 0.91},
        {"name": "雞胸肉", "category": "protein", "confidence": 0.83},
        {"name": "青花菜", "category": "vegetable", "confidence": 0.88},
    ]
    with sqlite3.connect(tmp_path / "meal-photo-duplicate.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="DUP-1", payload=payload
        )
        removed = apply_meal_photo_action(
            conn, event_id="REMOVE-DUP-1", user_id="U1", token=token,
            expected_version=1, action="remove_item", value="雞胸肉",
        )

    names = [item["name"] for item in removed["draft"]["payload"]["visible_items"]]
    assert names == ["雞胸肉", "青花菜"]


def test_cancel_add_returns_draft_to_confirmation_without_changing_items(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-cancel-add.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="CANCEL-ADD-1", payload=sample_payload()
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-ADD-CANCEL", user_id="U1", token=token,
            expected_version=1, action="request_add",
        )
        cancelled = apply_meal_photo_action(
            conn, event_id="CANCEL-ADD-1", user_id="U1", token=token,
            expected_version=2, action="cancel_add",
        )

    assert cancelled["result"] == {"kind": "updated", "version": 3}
    assert cancelled["draft"]["status"] == "awaiting_confirmation"
    assert [item["name"] for item in cancelled["draft"]["payload"]["visible_items"]] == [
        "高麗菜", "青花菜"
    ]


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


def _apply_answer(conn, token, field, value, *, event_id, user_id="U1"):
    draft = get_meal_photo_draft(conn, user_id=user_id, token=token)
    return apply_meal_photo_action(
        conn, event_id=event_id, user_id=user_id, token=token,
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


def _answer_all(conn, token, *, unknown=False, user_id="U1"):
    values = {
        "scope": "visible_only",
        "protein_type": "unknown" if unknown else "chicken",
        "protein_portion": "unknown" if unknown else "one_palm",
        "protein_more": "done",
        "starch_portion": "unseen_unknown" if unknown else "none",
        "vegetable_portion": "unknown" if unknown else "two_bowl",
        "cooking_oil": "unknown" if unknown else "light",
        "sauce_level": "unknown" if unknown else "half",
    }
    draft = get_meal_photo_draft(conn, user_id=user_id, token=token)
    for index, (field, value) in enumerate(values.items(), start=1):
        draft = _apply_answer(
            conn, token, field, value, event_id=f"ANSWER-{index}-{token}", user_id=user_id,
        )
    return draft


def test_customer_final_confirmation_creates_one_canonical_unapproved_log(tmp_path):
    with sqlite3.connect(tmp_path / "customer-confirmed-photo.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-CONFIRM-1",
            payload=sample_payload(), source_image_ref="nutrition-image:kept.jpg",
            meal_slot="午餐", consumed_at="2026-09-12T12:00:00+08:00",
        )
        estimated = _answer_all(conn, token)
        assert estimated["status"] == "estimated"
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        card = build_meal_photo_estimate_bubble(estimated)
        assert any(
            item.get("action", {}).get("data") ==
            f"mp:v1:{token}:{estimated['version']}:confirm_estimate"
            for item in card["footer"]["contents"]
        )

        confirmed = apply_meal_photo_action(
            conn, event_id="CONFIRM-EVENT-1", user_id="U1", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        log_id = confirmed["result"]["log_id"]
        assert confirmed["result"]["kind"] == "recorded"
        assert confirmed["draft"]["status"] == "user_confirmed"
        assert confirmed["draft"]["confirmed_log_id"] == log_id
        assert conn.execute("SELECT COUNT(*) FROM food_catalog").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_exchange_approvals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM nutrition_sheet_outbox").fetchone()[0] == 2
        row = conn.execute(
            """SELECT approved_exchange_json,exchange_approval_id,nutrition_snapshot_json,
                      trust_type,source_image_ref FROM food_logs WHERE log_id=?""", (log_id,)
        ).fetchone()
        assert row == ("{}", "", "{}", "user_confirmed_ai_estimate", "nutrition-image:kept.jpg")
        assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        replay_projection = _confirmed_result(conn, log_id, already_confirmed=True)
        assert replay_projection["log"]["exchange_review_status"] == "user_confirmed_ai_estimate"
        assert replay_projection["log"]["trust_type"] == "user_confirmed_ai_estimate"
        assert replay_projection["log"]["approved_exchange"] == {}
        assert replay_projection["log"]["nutrition"] == {}
        assert daily_consumed_totals(
            conn, user_id="U1", date_iso="2026-09-12"
        )["starch_exchange"] == 0
        summary = daily_food_summary(conn, user_id="U1", date_iso="2026-09-12")
        assert summary["pending_reviews"] == 0
        assert summary["foods"][0]["calories_kcal"] is None
        assert summary["foods"][0]["trust_type"] == "user_confirmed_ai_estimate"
        assert list_pending_meal_photo_reviews(conn) == []
        assert daily_pending_meal_photo_count(conn, user_id="U1", date_iso="2026-09-12") == 0


def test_customer_confirmation_replays_same_log_for_same_or_new_event(tmp_path):
    with sqlite3.connect(tmp_path / "customer-confirm-replay.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-REPLAY", payload=sample_payload()
        )
        estimated = _answer_all(conn, token)
        kwargs = dict(
            user_id="U1", token=token, expected_version=estimated["version"],
            action="confirm_estimate",
        )
        first = apply_meal_photo_action(conn, event_id="CONFIRM-REPLAY-1", **kwargs)
        same = apply_meal_photo_action(conn, event_id="CONFIRM-REPLAY-1", **kwargs)
        second_button = apply_meal_photo_action(conn, event_id="CONFIRM-REPLAY-2", **kwargs)
        assert same["replayed"] is True
        assert first["result"]["log_id"] == same["result"]["log_id"] == second_button["result"]["log_id"]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM nutrition_sheet_outbox").fetchone()[0] == 2



def test_user_confirmed_validator_rejects_replay_event_substituted_into_trust_payload(tmp_path):
    with sqlite3.connect(tmp_path / "original-confirmation-anchor.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-ORIGINAL-EVENT", payload=sample_payload()
        )
        estimated = _answer_all(conn, token)
        kwargs = dict(
            user_id="U1", token=token, expected_version=estimated["version"],
            action="confirm_estimate",
        )
        first = apply_meal_photo_action(conn, event_id="CONFIRM-ORIGINAL", **kwargs)
        replay = apply_meal_photo_action(conn, event_id="CONFIRM-REPEAT", **kwargs)
        log_id = first["result"]["log_id"]
        assert replay["result"]["log_id"] == log_id
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE token=? AND action='confirm_estimate'",
            (token,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT original_confirmation_event_id FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()[0] == "CONFIRM-ORIGINAL"
        assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)

        payload = json.loads(conn.execute(
            "SELECT trust_payload_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0])
        payload["confirmation_event_id"] = "CONFIRM-REPEAT"
        from nutrition_system import _canonical_json_hash
        conn.execute(
            "UPDATE food_logs SET trust_payload_json=?,trust_hash=? WHERE log_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),
             _canonical_json_hash(payload), log_id),
        )
        conn.commit()

        assert not user_confirmed_meal_photo_estimate_is_valid(conn, log_id)

        payload["confirmation_event_id"] = "CONFIRM-ORIGINAL"
        conn.execute(
            "UPDATE food_logs SET trust_payload_json=?,trust_hash=? WHERE log_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),
             _canonical_json_hash(payload), log_id),
        )
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts DROP COLUMN original_confirmation_event_id"
        )
        conn.commit()
        assert not user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        assert user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate"
        )["trust_type"] == "untrusted_user_confirmed_ai_estimate"


@pytest.mark.parametrize("anchor", ["", "CONFIRM-WRONG"])
def test_user_confirmed_validator_requires_nonempty_matching_original_event_anchor(tmp_path, anchor):
    with sqlite3.connect(tmp_path / f"original-anchor-{anchor or 'empty'}.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-ANCHOR", payload=sample_payload()
        )
        estimated = _answer_all(conn, token)
        result = apply_meal_photo_action(
            conn, event_id="CONFIRM-ANCHOR", user_id="U1", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        log_id = result["result"]["log_id"]
        assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET original_confirmation_event_id=? WHERE token=?",
            (anchor, token),
        )
        conn.commit()
        assert not user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        assert user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate"
        )["trust_type"] == "untrusted_user_confirmed_ai_estimate"


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("food_logs", "trust_type", ""),
        ("food_logs", "trust_type", "forged"),
        ("food_logs", "trust_payload_json", "{}"),
        ("food_logs", "trust_hash", ""),
        ("food_catalog", "source_type", "custom"),
        ("food_catalog", "exchange_review_status", "pending_review"),
        ("food_catalog", "verification_status", "approved"),
    ],
)
def test_new_lane_single_field_tamper_stays_untrusted_in_all_local_consumers(
    tmp_path, table, column, value
):
    with sqlite3.connect(tmp_path / f"new-lane-{table}-{column}.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-LANE",
            payload=sample_payload(), consumed_at="2026-09-12T12:00:00+08:00",
        )
        estimated = _answer_all(conn, token)
        result = apply_meal_photo_action(
            conn, event_id="CONFIRM-LANE", user_id="U1", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        log_id = result["result"]["log_id"]
        food_id = conn.execute(
            "SELECT food_id FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0]
        key = log_id if table == "food_logs" else food_id
        id_column = "log_id" if table == "food_logs" else "food_id"
        conn.execute(f"UPDATE {table} SET {column}=? WHERE {id_column}=?", (value, key))
        conn.commit()

        trust = user_confirmed_meal_photo_trust_projection(conn, log_id, value if column == "trust_type" else "user_confirmed_ai_estimate")
        assert trust["trust_type"] == "untrusted_user_confirmed_ai_estimate"
        replay = _confirmed_result(conn, log_id, already_confirmed=True)["log"]
        assert replay["trust_type"] == "untrusted_user_confirmed_ai_estimate"
        assert replay["suggested_exchange"] == {}
        summary = daily_food_summary(conn, user_id="U1", date_iso="2026-09-12")
        assert summary["pending_reviews"] == 0
        assert summary["foods"][0]["calories_kcal"] is None
        assert summary["foods"][0]["protein_g"] is None


def test_catalog_trust_ignores_unrelated_cross_owner_log_when_owner_log_is_valid(tmp_path):
    with sqlite3.connect(tmp_path / "catalog-cross-owner.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-CATALOG", payload=sample_payload()
        )
        estimated = _answer_all(conn, token)
        result = apply_meal_photo_action(
            conn, event_id="CONFIRM-CATALOG", user_id="U1", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        log_id = result["result"]["log_id"]
        food_id = conn.execute(
            "SELECT food_id FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0]
        columns = [row[1] for row in conn.execute("PRAGMA table_info(food_logs)") if row[1] != "log_id"]
        column_sql = ",".join(columns)
        conn.execute(
            f"INSERT INTO food_logs(log_id,{column_sql}) "
            f"SELECT ?,{column_sql} FROM food_logs WHERE log_id=?",
            ("cross-owner-log", log_id),
        )
        conn.execute(
            "UPDATE food_logs SET user_id='U2',trust_hash='tampered' WHERE log_id='cross-owner-log'"
        )
        conn.commit()
        assert user_confirmed_meal_photo_food_trust_projection(conn, food_id)["integrity_status"] == "verified"

        conn.execute("UPDATE food_logs SET trust_hash='tampered' WHERE log_id=?", (log_id,))
        conn.commit()
        assert user_confirmed_meal_photo_food_trust_projection(conn, food_id)["trust_type"] == (
            "untrusted_user_confirmed_ai_estimate"
        )


@pytest.mark.parametrize("tamper", ["cross_owner", "cross_draft"])
def test_customer_confirmation_replay_rejects_log_from_another_draft(tmp_path, tamper):
    with sqlite3.connect(tmp_path / f"customer-confirm-{tamper}.db") as conn:
        owners = ("U1", "U2") if tamper == "cross_owner" else ("U1", "U1")
        confirmed = []
        for index, owner in enumerate(owners, start=1):
            token = save_meal_photo_draft(
                conn, user_id=owner, source_message_id=f"PHOTO-{tamper}-{index}",
                payload=sample_payload(),
            )
            estimated = _answer_all(conn, token, user_id=owner)
            result = apply_meal_photo_action(
                conn, event_id=f"CONFIRM-{tamper}-{index}", user_id=owner,
                token=token, expected_version=estimated["version"], action="confirm_estimate",
            )
            confirmed.append((token, estimated["version"], result["result"]["log_id"]))

        token, old_version, _own_log = confirmed[1]
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET confirmed_log_id=? WHERE token=? AND user_id=?",
            (confirmed[0][2], token, owners[1]),
        )
        conn.commit()

        with pytest.raises(ValueError, match="完整性驗證失敗"):
            apply_meal_photo_action(
                conn, event_id=f"REPLAY-{tamper}", user_id=owners[1], token=token,
                expected_version=old_version, action="confirm_estimate",
            )


@pytest.mark.parametrize("tamper", ["draft_token", "confirmation_event_id", "log_version"])
def test_user_confirmed_log_validator_binds_draft_event_and_version(tmp_path, tamper):
    with sqlite3.connect(tmp_path / f"confirmation-binding-{tamper}.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id=f"PHOTO-BIND-{tamper}", payload=sample_payload()
        )
        estimated = _answer_all(conn, token)
        result = apply_meal_photo_action(
            conn, event_id=f"CONFIRM-BIND-{tamper}", user_id="U1", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        log_id = result["result"]["log_id"]
        assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)

        if tamper == "log_version":
            conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            conn.execute("UPDATE food_logs SET version=version+1 WHERE log_id=?", (log_id,))
        else:
            payload = json.loads(conn.execute(
                "SELECT trust_payload_json FROM food_logs WHERE log_id=?", (log_id,)
            ).fetchone()[0])
            payload[tamper] = "forged-binding"
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            from nutrition_system import _canonical_json_hash
            conn.execute(
                "UPDATE food_logs SET trust_payload_json=?,trust_hash=? WHERE log_id=?",
                (encoded, _canonical_json_hash(payload), log_id),
            )
        conn.commit()

        assert not user_confirmed_meal_photo_estimate_is_valid(conn, log_id)


def test_daily_summary_keeps_tampered_user_estimate_nutrition_na(tmp_path):
    with sqlite3.connect(tmp_path / "tampered-summary-na.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-SUMMARY-TAMPER",
            payload=sample_payload(), consumed_at="2026-09-12T12:00:00+08:00",
        )
        estimated = _answer_all(conn, token)
        result = apply_meal_photo_action(
            conn, event_id="CONFIRM-SUMMARY-TAMPER", user_id="U1", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        conn.execute(
            "UPDATE food_logs SET trust_hash='tampered' WHERE log_id=?",
            (result["result"]["log_id"],),
        )
        conn.commit()

        summary = daily_food_summary(conn, user_id="U1", date_iso="2026-09-12")
        food = summary["foods"][0]
        assert summary["pending_reviews"] == 0
        assert food["calories_kcal"] is None
        assert food["protein_g"] is None
        assert food["trust_type"] == "untrusted_user_confirmed_ai_estimate"
        assert food["trust_integrity_status"] == "integrity_verification_failed"
        projection = _confirmed_result(conn, result["result"]["log_id"], already_confirmed=True)
        assert projection["log"]["exchange_review_status"] == "untrusted_user_confirmed_ai_estimate"
        assert projection["log"]["trust_type"] == "untrusted_user_confirmed_ai_estimate"
        assert projection["log"]["trust_integrity_status"] == "integrity_verification_failed"
        assert projection["log"]["trust_schema_version"] == ""
        assert projection["log"]["suggested_exchange"] == {}
        assert projection["log"]["exchange"] == {}


def test_customer_confirmation_fails_closed_on_owner_or_estimate_tamper(tmp_path):
    with sqlite3.connect(tmp_path / "customer-confirm-tamper.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-TAMPER", payload=sample_payload()
        )
        estimated = _answer_all(conn, token)
        with pytest.raises(ValueError, match="找不到"):
            apply_meal_photo_action(
                conn, event_id="CROSS-USER", user_id="U2", token=token,
                expected_version=estimated["version"], action="confirm_estimate",
            )
        estimate = dict(estimated["estimate"])
        estimate["starch_exchange"] = {"min": 99, "max": 99, "basis": "forged"}
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET estimate_json=? WHERE token=?",
            (json.dumps(estimate), token),
        )
        conn.commit()
        with pytest.raises(ValueError, match="估算完整性"):
            apply_meal_photo_action(
                conn, event_id="TAMPERED-CONFIRM", user_id="U1", token=token,
                expected_version=estimated["version"], action="confirm_estimate",
            )
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE event_id='TAMPERED-CONFIRM'").fetchone()[0] == 0


def test_optional_second_protein_is_explicitly_added_summed_and_persisted(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-multi-protein.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="MULTI-PROTEIN-1",
            payload=sample_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        for index, (field, value) in enumerate((
            ("scope", "visible_only"),
            ("protein_type", "chicken"),
            ("protein_portion", "one_palm"),
        ), start=1):
            draft = _apply_answer(
                conn, token, field, value, event_id=f"MP-BASE-{index}"
            )

        assert next_meal_photo_step(draft) == "protein_more"
        assert [item["label"] for item in meal_photo_step_options(
            token, "protein_more", version=draft["version"]
        )] == ["就這一種", "＋還有其他蛋白質"]

        for index, (field, value) in enumerate((
            ("protein_more", "add"),
            ("protein_extra_type", "fish"),
            ("protein_extra_portion", "half_palm"),
            ("protein_more", "done"),
            ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"),
            ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            draft = _apply_answer(
                conn, token, field, value, event_id=f"MP-REST-{index}"
            )

        assert draft["estimate"]["protein_total_exchange"] == {
            "min": 3.0, "max": 5.0,
            "basis": "summed_hand_portion_ranges_v2",
        }
        assert [item["type"] for item in draft["estimate"]["protein_items"]] == [
            "chicken", "fish",
        ]
        confirmation_text = "\n".join(flatten_text(build_meal_photo_estimate_bubble(draft)))
        assert "雞肉：約2～3份" in confirmation_text
        assert "魚類：約1～2份" in confirmation_text
        assert "蛋白質食物合計：約3～5份" in confirmation_text

        review = apply_meal_photo_review_action(
            conn, event_id="MP-REVIEW", user_id="U1", admin_user_id="ADMIN",
            required_admin_user_id="ADMIN", token=token,
            expected_version=draft["version"], action="start",
        )
        review_values = {
            "protein_class": "medium", "protein_exchange": "4",
            "starch_exchange": "0", "vegetable_exchange": "3",
            "milk_exchange": "0", "fruit_exchange": "0",
        }
        index = 0
        while next_meal_photo_review_step(review["draft"]) != "complete":
            index += 1
            field = next_meal_photo_review_step(review["draft"])
            review = apply_meal_photo_review_action(
                conn, event_id=f"MP-REVIEW-{index}", user_id="U1",
                admin_user_id="ADMIN", required_admin_user_id="ADMIN",
                token=token, expected_version=review["draft"]["version"],
                action="set", field=field, value=review_values[field],
            )
        approved = apply_meal_photo_review_action(
            conn, event_id="MP-APPROVE", user_id="U1", admin_user_id="ADMIN",
            required_admin_user_id="ADMIN", token=token,
            expected_version=review["draft"]["version"], action="approve",
        )
        canonical = json.loads(conn.execute(
            "SELECT exchange_snapshot_json FROM food_logs WHERE log_id=?",
            (approved["result"]["log_id"],),
        ).fetchone()[0])
        assert [item["type"] for item in canonical["protein_items"]] == [
            "chicken", "fish",
        ]
        assert canonical["protein_total_exchange"] == {
            "min": 3.0, "max": 5.0,
            "basis": "summed_hand_portion_ranges_v2",
        }
        assert canonical["protein_medium_exchange"] == 4.0

        approval_id = approved["result"]["approval_id"]
        log_id = approved["result"]["log_id"]
        approval_json = conn.execute(
            "SELECT approved_exchange_json FROM food_exchange_approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0]
        applied_json = conn.execute(
            "SELECT approved_exchange_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0]
        consumed_at = conn.execute(
            "SELECT consumed_at FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0]
        local_date = datetime.fromisoformat(consumed_at).astimezone(
            timezone(timedelta(hours=8))
        ).date().isoformat()
        approved_totals = daily_consumed_totals(
            conn, user_id="U1", date_iso=local_date
        )
        assert approved_totals["protein_medium_exchange"] == 4
        applied_payload = json.loads(applied_json)
        applied_payload["protein_items"][1]["exchange"]["min"] = "1.0"
        conn.execute(
            "UPDATE food_logs SET approved_exchange_json=? WHERE log_id=?",
            (json.dumps(applied_payload, ensure_ascii=False, sort_keys=True), log_id),
        )
        conn.commit()
        tampered_log = _confirmed_result(conn, log_id, already_confirmed=True)
        assert tampered_log["log"]["exchange_review_status"] == "pending_review"
        assert tampered_log["log"]["exchange_approval_id"] == ""
        tampered_totals = daily_consumed_totals(
            conn, user_id="U1", date_iso=local_date
        )
        assert tampered_totals["protein_medium_exchange"] == 0
        tampered_summary = daily_food_summary(
            conn, user_id="U1", date_iso=local_date
        )
        assert tampered_summary["pending_reviews"] == 1
        conn.execute(
            "UPDATE food_logs SET approved_exchange_json=? WHERE log_id=?",
            (applied_json, log_id),
        )
        approval_payload = json.loads(approval_json)
        approval_payload["protein_total_exchange"]["min"] = 99
        conn.execute(
            "UPDATE food_exchange_approvals SET approved_exchange_json=? WHERE approval_id=?",
            (json.dumps(approval_payload, ensure_ascii=False, sort_keys=True), approval_id),
        )
        conn.commit()
        tampered_approval = _confirmed_result(conn, log_id, already_confirmed=True)
        assert tampered_approval["log"]["exchange_review_status"] == "pending_review"
        assert tampered_approval["log"]["exchange_approval_id"] == ""

        for table, column, original_json in (
            ("food_exchange_approvals", "approval_id", approval_json),
            ("food_logs", "log_id", applied_json),
        ):
            conn.execute(
                f"UPDATE {table} SET approved_exchange_json=? WHERE {column}=?",
                (original_json, approval_id if table == "food_exchange_approvals" else log_id),
            )
            conn.commit()
            malformed_values = ["{", "[]", "null"]
            wrong_scalar = json.loads(original_json)
            wrong_scalar["protein_medium_exchange"] = "oops"
            malformed_values.append(json.dumps(wrong_scalar, ensure_ascii=False, sort_keys=True))
            for malformed in malformed_values:
                conn.execute(
                    f"UPDATE {table} SET approved_exchange_json=? WHERE {column}=?",
                    (malformed, approval_id if table == "food_exchange_approvals" else log_id),
                )
                conn.commit()
                failed_closed = _confirmed_result(conn, log_id, already_confirmed=True)
                assert failed_closed["log"]["exchange_review_status"] == "pending_review"
                assert daily_consumed_totals(
                    conn, user_id="U1", date_iso=local_date
                )["protein_medium_exchange"] == 0
                assert daily_food_summary(
                    conn, user_id="U1", date_iso=local_date
                )["pending_reviews"] == 1
            conn.execute(
                f"UPDATE {table} SET approved_exchange_json=? WHERE {column}=?",
                (original_json, approval_id if table == "food_exchange_approvals" else log_id),
            )
            conn.commit()

        invalid_approval = json.loads(approval_json)
        invalid_applied = json.loads(applied_json)
        invalid_approval["protein_medium_exchange"] = "4.0"
        invalid_applied["protein_medium_exchange"] = "4.0"
        fingerprint = conn.execute(
            "SELECT food_fingerprint FROM food_exchange_approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0]
        invalid_hash = exchange_approval_hash(
            fingerprint, "meal-photo-admin-v2", invalid_approval
        )
        conn.execute(
            "UPDATE food_exchange_approvals SET approved_exchange_json=?,approved_exchange_hash=? "
            "WHERE approval_id=?",
            (json.dumps(invalid_approval), invalid_hash, approval_id),
        )
        conn.execute(
            "UPDATE food_logs SET approved_exchange_json=? WHERE log_id=?",
            (json.dumps(invalid_applied), log_id),
        )
        conn.commit()
        recomputed = _confirmed_result(conn, log_id, already_confirmed=True)
        assert recomputed["log"]["exchange_review_status"] == "pending_review"
        assert daily_consumed_totals(
            conn, user_id="U1", date_iso=local_date
        )["protein_medium_exchange"] == 0.0
        assert daily_food_summary(
            conn, user_id="U1", date_iso=local_date
        )["pending_reviews"] == 1
        with pytest.raises(ValueError, match="核准重播紀錄驗證失敗"):
            apply_meal_photo_review_action(
                conn,
                event_id="MP-APPROVE",
                user_id="U1",
                admin_user_id="ADMIN",
                required_admin_user_id="ADMIN",
                token=token,
                expected_version=review["draft"]["version"],
                action="approve",
            )


def test_legacy_v1_approval_hash_remains_compatible_with_old_exchange_only_scope():
    payload = {
        "protein_medium_exchange": 4,
        "protein_items": [{"type": "chicken", "portion": "one_palm"}],
    }
    original = exchange_approval_hash("fingerprint", "meal-photo-admin-v1", payload)
    payload["protein_items"][0]["type"] = "fish"
    payload["protein_total_exchange"] = {"min": 99, "max": 99, "basis": "tampered"}
    assert exchange_approval_hash("fingerprint", "meal-photo-admin-v1", payload) == original

    numeric_string = {"protein_medium_exchange": "4"}
    assert exchange_approval_hash(
        "fingerprint", "meal-photo-admin-v1", numeric_string
    ) == original
    assert exchange_approval_payload_is_valid("meal-photo-admin-v1", numeric_string)

    malformed = {"protein_medium_exchange": "oops"}
    assert exchange_approval_hash(
        "fingerprint", "meal-photo-admin-v1", malformed
    ) != original
    assert not exchange_approval_payload_is_valid("meal-photo-admin-v1", malformed)
    for invalid_value in (True, {}, []):
        invalid = {"protein_medium_exchange": invalid_value}
        assert exchange_approval_hash(
            "fingerprint", "meal-photo-admin-v1", invalid
        ) != original
        assert not exchange_approval_payload_is_valid("meal-photo-admin-v1", invalid)


def test_v2_approval_hash_rejects_numeric_strings_and_presence_changes():
    payload: dict[str, object] = {
        "milk_exchange": 0.0,
        "protein_low_exchange": 0.0,
        "protein_medium_exchange": 4.0,
        "protein_high_exchange": 0.0,
        "starch_exchange": 0.0,
        "vegetable_exchange": 3.0,
        "fruit_exchange": 0.0,
        "fat_exchange": 0.0,
    }
    original = exchange_approval_hash("fingerprint", "meal-photo-admin-v2", payload)

    numeric_string = dict(payload)
    numeric_string["protein_medium_exchange"] = "4.0"
    assert exchange_approval_hash(
        "fingerprint", "meal-photo-admin-v2", numeric_string
    ) != original

    missing_zero = dict(payload)
    del missing_zero["milk_exchange"]
    assert exchange_approval_hash(
        "fingerprint", "meal-photo-admin-v2", missing_zero
    ) != original

    explicit_null = dict(payload)
    explicit_null["protein_total_exchange"] = None
    assert exchange_approval_hash(
        "fingerprint", "meal-photo-admin-v2", explicit_null
    ) != original

    for invalid_value in ("4.0", True, None, math.nan, math.inf):
        invalid = dict(payload)
        invalid["protein_medium_exchange"] = invalid_value
        # Recomputing an unkeyed digest must not make malformed v2 data valid.
        exchange_approval_hash("fingerprint", "meal-photo-admin-v2", invalid)
        assert not exchange_approval_payload_is_valid("meal-photo-admin-v2", invalid)


def test_v2_approval_hash_distinguishes_nested_absent_from_null():
    payload = {
        "milk_exchange": 0.0,
        "protein_low_exchange": 0.0,
        "protein_medium_exchange": 4.0,
        "protein_high_exchange": 0.0,
        "starch_exchange": 0.0,
        "vegetable_exchange": 3.0,
        "fruit_exchange": 0.0,
        "fat_exchange": 0.0,
        "protein_items": [
            {
                "type": "chicken",
                "portion": "one_palm",
                "exchange": {"min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"},
            },
            {
                "type": "fish",
                "portion": "half_palm",
                "exchange": {"min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1"},
            },
        ],
        "protein_total_exchange": {
            "min": 3.0, "max": 5.0, "basis": "summed_hand_portion_ranges_v2",
        },
    }
    paths = [
        ("protein_items", 0, "type"),
        ("protein_items", 0, "portion"),
        ("protein_items", 0, "exchange", "min"),
        ("protein_items", 0, "exchange", "max"),
        ("protein_items", 0, "exchange", "basis"),
        ("protein_total_exchange", "min"),
        ("protein_total_exchange", "max"),
        ("protein_total_exchange", "basis"),
    ]
    for path in paths:
        missing = json.loads(json.dumps(payload))
        explicit_null = json.loads(json.dumps(payload))
        missing_parent = missing
        null_parent = explicit_null
        for part in path[:-1]:
            missing_parent = missing_parent[part]
            null_parent = null_parent[part]
        missing_parent.pop(path[-1])
        null_parent[path[-1]] = None
        assert exchange_approval_hash(
            "fingerprint", "meal-photo-admin-v2", missing
        ) != exchange_approval_hash(
            "fingerprint", "meal-photo-admin-v2", explicit_null
        )


@pytest.mark.parametrize("protein_type", ["chicken", "unknown"])
def test_duplicate_additional_protein_is_rejected_without_mutating_draft(
    tmp_path, protein_type
):
    with sqlite3.connect(tmp_path / f"meal-photo-duplicate-{protein_type}.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id=f"DUPLICATE-{protein_type}",
            payload=sample_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        for index, (field, value) in enumerate((
            ("scope", "visible_only"),
            ("protein_type", protein_type),
            ("protein_portion", "one_palm"),
            ("protein_more", "add"),
        ), start=1):
            draft = _apply_answer(
                conn, token, field, value, event_id=f"DUPLICATE-BASE-{index}"
            )
        version_before = draft["version"]

        with pytest.raises(ValueError, match="已經選過"):
            _apply_answer(
                conn, token, "protein_extra_type", protein_type,
                event_id=f"DUPLICATE-{protein_type}-EVENT",
            )

        unchanged = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert unchanged["version"] == version_before
        assert unchanged["answers"]["protein_extra_type"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE event_id=?",
            (f"DUPLICATE-{protein_type}-EVENT",),
        ).fetchone()[0] == 0


def test_formal_boundary_rejects_duplicate_unknown_protein_items(tmp_path):
    duplicate_items = [
        {
            "type": "unknown", "portion": "one_palm",
            "exchange": {"min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"},
        },
        {
            "type": "unknown", "portion": "half_palm",
            "exchange": {"min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1"},
        },
    ]
    exact = {
        "milk_exchange": 0, "protein_low_exchange": 0,
        "protein_medium_exchange": 4, "protein_high_exchange": 0,
        "starch_exchange": 0, "vegetable_exchange": 3,
        "fruit_exchange": 0, "fat_exchange": 0,
    }
    with sqlite3.connect(tmp_path / "formal-duplicate-unknown.db") as conn:
        with pytest.raises(ValueError, match="複數蛋白質種類重複"):
            insert_approved_meal_photo_log(
                conn, token="abcdef123456", user_id="U1", reviewer="ADMIN",
                consumed_at="2026-09-12T12:00:00+08:00", meal_slot="lunch",
                source_image_ref="nutrition-image:test.jpg", observed_payload={},
                answers={
                    "protein_type": "unknown",
                    "protein_portion": "one_palm",
                    "protein_items": [
                        {"type": item["type"], "portion": item["portion"]}
                        for item in duplicate_items
                    ],
                },
                exact_exchange=exact,
                estimate={
                    "protein_items": duplicate_items,
                    "protein_total_exchange": {
                        "min": 3.0, "max": 5.0,
                        "basis": "summed_hand_portion_ranges_v2",
                    },
                    "rule_version": "hand-portion-range-v2",
                },
            )


@pytest.mark.parametrize(
    ("target", "bad_value"),
    [
        ("exact", "4.0"),
        ("exact", True),
        ("item_min", "2.0"),
        ("item_min", True),
        ("item_max", math.inf),
        ("total_min", "3.0"),
        ("total_min", True),
        ("total_max", math.nan),
    ],
)
def test_formal_boundary_rejects_noncanonical_v2_numbers_without_writes(
    tmp_path, target, bad_value
):
    items = [
        {
            "type": "chicken", "portion": "one_palm",
            "exchange": {"min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"},
        },
        {
            "type": "fish", "portion": "half_palm",
            "exchange": {"min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1"},
        },
    ]
    exact = {
        "milk_exchange": 0, "protein_low_exchange": 0,
        "protein_medium_exchange": 4, "protein_high_exchange": 0,
        "starch_exchange": 0, "vegetable_exchange": 3,
        "fruit_exchange": 0, "fat_exchange": 0,
    }
    total = {"min": 3.0, "max": 5.0, "basis": "summed_hand_portion_ranges_v2"}
    if target == "exact":
        exact["protein_medium_exchange"] = bad_value
    elif target == "item_min":
        items[0]["exchange"]["min"] = bad_value
    elif target == "item_max":
        items[0]["exchange"]["max"] = bad_value
    elif target == "total_min":
        total["min"] = bad_value
    else:
        total["max"] = bad_value

    with sqlite3.connect(tmp_path / f"formal-number-{target}.db") as conn:
        with pytest.raises(ValueError, match="JSON數字|有限數字"):
            insert_approved_meal_photo_log(
                conn, token="abcdef123456", user_id="U1", reviewer="ADMIN",
                consumed_at="2026-09-12T12:00:00+08:00", meal_slot="lunch",
                source_image_ref="nutrition-image:test.jpg", observed_payload={},
                answers={
                    "protein_type": "chicken", "protein_portion": "one_palm",
                    "protein_items": [
                        {"type": item["type"], "portion": item["portion"]}
                        for item in items
                    ],
                },
                exact_exchange=exact,
                estimate={
                    "protein_items": items,
                    "protein_total_exchange": total,
                    "rule_version": "hand-portion-range-v2",
                },
            )
        assert not conn.in_transaction
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("protein_type", "protein_portion"),
    [("none", None), ("chicken", "half_palm")],
)
def test_formal_boundary_rejects_v2_items_contradicting_top_level_answer(
    tmp_path, protein_type, protein_portion
):
    items = [
        {
            "type": "chicken", "portion": "one_palm",
            "exchange": {"min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"},
        },
        {
            "type": "fish", "portion": "half_palm",
            "exchange": {"min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1"},
        },
    ]
    with sqlite3.connect(tmp_path / f"formal-contradiction-{protein_type}.db") as conn:
        with pytest.raises(ValueError, match="蛋白質明細與主要回答不符"):
            insert_approved_meal_photo_log(
                conn, token="abcdef123456", user_id="U1", reviewer="ADMIN",
                consumed_at="2026-09-12T12:00:00+08:00", meal_slot="lunch",
                source_image_ref="nutrition-image:test.jpg", observed_payload={},
                answers={
                    "protein_type": protein_type,
                    "protein_portion": protein_portion,
                    "protein_items": [
                        {"type": item["type"], "portion": item["portion"]}
                        for item in items
                    ],
                },
                exact_exchange={
                    "milk_exchange": 0, "protein_low_exchange": 0,
                    "protein_medium_exchange": 4, "protein_high_exchange": 0,
                    "starch_exchange": 0, "vegetable_exchange": 3,
                    "fruit_exchange": 0, "fat_exchange": 0,
                },
                estimate={
                    "protein_items": items,
                    "protein_total_exchange": {
                        "min": 3.0, "max": 5.0,
                        "basis": "summed_hand_portion_ranges_v2",
                    },
                    "rule_version": "hand-portion-range-v2",
                },
            )


def test_formal_boundary_rejects_single_item_contradicting_top_level_answer(tmp_path):
    with sqlite3.connect(tmp_path / "formal-single-contradiction.db") as conn:
        with pytest.raises(ValueError, match="蛋白質明細與主要回答不符"):
            insert_approved_meal_photo_log(
                conn, token="abcdef123456", user_id="U1", reviewer="ADMIN",
                consumed_at="2026-09-12T12:00:00+08:00", meal_slot="lunch",
                source_image_ref="nutrition-image:test.jpg", observed_payload={},
                answers={
                    "protein_type": "chicken", "protein_portion": "one_palm",
                    "protein_items": [{"type": "fish", "portion": "half_palm"}],
                },
                exact_exchange={
                    "milk_exchange": 0, "protein_low_exchange": 0,
                    "protein_medium_exchange": 1.5, "protein_high_exchange": 0,
                    "starch_exchange": 0, "vegetable_exchange": 3,
                    "fruit_exchange": 0, "fat_exchange": 0,
                },
                estimate={
                    "protein_total_exchange": {
                        "min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1",
                    },
                    "rule_version": "hand-portion-range-v1",
                },
            )


def test_zero_first_protein_portion_skips_additional_protein_without_mutation(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-zero-first-protein.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="ZERO-FIRST-PROTEIN",
            payload=sample_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        for index, (field, value) in enumerate((
            ("scope", "visible_only"),
            ("protein_type", "chicken"),
            ("protein_portion", "none"),
        ), start=1):
            draft = _apply_answer(
                conn, token, field, value, event_id=f"ZERO-FIRST-{index}"
            )

        assert next_meal_photo_step(draft) == "starch_portion"
        version_before = draft["version"]
        with pytest.raises(ValueError, match="餐點確認步驟不符"):
            _apply_answer(
                conn, token, "protein_more", "add", event_id="ZERO-FIRST-STALE-ADD"
            )
        unchanged = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert unchanged["version"] == version_before
        assert unchanged["answers"]["protein_more"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE event_id='ZERO-FIRST-STALE-ADD'"
        ).fetchone()[0] == 0


def test_old_single_protein_estimate_without_v2_items_remains_approvable(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-legacy-single.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="LEGACY-SINGLE-1",
            payload=sample_payload(),
        )
        legacy_answers = {
            "scope": "visible_only", "protein_type": "chicken",
            "protein_portion": "one_palm", "starch_portion": "none",
            "vegetable_portion": "two_bowl", "cooking_oil": "light",
            "sauce_level": "half",
        }
        legacy_estimate = {
            "calories_kcal": None, "protein_g": None, "fat_g": None,
            "carbohydrate_g": None,
            "protein_total_exchange": {
                "min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1",
            },
            "starch_exchange": {
                "min": 0.0, "max": 0.0, "basis": "user_confirmed_none",
            },
            "vegetable_exchange": {
                "min": 2.0, "max": 4.0, "basis": "hand_portion_range_v1",
            },
            "formal_status": "pending_review_not_counted",
            "rule_version": "hand-portion-range-v1",
        }
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET answers_json=?,estimate_json=?,status='estimated',version=8
               WHERE token=?""",
            (
                json.dumps(legacy_answers, ensure_ascii=False, sort_keys=True),
                json.dumps(legacy_estimate, ensure_ascii=False, sort_keys=True),
                token,
            ),
        )
        conn.commit()

        review = apply_meal_photo_review_action(
            conn, event_id="LEGACY-START", user_id="U1", admin_user_id="ADMIN",
            required_admin_user_id="ADMIN", token=token,
            expected_version=8, action="start",
        )
        values = {
            "protein_class": "medium", "protein_exchange": "2.5",
            "starch_exchange": "0", "vegetable_exchange": "3",
            "milk_exchange": "0", "fruit_exchange": "0",
        }
        index = 0
        while next_meal_photo_review_step(review["draft"]) != "complete":
            index += 1
            field = next_meal_photo_review_step(review["draft"])
            review = apply_meal_photo_review_action(
                conn, event_id=f"LEGACY-SET-{index}", user_id="U1",
                admin_user_id="ADMIN", required_admin_user_id="ADMIN",
                token=token, expected_version=review["draft"]["version"],
                action="set", field=field, value=values[field],
            )
        approved = apply_meal_photo_review_action(
            conn, event_id="LEGACY-APPROVE", user_id="U1", admin_user_id="ADMIN",
            required_admin_user_id="ADMIN", token=token,
            expected_version=review["draft"]["version"], action="approve",
        )
        canonical = json.loads(conn.execute(
            "SELECT exchange_snapshot_json FROM food_logs WHERE log_id=?",
            (approved["result"]["log_id"],),
        ).fetchone()[0])
        assert "protein_items" not in canonical
        assert canonical["protein_medium_exchange"] == 2.5


def test_v2_protein_total_without_items_is_rejected_without_formal_write(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-missing-protein-items.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="MISSING-PROTEIN-ITEMS",
            payload=sample_payload(),
        )
        estimated = _answer_all(conn, token)
        malformed = dict(estimated["estimate"])
        malformed.pop("protein_items", None)
        malformed["protein_total_exchange"] = {
            "min": 3.0, "max": 5.0,
            "basis": "summed_hand_portion_ranges_v2",
        }
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET estimate_json=? WHERE token=?",
            (json.dumps(malformed, ensure_ascii=False, sort_keys=True), token),
        )
        conn.commit()

        review = apply_meal_photo_review_action(
            conn, event_id="MISSING-START", user_id="U1", admin_user_id="ADMIN",
            required_admin_user_id="ADMIN", token=token,
            expected_version=estimated["version"], action="start",
        )
        values = {
            "protein_class": "medium", "protein_exchange": "4",
            "starch_exchange": "0", "vegetable_exchange": "3",
            "milk_exchange": "0", "fruit_exchange": "0",
        }
        while next_meal_photo_review_step(review["draft"]) != "complete":
            field = next_meal_photo_review_step(review["draft"])
            review = apply_meal_photo_review_action(
                conn, event_id=f"MISSING-SET-{field}", user_id="U1",
                admin_user_id="ADMIN", required_admin_user_id="ADMIN",
                token=token, expected_version=review["draft"]["version"],
                action="set", field=field, value=values[field],
            )
        before = conn.execute(
            "SELECT status,version FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
        with pytest.raises(ValueError, match="複數蛋白質明細無效"):
            apply_meal_photo_review_action(
                conn, event_id="MISSING-APPROVE", user_id="U1",
                admin_user_id="ADMIN", required_admin_user_id="ADMIN",
                token=token, expected_version=review["draft"]["version"],
                action="approve",
            )
        after = conn.execute(
            "SELECT status,version FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
        assert after == before
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_v2_answers_cannot_be_downgraded_to_legacy_estimate(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo-v2-downgrade.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="V2-DOWNGRADE",
            payload=sample_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "add"),
            ("protein_extra_type", "fish"),
            ("protein_extra_portion", "half_palm"), ("protein_more", "done"),
            ("starch_portion", "none"), ("vegetable_portion", "two_bowl"),
            ("cooking_oil", "light"), ("sauce_level", "half"),
        ), start=1):
            draft = _apply_answer(
                conn, token, field, value, event_id=f"DOWNGRADE-ANSWER-{index}"
            )
        assert len(draft["answers"]["protein_items"]) == 2
        malformed = dict(draft["estimate"])
        malformed.pop("protein_items")
        malformed["rule_version"] = "hand-portion-range-v1"
        malformed["protein_total_exchange"] = {
            "min": 3.0, "max": 5.0, "basis": "hand_portion_range_v1",
        }
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET estimate_json=? WHERE token=?",
            (json.dumps(malformed, ensure_ascii=False, sort_keys=True), token),
        )
        conn.commit()

        review = apply_meal_photo_review_action(
            conn, event_id="DOWNGRADE-START", user_id="U1", admin_user_id="ADMIN",
            required_admin_user_id="ADMIN", token=token,
            expected_version=draft["version"], action="start",
        )
        values = {
            "protein_class": "medium", "protein_exchange": "4",
            "starch_exchange": "0", "vegetable_exchange": "3",
            "milk_exchange": "0", "fruit_exchange": "0",
        }
        while next_meal_photo_review_step(review["draft"]) != "complete":
            field = next_meal_photo_review_step(review["draft"])
            review = apply_meal_photo_review_action(
                conn, event_id=f"DOWNGRADE-SET-{field}", user_id="U1",
                admin_user_id="ADMIN", required_admin_user_id="ADMIN",
                token=token, expected_version=review["draft"]["version"],
                action="set", field=field, value=values[field],
            )
        before = conn.execute(
            "SELECT status,version FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
        with pytest.raises(ValueError, match="複數蛋白質明細無效"):
            apply_meal_photo_review_action(
                conn, event_id="DOWNGRADE-APPROVE", user_id="U1",
                admin_user_id="ADMIN", required_admin_user_id="ADMIN",
                token=token, expected_version=review["draft"]["version"],
                action="approve",
            )
        assert conn.execute(
            "SELECT status,version FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone() == before
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_button_state_machine_collects_every_required_confirmation(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload()
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        expected_steps = [
            "scope", "protein_type", "protein_portion", "protein_more", "starch_portion",
            "vegetable_portion", "cooking_oil", "sauce_level",
        ]
        choices = {
            "scope": "visible_only", "protein_type": "chicken",
            "protein_portion": "one_palm", "protein_more": "done",
            "starch_portion": "none",
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
        assert "照片估算｜確認後正式記錄" in text
        assert "熱量：NA" in text
        assert "主食：0份（使用者確認沒有）" in text
        assert "不會冒充營養師核准值" in text


@pytest.mark.parametrize(
    ("workflow_version", "allow_admin_review", "expected_heading", "expected_action"),
    [
        ("expert_review_v1", False, "照片估算｜尚未計入正式份量", None),
        ("expert_review_v1", True, "照片估算｜尚未計入正式份量", "start"),
        ("user_confirmed_ai_estimate_v1", False, "照片估算｜確認後正式記錄", "confirm_estimate"),
        ("user_confirmed_ai_estimate_v1", True, "照片估算｜確認後正式記錄", "confirm_estimate"),
    ],
)
def test_estimate_card_actions_match_workflow_and_viewer(
    tmp_path, workflow_version, allow_admin_review, expected_heading, expected_action
):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload(),
            workflow_version=workflow_version,
        )
        draft = _answer_all(conn, token)

    card = build_meal_photo_estimate_bubble(
        draft, allow_admin_review=allow_admin_review
    )
    text = "\n".join(flatten_text(card))
    actions = [
        item.get("action", {}).get("data")
        for item in card.get("footer", {}).get("contents", [])
    ]

    assert expected_heading in text
    if workflow_version == "expert_review_v1":
        assert "待營養師審核，尚未扣入個人營養計畫" in text
        assert "確認後會記入飲食紀錄" not in text
    else:
        assert "確認後會記入飲食紀錄" in text
        assert "待營養師審核" not in text
    if expected_action == "start":
        assert actions == [f"mpr:v1:{token}:{draft['version']}:start"]
    elif expected_action == "confirm_estimate":
        assert actions == [f"mp:v1:{token}:{draft['version']}:confirm_estimate"]
    else:
        assert actions == []
    assert not any("confirm_estimate" in (action or "") for action in actions) or (
        workflow_version == "user_confirmed_ai_estimate_v1"
    )


def test_legacy_customer_card_matches_backend_confirm_rejection(tmp_path):
    with sqlite3.connect(tmp_path / "meal-photo.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload(),
            workflow_version="expert_review_v1",
        )
        draft = _answer_all(conn, token)
        card = build_meal_photo_estimate_bubble(draft)

        assert "footer" not in card
        with pytest.raises(ValueError, match="舊版待審草稿不能由顧客直接記錄"):
            apply_meal_photo_action(
                conn, event_id="LEGACY-CONFIRM", user_id="U1", token=token,
                expected_version=draft["version"], action="confirm_estimate",
            )
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


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


def test_admin_review_selects_exact_values_and_applies_once(tmp_path):
    db = tmp_path / "meal-photo-review.db"
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_ADMIN", source_message_id="M_REVIEW", payload=sample_payload(),
            consumed_at="2026-07-23T12:10:00+08:00", meal_slot="午餐",
        )
        values = (
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "one_half_bowl"),
            ("vegetable_portion", "none"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        )
        for index, (field, value) in enumerate(values, start=1):
            apply_meal_photo_action(
                conn, event_id=f"ANSWER-{index}", user_id="U_ADMIN", token=token,
                expected_version=index, action="answer", field=field, value=value,
            )
        draft = get_meal_photo_draft(conn, user_id="U_ADMIN", token=token)
        assert draft["version"] == 9 and draft["status"] == "estimated"

        with pytest.raises(PermissionError):
            apply_meal_photo_review_action(
                conn, event_id="UNAUTHORIZED", user_id="U_ADMIN", admin_user_id="OTHER",
                required_admin_user_id="U_ADMIN",
                token=token, expected_version=9, action="start",
            )
        assert get_meal_photo_draft(conn, user_id="U_ADMIN", token=token)["version"] == 9

        started = apply_meal_photo_review_action(
            conn, event_id="REVIEW-START", user_id="U_ADMIN", admin_user_id="U_ADMIN",
            required_admin_user_id="U_ADMIN",
            token=token, expected_version=9, action="start",
        )
        assert started["result"] == {"kind": "review_question", "step": "protein_class", "version": 10}
        assert next_meal_photo_review_step(started["draft"]) == "protein_class"

        sequence = (
            ("protein_class", "medium"),
            ("protein_exchange", "2.5"),
            ("starch_exchange", "6"),
            ("milk_exchange", "0"),
            ("fruit_exchange", "0"),
        )
        current = started
        for offset, (field, value) in enumerate(sequence, start=10):
            option_values = {item["value"] for item in meal_photo_review_options(current["draft"], field)}
            assert value in option_values
            current = apply_meal_photo_review_action(
                conn, event_id=f"REVIEW-{field}", user_id="U_ADMIN", admin_user_id="U_ADMIN",
                required_admin_user_id="U_ADMIN",
                token=token, expected_version=offset, action="set", field=field, value=value,
            )
        assert current["result"]["kind"] == "review_ready"
        assert current["draft"]["review"]["vegetable_exchange"] == 0.0

        approved = apply_meal_photo_review_action(
            conn, event_id="REVIEW-APPROVE", user_id="U_ADMIN", admin_user_id="U_ADMIN",
            required_admin_user_id="U_ADMIN",
            token=token, expected_version=15, action="approve",
        )
        assert approved["result"]["kind"] == "approved"
        assert approved["result"]["estimated_nutrition"]["calories_kcal"] == 590.5
        assert approved["result"]["estimated_nutrition"]["protein_g"] == 29.5
        assert approved["draft"]["status"] == "approved"
        assert approved["draft"]["approved_log_id"]
        totals = daily_consumed_totals(conn, user_id="U_ADMIN", date_iso="2026-07-23")
        assert totals["protein_medium_exchange"] == 2.5
        assert totals["starch_exchange"] == 6.0
        assert totals["vegetable_exchange"] == 0.0
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1

        replay = apply_meal_photo_review_action(
            conn, event_id="REVIEW-APPROVE", user_id="U_ADMIN", admin_user_id="U_ADMIN",
            required_admin_user_id="U_ADMIN",
            token=token, expected_version=15, action="approve",
        )
        assert replay["replayed"] is True
        assert replay["result"]["estimated_nutrition"] == approved["result"]["estimated_nutrition"]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1


def test_review_action_rejects_actor_who_is_not_configured_admin():
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(PermissionError, match="管理員限定"):
            apply_meal_photo_review_action(
                conn,
                event_id="FORGED-REVIEW",
                user_id="REGULAR_USER",
                admin_user_id="REGULAR_USER",
                required_admin_user_id="REAL_ADMIN",
                token="abcdef123456",
                expected_version=1,
                action="start",
            )


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
        notification_table = conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='meal_photo_notification_events'"""
        ).fetchone()
        claim_table = conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='meal_photo_notification_claims'"""
        ).fetchone()
    assert "version" in columns
    for col in (
        "review_json", "approved_log_id", "approved_at", "approved_by",
        "original_confirmation_event_id",
    ):
        assert col in columns, f"migration should add {col}"
    assert version == 7
    assert draft == ("U1", "M1", 1)
    assert event_table == ("meal_photo_events",)
    assert notification_table == ("meal_photo_notification_events",)
    assert claim_table == ("meal_photo_notification_claims",)


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


def test_configured_admin_can_review_another_users_meal_and_log_stays_with_owner(tmp_path):
    with sqlite3.connect(tmp_path / "cross-user-review.db") as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M_CUSTOMER", payload=sample_payload(),
            consumed_at="2026-07-23T12:10:00+08:00", meal_slot="午餐",
        )
        draft = _answer_all(conn, token)
        assert draft["status"] == "estimated"

        with pytest.raises(PermissionError, match="管理員限定"):
            get_meal_photo_draft_for_admin(
                conn, token=token, admin_user_id="U_FORGED",
                required_admin_user_id="U_ADMIN",
            )
        admin_draft = get_meal_photo_draft_for_admin(
            conn, token=token, admin_user_id="U_ADMIN",
            required_admin_user_id="U_ADMIN",
        )
        assert admin_draft["user_id"] == "U1"

        started = apply_meal_photo_review_action(
            conn, event_id="CROSS-START", user_id="U1", admin_user_id="U_ADMIN",
            required_admin_user_id="U_ADMIN", token=token,
            expected_version=draft["version"], action="start",
        )
        sequence = (
            ("protein_class", "medium"), ("protein_exchange", "2.5"),
            ("vegetable_exchange", "2"), ("milk_exchange", "0"),
            ("fruit_exchange", "0"),
        )
        current = started
        for field, value in sequence:
            current = apply_meal_photo_review_action(
                conn, event_id=f"CROSS-{field}", user_id="U1",
                admin_user_id="U_ADMIN", required_admin_user_id="U_ADMIN",
                token=token, expected_version=current["draft"]["version"],
                action="set", field=field, value=value,
            )
        approved = apply_meal_photo_review_action(
            conn, event_id="CROSS-APPROVE", user_id="U1",
            admin_user_id="U_ADMIN", required_admin_user_id="U_ADMIN",
            token=token, expected_version=current["draft"]["version"], action="approve",
        )
        assert approved["draft"]["approved_by"] == "U_ADMIN"
        assert daily_consumed_totals(
            conn, user_id="U1", date_iso="2026-07-23"
        )["vegetable_exchange"] == 2.0
        assert daily_consumed_totals(
            conn, user_id="U_ADMIN", date_iso="2026-07-23"
        )["vegetable_exchange"] == 0.0


def test_pending_review_list_includes_estimated_and_in_progress_drafts(tmp_path):
    with sqlite3.connect(tmp_path / "pending-review-list.db") as conn:
        first = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="M1", payload=sample_payload(),
            workflow_version="expert_review_v1",
        )
        _answer_all(conn, first)
        waiting = save_meal_photo_draft(
            conn, user_id="U2", source_message_id="M2", payload=sample_payload()
        )
        pending = list_pending_meal_photo_reviews(conn, limit=10)
        assert [item["token"] for item in pending] == [first]
        assert pending[0]["user_id"] == "U1"
        assert waiting not in [item["token"] for item in pending]
        current = get_meal_photo_draft(conn, user_id="U1", token=first)
        started = apply_meal_photo_review_action(
            conn, event_id="ADMIN-START-PENDING", user_id="U1",
            admin_user_id="U_ADMIN", required_admin_user_id="U_ADMIN",
            token=first, expected_version=current["version"], action="start",
        )
        assert started["draft"]["status"] in {"reviewing", "review_ready"}
        resumed = list_pending_meal_photo_reviews(conn, limit=10)
        assert [item["token"] for item in resumed] == [first]
        assert resumed[0]["status"] == started["draft"]["status"]
