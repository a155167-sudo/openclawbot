import sqlite3
from types import SimpleNamespace

import pytest
import server


PREFIX_TEMPLATES = (
    "我要記錄飲食 {}",
    "記錄一下飲食 {}",
    "幫我記錄我今天吃了 {}",
    "幫我記一下我喝了 {}",
    "幫我記錄飲食 {}",
    "幫我記一下 {}",
    "我吃了 {}",
    "我今天喝了 {}",
    "早餐吃了 {}",
)


def _event(message_id, text, user_id="U-PREFIX"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
        webhook_event_id=f"webhook-{message_id}",
    )


def _setup(tmp_path, monkeypatch, quota=4):
    db = tmp_path / "prefix.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES (?,?,?,?,?,?,?)""",
            (
                "U-PREFIX", quota, 10, server.tw_today().isoformat(),
                "vip", "2099-12-31", quota,
            ),
        )
        conn.commit()
    return db


def _estimate(request):
    return {
        "food_name": request["food_name"],
        "portion_assumption": "1份",
        "calories_kcal": {"estimate": 300, "min": 250, "max": 350},
        "protein_g": {"estimate": 15, "min": 10, "max": 20},
        "provenance": {
            "provider": "offline", "model": "mock", "method": "text_meal_estimate"
        },
    }


@pytest.mark.parametrize("template", PREFIX_TEMPLATES)
@pytest.mark.parametrize(
    "body",
    ("我沒吃鮪魚蛋吐司", "請問鮪魚蛋吐司熱量", "請問鮪魚蛋吐司熱量？"),
)
def test_every_supported_prefix_blocks_unsafe_body_before_catalog_or_ai(
    tmp_path, monkeypatch, template, body,
):
    db = _setup(tmp_path, monkeypatch)
    catalog_calls = []
    provider_calls = []
    general_ai_calls = []
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server,
        "_natural_food_candidates",
        lambda *_a, **_k: catalog_calls.append(True) or pytest.fail("catalog called"),
    )
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(request) or pytest.fail("provider called"),
    )
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_a, **_k: general_ai_calls.append(True) or pytest.fail("general AI called"),
    )
    server.processed_messages.clear()

    server._handle_message_impl(_event("blocked", template.format(body)))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-PREFIX'"
        ).fetchone()[0] == 4
    assert catalog_calls == []
    assert provider_calls == []
    assert general_ai_calls == []
    assert len(replies) == 1


@pytest.mark.parametrize("template", PREFIX_TEMPLATES)
def test_every_supported_prefix_still_reaches_normal_positive_handler(
    tmp_path, monkeypatch, template,
):
    db = _setup(tmp_path, monkeypatch)
    catalog_calls = []
    provider_calls = []
    general_ai_calls = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_a: None)
    monkeypatch.setattr(
        server,
        "_natural_food_candidates",
        lambda *_a, **_k: (catalog_calls.append(True) or ([], "none")),
    )
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(dict(request)) or _estimate(request),
    )
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_a, **_k: general_ai_calls.append(True) or pytest.fail("general AI called"),
    )
    server.processed_messages.clear()

    server._handle_message_impl(_event("positive", template.format("鮪魚蛋吐司")))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-PREFIX'"
        ).fetchone()[0] == 3
    assert len(catalog_calls) == 1
    assert len(provider_calls) == 1
    assert provider_calls[0]["food_name"] == "鮪魚蛋吐司"
    assert general_ai_calls == []


def test_suffix_form_still_reaches_normal_positive_handler(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    provider_calls = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_a: None)
    monkeypatch.setattr(server, "_natural_food_candidates", lambda *_a, **_k: ([], "none"))
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(dict(request)) or _estimate(request),
    )
    server.processed_messages.clear()

    server._handle_message_impl(_event("suffix", "鮪魚蛋吐司吃了1份"))

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-PREFIX'"
        ).fetchone()[0] == 3
    assert provider_calls == [{
        "food_name": "鮪魚蛋吐司", "amount": 1.0,
        "unit": "serving", "meal_slot": "",
    }]


def test_body_extractor_is_the_single_grammar_contract_for_gate_and_parser(monkeypatch):
    calls = []

    def extract(_text):
        calls.append(_text)
        return "", "安全餐"

    monkeypatch.setattr(server, "_extract_natural_food_log_body", extract)

    assert server._blocked_explicit_food_log_semantics("任意命令") is False
    assert server.parse_natural_food_log_intent("任意命令") == {
        "food_name": "安全餐", "amount": None, "unit": "", "meal_slot": "",
    }
    assert calls == ["任意命令", "任意命令", "任意命令"]


def test_bare_food_is_not_misclassified_as_explicit_blocked_semantics():
    assert server._blocked_explicit_food_log_semantics("我沒吃鮪魚蛋吐司") is False
    assert server._blocked_explicit_food_log_semantics("請問鮪魚蛋吐司熱量？") is False
