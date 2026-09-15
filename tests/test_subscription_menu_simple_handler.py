import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


def _event(uid="U" + "a" * 32, message_id="menu-complete-1"):
    return SimpleNamespace(
        reply_token="synthetic-reply-token",
        source=SimpleNamespace(user_id=uid),
        message=SimpleNamespace(id=message_id, text="查看菜單"),
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
    assert "2026/10/01" not in messages[0].text
