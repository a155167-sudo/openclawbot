import json
import os
import sqlite3
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


def _event(message_id, text, uid="U-NAV-CUSTOMER"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=uid),
        reply_token=f"reply-{message_id}",
    )


def test_function_menu_command_returns_usable_entry_flex_without_ai_or_quota(monkeypatch):
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda *_: (_ for _ in ()).throw(AssertionError("navigation must not consume quota")))
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("navigation must not invoke AI")))
    monkeypatch.setattr(server, "resolve_customer_health_services", lambda _uid: {"available": True, "training_available": True, "health_check_url": None})
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-ENTRY-1", "功能選單"))

    assert len(replies) == 1
    token, message = replies[0]
    assert token == "reply-NAV-ENTRY-1"
    payload = message.as_json_dict()
    assert payload["type"] == "flex"
    assert payload["altText"] == "功能選單"
    action_texts = [
        node["action"]["text"]
        for node in payload["contents"]["body"]["contents"]
        if node.get("type") == "box"
        for node in node.get("contents", [])
        if node.get("type") == "button"
    ]
    assert action_texts == [
        "首頁", "搜尋", "我要紀錄飲食", "重選常吃", "查看菜單", "健康服務",
    ]


def test_search_menu_exact_click_uses_registered_catalog_browse_without_logging_or_quota(
    tmp_path, monkeypatch,
):
    db = tmp_path / "search-entry.db"
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "resolve_customer_health_services", lambda _uid: {"available": False})
    monkeypatch.setattr(
        server, "check_permission_and_quota",
        lambda *_: (_ for _ in ()).throw(AssertionError("search browse must not consume quota")),
    )
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search browse must not invoke AI")),
    )
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.sync_menu_to_food_catalog()
    server.processed_messages.clear()

    server.handle_message(_event("SEARCH-ENTRY-MENU", "功能選單"))
    menu = replies[-1][1].as_json_dict()["contents"]
    first_row = next(
        node for node in menu["body"]["contents"]
        if node.get("type") == "box" and node.get("layout") == "horizontal"
    )
    search_action = first_row["contents"][1]["action"]
    assert search_action == {"type": "message", "label": "搜尋餐點", "text": "搜尋"}

    server.handle_message(_event("SEARCH-ENTRY-CLICK", search_action["text"]))

    result = replies[-1][1].as_json_dict()
    rendered = json.dumps(result, ensure_ascii=False)
    assert result["type"] == "flex"
    assert "一日樂食菜單" in rendered
    assert "請選擇分類查看餐點" in rendered
    assert '"text": "搜尋 便當"' in rendered
    assert len(replies) == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_non_vip_cannot_open_customer_function_menu(monkeypatch):
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-DENY-1", "功能選單"))

    assert replies == []


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_health_services_command_only_renders_resolved_training_and_checkup_routes(monkeypatch):
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "resolve_customer_health_services", lambda _uid, **_kwargs: {
        "available": True,
        "training_available": True,
        "health_check_url": "https://liff.line.me/2009251085-customerCheckup",
    })
    monkeypatch.setattr(server, "check_permission_and_quota", lambda *_: (_ for _ in ()).throw(AssertionError("navigation must not consume quota")))
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("navigation must not invoke AI")))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-HEALTH-1", "健康服務"))

    assert len(replies) == 1
    payload = replies[0][1].as_json_dict()
    assert payload["type"] == "flex"
    actions = [node["action"] for node in _walk(payload) if node.get("type") == "button"]
    assert {action.get("text") for action in actions if action.get("type") == "message"} == {"運動"}
    assert {action.get("uri") for action in actions if action.get("type") == "uri"} == {
        "https://liff.line.me/2009251085-customerCheckup"
    }


def test_health_services_command_fails_closed_when_no_service_is_eligible(monkeypatch):
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "resolve_customer_health_services", lambda _uid, **_kwargs: {
        "available": False, "training_available": False, "health_check_url": None,
    })
    monkeypatch.setattr(server, "check_permission_and_quota", lambda *_: (_ for _ in ()).throw(AssertionError("navigation must not consume quota")))
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("navigation must not invoke AI")))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-HEALTH-2", "健康服務"))

    assert len(replies) == 1
    payload = replies[0][1].as_json_dict()
    assert payload["type"] == "text"
    assert "目前沒有符合資格" in payload["text"]


def test_sport_command_rechecks_training_access_before_building_training_cards(monkeypatch):
    replies = []
    service_checks = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "resolve_customer_health_services", lambda _uid, **kwargs: service_checks.append(kwargs) or {
        "available": False, "training_available": False, "health_check_url": None,
    })
    monkeypatch.setattr(server, "build_sport_carousel_flex", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must gate before builder")))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-SPORT-DENY-1", "運動"))

    assert service_checks == [{"force_refresh": True}]
    assert len(replies) == 1
    assert "目前沒有符合資格" in replies[0][1].as_json_dict()["text"]


def test_eligible_sport_command_uses_one_fresh_entitlement_snapshot(monkeypatch):
    replies = []
    dashboard_reads = []
    dashboard = {"has_training_service": True, "today_workout": "輕鬆跑 30 分鐘", "future_days": []}
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    server._customer_health_service_cache.clear()
    monkeypatch.setattr(server, "get_dashboard_data", lambda uid: dashboard_reads.append(uid) or dashboard)
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-SPORT-ALLOW-1", "運動"))

    assert dashboard_reads == ["U-NAV-CUSTOMER"]
    assert len(replies) == 1
    assert replies[0][1].as_json_dict()["type"] == "flex"


def test_record_meal_entry_opens_guidance_without_ai_or_quota(monkeypatch):
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda *_: (_ for _ in ()).throw(AssertionError("entry guidance must not consume quota")))
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("entry guidance must not invoke AI")))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append((token, message)))
    server.processed_messages.clear()

    server.handle_message(_event("NAV-RECORD-START-1", "我要紀錄飲食"))

    assert len(replies) == 1
    payload = replies[0][1].as_json_dict()
    assert payload["type"] == "text"
    assert "餐別、餐點和份量" in payload["text"]
    assert "照片" in payload["text"]
