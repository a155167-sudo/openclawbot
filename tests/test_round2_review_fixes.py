import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

import server
from tests.test_nanjing_meal_logging_flow import _postback_actions, _setup, _text_event
from tests.test_round2_meal_draft_liff import _photo_draft


OLD_TEXT_WIRE = re.compile(
    r"tmest:v(?:1|2):([0-9a-f]{24}|[0-9a-f]{32}):(\d+):(confirm|cancel)"
)
OLD_PHOTO_WIRE = re.compile(
    r"mp:v1:([0-9a-f]{12}):(\d+):(confirm_estimate|cancel)"
)


def _fixed_draft(message_id="review-fix"):
    return server.create_fixed_text_meal_draft(
        user_id="U-NANJING",
        message_id=message_id,
        request={
            "food_name": "測試豆漿",
            "amount": 500,
            "unit": "ml",
            "meal_slot": "午餐",
            "calories_kcal": 165,
            "protein_g": 16,
            "fat_g": 7,
            "carbohydrate_g": 9,
        },
    )


def _source_event(message_id, text, *, source_type="user", scope_id=""):
    source = SimpleNamespace(type=source_type, user_id="U-NANJING")
    if source_type == "group":
        source.group_id = scope_id
    elif source_type == "room":
        source.room_id = scope_id
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=source,
        reply_token=f"reply-{message_id}",
        webhook_event_id=f"webhook-{message_id}",
    )


@pytest.mark.parametrize("legacy_length", [24, 32])
def test_liff_edit_atomically_rotates_legacy_token_and_replays_from_old_request(
    tmp_path, monkeypatch, legacy_length
):
    db, _replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_draft(f"legacy-{legacy_length}")
    legacy_token = "a" * legacy_length
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_text_meal_estimates SET token=? WHERE token=?",
            (legacy_token, draft["token"]),
        )
        conn.execute(
            "INSERT INTO text_meal_provider_attempts "
            "(attempt_id,token,user_id,quota_attempt_id,state,provider_started_at,completed_at,error_kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                f"attempt-{legacy_length}", legacy_token, "U-NANJING",
                f"quota-{legacy_length}", "completed", "now", "now", "",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO text_meal_estimate_quota_ledger "
            "(attempt_id,token,user_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"quota-{legacy_length}", legacy_token, "U-NANJING", "charged", "now", "now"),
        )
        conn.commit()

    request = dict(
        user_id="U-NANJING",
        token=legacy_token,
        expected_version=1,
        amount=400,
        unit="ml",
        meal_slot="晚餐",
        nutrition={
            "calories_kcal": 180,
            "protein_g": 17,
            "fat_g": 8,
            "carbohydrate_g": 10,
        },
    )
    first = server.save_text_meal_draft_from_liff(**request)
    replay = server.save_text_meal_draft_from_liff(**request)

    rotated = first["draft"]["token"]
    assert len(rotated) == 40 and rotated != legacy_token
    assert replay["receipt_id"] == first["receipt_id"]
    assert replay["draft"]["token"] == rotated
    actions = _postback_actions(server.build_text_meal_estimate_flex(first["draft"]))
    assert actions and all(action.startswith("tmest:v3:") for action in actions)
    assert not any(OLD_TEXT_WIRE.fullmatch(action) for action in actions)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_text_meal_estimates WHERE token=?", (legacy_token,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT token FROM text_meal_provider_attempts WHERE attempt_id=?",
            (f"attempt-{legacy_length}",),
        ).fetchone()[0] == rotated
        assert conn.execute(
            "SELECT token FROM text_meal_estimate_quota_ledger WHERE attempt_id=?",
            (f"quota-{legacy_length}",),
        ).fetchone()[0] == rotated
        receipt = conn.execute(
            "SELECT draft_token,request_token FROM meal_draft_return_receipts WHERE receipt_id=?",
            (first["receipt_id"],),
        ).fetchone()
        assert receipt == (rotated, legacy_token)


def test_receipt_return_is_user_chat_bound_and_group_replay_does_not_deliver(
    tmp_path, monkeypatch
):
    db, replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_draft("scope-bound")
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING",
        token=draft["token"],
        expected_version=1,
        amount=500,
        unit="ml",
        meal_slot="午餐",
        nutrition={"calories_kcal": 165, "protein_g": 16, "fat_g": 7, "carbohydrate_g": 9},
    )
    server.processed_messages.clear()

    server.handle_message(
        _source_event("WRONG-GROUP", saved["return_command"], source_type="group", scope_id="GROUP-B")
    )

    with sqlite3.connect(db) as conn:
        delivered = conn.execute(
            "SELECT delivered_at FROM meal_draft_return_receipts WHERE receipt_id=?",
            (saved["receipt_id"],),
        ).fetchone()[0]
    assert replies == []
    assert delivered == ""


def test_vip_without_subscription_order_can_return_receipt(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_draft("vip-no-order")
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING",
        token=draft["token"],
        expected_version=1,
        amount=500,
        unit="ml",
        meal_slot="午餐",
        nutrition={"calories_kcal": 165, "protein_g": 16, "fat_g": 7, "carbohydrate_g": 9},
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM subscription_orders WHERE user_id='U-NANJING'"
        ).fetchone()[0] == 0
    server.processed_messages.clear()

    server.handle_message(_source_event("VIP-RETURN", saved["return_command"]))

    with sqlite3.connect(db) as conn:
        delivered = conn.execute(
            "SELECT delivered_at FROM meal_draft_return_receipts WHERE receipt_id=?",
            (saved["receipt_id"],),
        ).fetchone()[0]
    assert len(replies) == 1
    assert delivered


def test_revoked_vip_receipt_is_silent_without_provider_or_database_change(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_draft("revoked-vip")
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING",
        token=draft["token"],
        expected_version=1,
        amount=500,
        unit="ml",
        meal_slot="午餐",
        nutrition={"calories_kcal": 165, "protein_g": 16, "fat_g": 7, "carbohydrate_g": 9},
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE usage SET status='free' WHERE user_id='U-NANJING'")
        conn.commit()
        before = {
            "draft": conn.execute(
                "SELECT token,status,version,estimate_json FROM pending_text_meal_estimates"
            ).fetchall(),
            "receipt": conn.execute(
                "SELECT receipt_id,draft_token,delivered_at FROM meal_draft_return_receipts"
            ).fetchall(),
            "logs": conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0],
        }
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_a, **_k: pytest.fail("revoked receipt must not reach generic AI"),
    )
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda *_a, **_k: pytest.fail("revoked receipt must not reach meal provider"),
    )
    server.processed_messages.clear()

    server.handle_message(_source_event("REVOKED-RETURN", saved["return_command"]))

    with sqlite3.connect(db) as conn:
        after = {
            "draft": conn.execute(
                "SELECT token,status,version,estimate_json FROM pending_text_meal_estimates"
            ).fetchall(),
            "receipt": conn.execute(
                "SELECT receipt_id,draft_token,delivered_at FROM meal_draft_return_receipts"
            ).fetchall(),
            "logs": conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0],
        }
    assert replies == []
    assert after == before


@pytest.mark.parametrize(
    ("sentence", "food_name", "meal_slot"),
    [
        ("午餐我喝了無糖豆漿500ml", "無糖豆漿", "午餐"),
        ("我午餐喝了無糖豆漿500ml", "無糖豆漿", "午餐"),
        ("早餐我吃了蛋餅1份", "蛋餅", "早餐"),
    ],
)
def test_complete_sentence_subject_is_stripped_only_in_grammar_position(
    sentence, food_name, meal_slot
):
    assert server.parse_natural_food_log_intent(sentence) == {
        "food_name": food_name,
        "amount": 500.0 if "500" in sentence else 1.0,
        "unit": "ml" if "500" in sentence else "serving",
        "meal_slot": meal_slot,
    }


def test_subject_like_text_inside_valid_food_name_is_preserved():
    assert server.parse_natural_food_log_intent("午餐喝了我家豆漿500ml")["food_name"] == "我家豆漿"
    assert server.parse_natural_food_log_intent("早餐吃了我的蛋餅1份")["food_name"] == "我的蛋餅"


def test_registered_subject_sentence_uses_structured_route_not_generic_ai(tmp_path, monkeypatch):
    db, _replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    generic_calls = []
    estimate = {
        "schema_version": "text-meal-v2",
        "food_name": "無糖豆漿",
        "portion_assumption": "500 ml",
        "basis_amount": 500,
        "basis_unit": "ml",
        "calories_kcal": {"estimate": 165, "min": 150, "max": 180},
        "protein_g": {"estimate": 16, "min": 15, "max": 17},
        "fat_g": {"estimate": 7, "min": 6, "max": 8},
        "carbohydrate_g": {"estimate": 9, "min": 8, "max": 10},
        "assessment": {"requires_correction": False},
        "provenance": {"provider": "fake", "model": "fake", "method": "text_meal_estimate"},
    }
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request, **_kwargs: provider_calls.append(dict(request)) or estimate,
    )
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda uid, text, operation_key="": generic_calls.append((uid, text, operation_key))
        or ("generic", None),
    )

    server._handle_message_impl(_text_event("SUBJECT-MOBILE", "午餐我喝了無糖豆漿500ml"))

    with sqlite3.connect(db) as conn:
        logs = conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0]
        drafts = conn.execute(
            "SELECT request_json,status FROM pending_text_meal_estimates"
        ).fetchall()
        quota_ops = conn.execute(
            "SELECT COUNT(*) FROM text_meal_estimate_quota_ledger"
        ).fetchone()[0]
    assert provider_calls and provider_calls[0]["amount"] == 500
    assert provider_calls[0]["unit"] == "ml"
    assert generic_calls == []
    assert logs == 0
    assert len(drafts) == 1 and json.loads(drafts[0][0])["amount"] == 500
    assert drafts[0][1] == "pending"
    assert quota_ops == 1


def test_photo_customer_override_uses_rollback_opaque_v2_wire_without_rewriting_draft(
    tmp_path, monkeypatch
):
    db, draft = _photo_draft(tmp_path, monkeypatch)
    items = server.get_meal_draft_for_liff("U1", draft["token"])["items"]
    saved = server.save_photo_meal_draft_from_liff(
        user_id="U1",
        token=draft["token"],
        expected_version=1,
        meal_slot="晚餐",
        items=items,
        nutrition={"calories_kcal": 701, "protein_g": 61, "fat_g": 21, "carbohydrate_g": 82},
    )["draft"]

    actions = [
        item["action"]["data"]
        for item in server.build_meal_photo_estimate_bubble(saved)["footer"]["contents"]
        if item.get("action", {}).get("type") == "postback"
    ]
    assert actions and all(action.startswith("mp:v2:") for action in actions)
    assert not any(OLD_PHOTO_WIRE.fullmatch(action) for action in actions)
    assert saved["token"] == draft["token"]
    with sqlite3.connect(db) as conn:
        persisted = conn.execute(
            "SELECT token,status,version,review_json FROM pending_meal_photo_drafts WHERE token=?",
            (draft["token"],),
        ).fetchone()
    assert persisted[0:3] == (draft["token"], "estimated", 2)
    assert json.loads(persisted[3])["liff_customer_nutrition_override"]["fat_g"] == 21.0
