import json
import sqlite3
from types import SimpleNamespace

from semantic_meal_pipeline import dispatch_semantic_meal_text


def test_existing_router_wins_before_semantic_ai_and_quota():
    calls = []
    handled = dispatch_semantic_meal_text(
        text="我的方案", user_id="U1", message_id="M1",
        route_existing=lambda text: calls.append(("route", text)) or True,
        reply=lambda payload: calls.append(("reply", payload)),
        claim_batch=lambda *_: calls.append(("claim",)) or {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: calls.append(("parse",)),
        find_reference_nutrition=lambda *_: None,
        estimate_per_100=lambda *_: [],
    )
    assert handled is True
    assert calls == [("route", "我的方案")]


def test_handler_emits_complete_multi_item_draft_without_foodlog_write():
    replies = []
    handled = dispatch_semantic_meal_text(
        text="午餐青菜100g和茶飲200ml", user_id="U1", message_id="M2",
        route_existing=lambda _text: False,
        reply=replies.append,
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B2"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "午餐", "clarification": "",
            "items": [
                {"food_name": "青菜", "amount": 100, "unit": "g", "portion_assumption": ""},
                {"food_name": "茶飲", "amount": 200, "unit": "ml", "portion_assumption": ""},
            ],
            "provider": "fake", "model": "parser", "raw_trace_id": "P",
        },
        find_reference_nutrition=lambda _request: None,
        estimate_per_100=lambda misses, _batch_id: [
            {"item_id": item["item_id"], "food_name": item["food_name"],
             "basis_amount": 100, "basis_unit": item["unit"],
             "nutrition": {"calories_kcal": 10, "protein_g": 1, "fat_g": 0, "carbohydrate_g": 1},
             "provider": "fake", "model": "n", "raw_trace_id": str(index)}
            for index, item in enumerate(misses)
        ],
    )
    assert handled is True
    assert len(replies) == 1
    assert replies[0]["status"] == "draft"
    assert len(replies[0]["items"]) == 2
    assert replies[0]["writes_food_log"] is False


def test_not_meal_is_not_replied_or_swallowed_by_semantic_dispatch():
    replies = []
    handled = dispatch_semantic_meal_text(
        text="今天天氣如何", user_id="U1", message_id="CHAT-1",
        route_existing=lambda _text: False,
        reply=replies.append,
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: {"intent": "other", "meal_slot": "", "items": [],
                                    "clarification": ""},
        find_reference_nutrition=lambda _: None,
        estimate_per_100=lambda *_: [],
    )
    assert handled is False
    assert replies == []


class _SequenceCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[len(self.calls) - 1]
        return SimpleNamespace(
            id=f"trace-{len(self.calls)}", model="test-model",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False), refusal=None,
            ))],
        )


class _IncompleteCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="trace-incomplete", model="test-model", choices=[])


def test_registered_handler_native_quota_durable_draft_and_replay(tmp_path, monkeypatch):
    import server

    db = tmp_path / "semantic-registered.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage(user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit) "
            "VALUES ('U-SEM',2,10,?,'vip','2099-12-31',2)", (today,),
        )
        conn.commit()
    fallback_item = {
        "item_id": "item-0", "food_name": "燕麥奶", "basis_amount": 100,
        "basis_unit": "ml",
        "calories_kcal": {"estimate": 45, "min": 35, "max": 55, "unit": "kcal"},
        "protein_g": {"estimate": 1.2, "min": 0.8, "max": 1.6, "unit": "g"},
        "fat_g": {"estimate": 1.5, "min": 1, "max": 2, "unit": "g"},
        "carbohydrate_g": {"estimate": 7, "min": 5, "max": 9, "unit": "g"},
    }
    completions = _SequenceCompletions([
        {"intent": "meal_log", "meal_slot": "午餐", "clarification": "", "items": [
            {"food_name": "燕麥奶", "amount": 300, "unit": "ml", "portion_assumption": "明示300ml"}
        ]},
        {"items": [fallback_item]},
    ])
    monkeypatch.setattr(server, "client", SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(server, "parse_natural_food_log_intent", lambda _text: None)
    monkeypatch.setattr(server, "classify_service_scope_text", lambda *_: "service")
    monkeypatch.setattr(server, "build_dashboard_flex", lambda _uid: server.TextSendMessage(text="DASH"))
    monkeypatch.setattr(server, "_refresh_health_check_after_food_log", lambda *_a, **_k: None)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    event = SimpleNamespace(
        message=SimpleNamespace(id="SEM-REGISTERED-1", text="剛剛午餐享用了300cc燕麥奶"),
        source=SimpleNamespace(user_id="U-SEM", type="user"),
        reply_token="reply-1", webhook_event_id="webhook-1",
    )

    server.processed_messages.clear()
    server.handle_message(event)
    server.processed_messages.clear()
    server.handle_message(event)

    assert len(completions.calls) == 2
    assert len(replies) == 2
    assert replies[0].as_json_dict() == replies[1].as_json_dict()
    rendered = json.dumps(replies[0].as_json_dict(), ensure_ascii=False)
    assert "營養估算草稿（尚未記錄）" in rendered
    assert "燕麥奶" in rendered and "300 ml" in rendered
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U-SEM'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM pending_text_meal_estimates").fetchone()[0] == "pending"
        assert conn.execute("SELECT status FROM semantic_meal_batches").fetchone()[0] == "completed"


def test_registered_incomplete_provider_is_durable_unknown_and_replays_without_throw_or_retry(
    tmp_path, monkeypatch,
):
    import server

    db = tmp_path / "semantic-incomplete.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage(user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit) "
            "VALUES ('U-UNKNOWN',2,10,?,'vip','2099-12-31',2)", (today,),
        )
        conn.commit()

    completions = _IncompleteCompletions()
    monkeypatch.setattr(
        server, "client", SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply)
    )
    event = SimpleNamespace(
        message=SimpleNamespace(id="SEM-INCOMPLETE-1", text="無糖豆漿500ml"),
        source=SimpleNamespace(user_id="U-UNKNOWN", type="user"),
        reply_token="reply-incomplete", webhook_event_id="webhook-incomplete",
    )

    server.processed_messages.clear()
    server.handle_message(event)
    server.processed_messages.clear()
    server.handle_message(event)

    expected = "⚠️ AI估算結果待確認，原餐點草稿未變。額度未退還；請勿重試，需人工確認。"
    assert [reply.text for reply in replies] == [expected, expected]
    assert len(completions.calls) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-UNKNOWN'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM semantic_meal_batches WHERE source_message_id='SEM-INCOMPLETE-1'"
        ).fetchone()[0] == "provider_unknown"
        assert conn.execute(
            "SELECT state FROM text_meal_provider_attempts WHERE user_id='U-UNKNOWN'"
        ).fetchone()[0] == "unknown"
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
