import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


def _event(uid="U" + "a" * 32, message_id="menu-complete-1", text="查看菜單"):
    return SimpleNamespace(
        reply_token="synthetic-reply-token",
        source=SimpleNamespace(user_id=uid),
        message=SimpleNamespace(id=message_id, text=text),
    )


def _four_week_menu(long_name_length=120):
    blocks = []
    for day in range(1, 29):
        week = (day - 1) // 7 + 1
        blocks.append(
            f"【第{week}週】\n2026/10/{day:02d}（週一）\n"
            f"午：午餐{day:02d}-" + "甲" * long_name_length + "\n"
            f"晚：晚餐{day:02d}-" + "乙" * long_name_length
        )
    return "\n\n".join(blocks)


def test_view_subscription_menu_replies_with_all_owned_snapshot_dates_in_line_safe_chunks(monkeypatch):
    replies = []
    menu = _four_week_menu()
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server,
        "get_subscription_menu_access",
        lambda _uid: ("active", menu, 56, "2026-10-31"),
    )
    monkeypatch.setattr(server, "get_active_subscription_order_id", lambda _uid: 123)
    monkeypatch.setattr(server, "CUSTOMER_RESCHEDULE_LIFF_ENABLED", True)
    monkeypatch.setattr(server, "CUSTOMER_RESCHEDULE_LIFF_ID", "2011528194-td43IPq1")
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda token, messages: replies.append((token, messages)),
    )
    server.processed_messages.clear()

    server.handle_message(_event())

    assert len(replies) == 1
    messages = replies[0][1]
    assert isinstance(messages, list)
    assert 1 <= len(messages) <= 5
    texts = [message.text for message in messages]
    assert all(len(text) <= 5000 for text in texts)
    quick_reply = messages[-1].quick_reply
    assert quick_reply is not None
    assert [(item.action.label, getattr(item.action, "text", None)) for item in quick_reply.items] == [
        ("互換餐點", "我要互換兩餐"),
        ("餐點改期", None),
    ]
    assert quick_reply.items[1].action.uri == "https://liff.line.me/2011528194-td43IPq1?order_id=123"
    joined = "\n".join(texts)
    for day in range(1, 29):
        assert joined.count(f"2026/10/{day:02d}") == 1
        assert joined.count(f"午：午餐{day:02d}-") == 1
        assert joined.count(f"晚：晚餐{day:02d}-") == 1
    assert "2026/10/01" in texts[0]
    assert "2026/10/28" in texts[-1]
    assert "課：" not in joined
    assert "高碳補糖" not in joined


def test_view_subscription_menu_sanitizes_legacy_training_lines_without_changing_dishes(monkeypatch):
    replies = []
    legacy = (
        "✅ 4 週訓練課表已生成！碳循環菜單已依強度調整 🎯\n"
        "2026/09/24（週四）🥗低碳\n"
        "午：鮭魚低碳\n晚：豬肉沙拉\n"
        "課：主動恢復（散步/伸展）\n"
        "🔥 高強度日 = 高碳補糖，🥗 低強度/休息日 = 低碳燃脂"
    )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server,
        "get_subscription_menu_access",
        lambda _uid: ("active", legacy, 2, "2026-10-31"),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda token, messages: replies.append(messages),
    )
    server.processed_messages.clear()

    server.handle_message(_event(message_id="legacy-menu-1"))

    messages = replies[0] if isinstance(replies[0], list) else [replies[0]]
    texts = [message.text for message in messages]
    joined = "\n".join(texts)
    assert "2026/09/24（週四）" in joined
    assert "午：鮭魚低碳" in joined
    assert "晚：豬肉沙拉" in joined
    assert "課：" not in joined
    assert "高碳補糖" not in joined
    assert "🥗低碳" not in joined


def test_view_subscription_menu_reports_over_capacity_instead_of_sending_truncated_menu(monkeypatch):
    replies = []
    oversized_menu = "\n\n".join(
        f"2026/10/{day:02d}（週一）\n午：{'甲' * 4900}\n晚：{'乙' * 4900}"
        for day in range(1, 7)
    )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server,
        "get_subscription_menu_access",
        lambda _uid: ("active", oversized_menu, 12, "2026-10-31"),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda token, messages: replies.append(messages),
    )
    server.processed_messages.clear()

    server.handle_message(_event(message_id="menu-over-capacity-1"))

    assert len(replies) == 1
    messages = replies[0] if isinstance(replies[0], list) else [replies[0]]
    assert len(messages) == 1
    assert "超過 LINE 單次可完整顯示" in messages[0].text
    assert "沒有截斷" in messages[0].text
    assert messages[0].quick_reply is None
    assert "2026/10/01" not in messages[0].text


import pytest


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("我要申請延餐", "#延餐 10/15 午餐 -> 10/17 晚餐"),
        ("我要互換兩餐", "10/15 午餐與 10/17 晚餐互換"),
    ],
)
def test_subscription_menu_actions_open_help_only_for_active_plan(monkeypatch, command, expected):
    replies = []
    access_calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "get_subscription_menu_access", lambda uid: access_calls.append(uid) or ("active", "menu", 10, "2026-10-31"))
    monkeypatch.setattr(server, "check_permission_and_quota", lambda *_: (_ for _ in ()).throw(AssertionError("menu help must not consume quota")))
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("menu help must not invoke AI")))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append(message))
    server.processed_messages.clear()

    server.handle_message(_event(message_id=f"menu-help-{command}", text=command))

    assert access_calls == ["U" + "a" * 32]
    assert len(replies) == 1
    assert replies[0].type == "text"
    assert expected in replies[0].text


def test_defer_request_is_denied_without_active_plan_before_sheet_or_db_write(tmp_path, monkeypatch):
    db_path = tmp_path / "defer-inactive.db"
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    replies = []
    access_calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "get_subscription_menu_access", lambda uid: access_calls.append(uid) or ("inactive", None, 0, ""))
    monkeypatch.setattr(server, "create_deferred_meal_request", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("inactive user must not create a request")))
    monkeypatch.setattr(server, "gc", None)
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, message: replies.append(message))
    server.processed_messages.clear()

    server.handle_message(_event(message_id="defer-inactive-1", text="#延餐 10/15 午餐 -> 10/17 晚餐"))

    assert access_calls == ["U" + "a" * 32]
    assert len(replies) == 1
    assert "有效的本期包月方案" in replies[0].text


def test_admin_can_parse_liff_generated_request_id_and_staging_gate(monkeypatch):
    request_id = "meal-reschedule-11ed6cf2-523c-48f5-9390-5cf321bdcd68"
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", False)
    monkeypatch.setattr(server, "CUSTOMER_RESCHEDULE_LIFF_ENABLED", True)
    assert server._parse_pair_reschedule_command(
        f"#核准雙餐改期 {request_id}", "admin"
    ) == (request_id,)
