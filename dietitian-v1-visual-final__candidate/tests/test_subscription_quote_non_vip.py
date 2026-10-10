import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


def _text_event(message_id, text, user_id="U_QUOTE"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


def _install_non_vip_gate(monkeypatch, replies):
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(
        server, "is_text_command_allowed_without_vip", lambda *_args: False
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )


@pytest.fixture(autouse=True)
def _clear_quote_globals():
    server.processed_messages.clear()
    server.pending_subscription_state.clear()
    server.subscription_delivery_blocked_users.clear()
    yield
    server.processed_messages.clear()
    server.pending_subscription_state.clear()
    server.subscription_delivery_blocked_users.clear()


def test_non_vip_exact_quote_start_enters_real_registered_flow(monkeypatch):
    uid = "U_QUOTE_START"
    replies = []
    _install_non_vip_gate(monkeypatch, replies)

    server.handle_message(_text_event("QUOTE-START", "開始包月估價", uid))

    assert len(replies) == 1
    assert "一週想吃幾天" in replies[0].text
    state = server.pending_subscription_state[uid]
    assert state["step"] == "days"
    assert isinstance(state["quote_started_at"], float)
    quick_reply = json.dumps(replies[0].as_json_dict(), ensure_ascii=False)
    assert all(f"包月天數 {days}" in quick_reply for days in (2, 3, 4, 5))


def test_non_vip_owned_self_pickup_sequence_renders_real_estimate_flex(monkeypatch):
    uid = "U_QUOTE_SELF"
    replies = []
    estimate_calls = []
    original_estimate = server.calculate_subscription_estimate
    _install_non_vip_gate(monkeypatch, replies)

    def capture_estimate(*args, **kwargs):
        estimate_calls.append((args, kwargs))
        return original_estimate(*args, **kwargs)

    monkeypatch.setattr(server, "calculate_subscription_estimate", capture_estimate)
    for index, text in enumerate(
        ("開始包月估價", "包月天數 3", "包月取餐 自取", "自取餐數 2")
    ):
        server.handle_message(_text_event(f"QUOTE-SELF-{index}", text, uid))

    assert len(replies) == 4
    assert len(estimate_calls) == 1
    args, kwargs = estimate_calls[0]
    assert args[:3] == (uid, 24, "")
    assert kwargs == {
        "delivery_count": 0,
        "pickup_method": "自取",
        "days_per_week": 3,
        "meals_per_day": 2,
    }
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "包月粗估結果" in rendered
    assert "自取" in rendered
    assert "外送費：$0" in rendered
    assert "本期合計：約 $4,080～$5,280" in rendered
    assert "可以，建立包月資料" in rendered
    assert server.get_subscription_form_link(uid) in rendered
    assert server.pending_subscription_state[uid]["step"] == "estimated"


@pytest.mark.parametrize("active_vip", [False, True], ids=["non-vip", "vip"])
def test_registered_known_delivery_quote_keeps_checkout_total_and_cta(
    active_vip, monkeypatch
):
    uid = f"U_QUOTE_DELIVERY_{active_vip}"
    address = "台北市松山區南京東路四段133巷4弄5號"
    replies = []
    map_calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: active_vip)
    monkeypatch.setattr(
        server, "is_text_command_allowed_without_vip", lambda *_args: False
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_args, **_kwargs: pytest.fail("address reached ordinary AI"),
    )

    def fake_delivery_quote(given_address):
        map_calls.append(given_address)
        return {
            "success": True,
            "delivery_available": True,
            "address": given_address,
            "distance_text": "1.2 公里",
            "distance_meters": 1200,
            "duration_text": "6 分鐘",
            "delivery_fee": 25,
            "delivery_fee_text": "25 元",
            "hub_name": "南京店",
            "route_group": "NANJING",
            "delivery_zone": "1.5KM",
            "carpool_hint": "",
        }

    monkeypatch.setattr(server, "calculate_delivery_quote", fake_delivery_quote)
    for index, text in enumerate(
        ("開始包月估價", "包月天數 3", "包月取餐 外送", address)
    ):
        server.handle_message(_text_event(f"QUOTE-DELIVERY-{index}", text, uid))

    assert map_calls == [address]
    assert len(replies) == 4
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "包月粗估結果" in rendered
    assert address in rendered
    assert "外送費：$300" in rendered
    assert "本期合計：約 $4,280～$5,480" in rendered
    assert "可以，建立包月資料" in rendered
    assert server.get_subscription_form_link(uid) in rendered
    assert server.pending_subscription_state[uid]["step"] == "estimated"


@pytest.mark.parametrize("active_vip", [False, True], ids=["non-vip", "vip"])
def test_registered_delivery_quote_with_unknown_maps_result_is_not_checkout_ready(
    active_vip, monkeypatch
):
    uid = f"U_QUOTE_MAPS_UNKNOWN_{active_vip}"
    address = "台北市測試路1號"
    replies = []
    map_calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: active_vip)
    monkeypatch.setattr(
        server, "is_text_command_allowed_without_vip", lambda *_args: False
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    def unavailable_maps(given_address):
        map_calls.append(given_address)
        return {
            "success": False,
            "delivery_available": None,
            "address": given_address,
            "distance_text": "距離需客服確認",
            "distance_meters": None,
            "duration_text": "",
            "delivery_fee": 0,
            "delivery_fee_text": "需客服確認",
            "hub_name": "南京店",
            "route_group": "MANUAL",
            "delivery_zone": "MANUAL",
            "carpool_hint": "",
        }

    monkeypatch.setattr(server, "calculate_delivery_quote", unavailable_maps)
    for index, text in enumerate(
        ("開始包月估價", "包月天數 3", "包月取餐 外送", address)
    ):
        server.handle_message(_text_event(f"QUOTE-MAPS-UNKNOWN-{active_vip}-{index}", text, uid))

    assert map_calls == [address]
    assert len(replies) == 4
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "餐費小計：$4,080～$5,280" in rendered
    assert "外送費：待客服確認" in rendered
    assert "外送費：$0" not in rendered
    assert "本期合計" not in rendered
    assert "可以，建立包月資料" not in rendered
    assert server.get_subscription_form_link(uid) not in rendered
    assert "重新選天數" in rendered
    assert '"text": "找客服"' not in rendered
    assert server.pending_subscription_state[uid]["step"] == "estimated"


def test_registered_vip_raw_order_with_unknown_maps_result_does_not_create_order(
    monkeypatch,
):
    uid = "U_VIP_RAW_ORDER_MAPS_UNKNOWN"
    address = "台北市測試路1號"
    replies = []
    created = []
    notified = []
    map_calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)

    def unavailable_maps(given_address):
        map_calls.append(given_address)
        return {
            "success": False,
            "delivery_available": None,
            "address": given_address,
            "distance_text": "距離需客服確認",
            "distance_meters": None,
            "duration_text": "",
            "delivery_fee": 0,
            "delivery_fee_text": "需客服確認",
            "hub_name": "",
            "route_group": "MANUAL",
            "delivery_zone": "MANUAL",
            "carpool_hint": "",
        }

    monkeypatch.setattr(server, "calculate_delivery_quote", unavailable_maps)
    monkeypatch.setattr(
        server,
        "create_subscription_order",
        lambda *_args, **_kwargs: created.append(True) or 999,
    )
    monkeypatch.setattr(
        server,
        "notify_admin_new_subscription_order",
        lambda *_args, **_kwargs: notified.append(True),
    )

    server.handle_message(
        _text_event("VIP-RAW-ORDER-MAPS-UNKNOWN", f"訂購 24餐 {address}", uid)
    )

    assert map_calls == [address]
    assert created == []
    assert notified == []
    assert len(replies) == 1
    reply_text = replies[0].text
    assert "外送費需客服確認" in reply_text
    assert "餐費粗估：$4,080～$5,280" in reply_text
    assert "外送費粗估：$0" not in reply_text
    assert "本期粗估合計" not in reply_text
    assert "已收到您的包月訂單" not in reply_text
    assert "建立包月資料" not in reply_text
    assert server.get_subscription_form_link(uid) not in reply_text


def test_registered_vip_raw_estimate_with_unknown_maps_result_is_manual_only(monkeypatch):
    uid = "U_VIP_RAW_ESTIMATE_MAPS_UNKNOWN"
    address = "台北市測試路1號"
    replies = []
    created = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message.text)
    )
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda given_address: {
            "success": False,
            "delivery_available": None,
            "address": given_address,
            "distance_text": "距離需客服確認",
            "delivery_fee": 0,
            "delivery_fee_text": "需客服確認",
        },
    )
    monkeypatch.setattr(
        server,
        "create_subscription_order",
        lambda *_args, **_kwargs: created.append(True) or 999,
    )

    server.handle_message(
        _text_event("VIP-RAW-ESTIMATE-MAPS-UNKNOWN", f"估價 24餐 {address}", uid)
    )

    assert created == []
    assert len(replies) == 1
    assert "外送費需客服確認" in replies[0]
    assert "餐費粗估：$4,080～$5,280" in replies[0]
    assert "$0" not in replies[0]
    assert "本期粗估合計" not in replies[0]
    assert "建立包月資料" not in replies[0]
    assert server.get_subscription_form_link(uid) not in replies[0]


def test_registered_vip_raw_order_with_known_delivery_still_creates_order(monkeypatch):
    uid = "U_VIP_RAW_ORDER_KNOWN"
    address = "台北市測試路1號"
    replies = []
    created = []
    notified = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message.text)
    )
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda given_address: {
            "success": True,
            "delivery_available": True,
            "address": given_address,
            "distance_text": "1.2 公里",
            "duration_text": "6 分鐘",
            "delivery_fee": 25,
            "delivery_fee_text": "25 元",
        },
    )
    monkeypatch.setattr(
        server,
        "create_subscription_order",
        lambda given_uid, estimate: created.append((given_uid, estimate)) or 999,
    )
    monkeypatch.setattr(
        server,
        "notify_admin_new_subscription_order",
        lambda order_id, given_uid, estimate: notified.append((order_id, given_uid, estimate)),
    )

    server.handle_message(_text_event("VIP-RAW-ORDER-KNOWN", f"訂購 24餐 {address}", uid))

    assert len(created) == 1
    assert created[0][0] == uid
    assert created[0][1]["delivery_available"] is True
    assert len(notified) == 1
    assert notified[0][:2] == (999, uid)
    assert len(replies) == 1
    assert "已收到您的包月訂單 #999" in replies[0]
    assert "外送費粗估：$600" in replies[0]
    assert "本期粗估合計：$4,680～$5,880" in replies[0]


@pytest.mark.parametrize("delivery_available", [None, False], ids=["unknown", "out-of-range"])
def test_create_subscription_order_rejects_non_ready_delivery_before_database(
    delivery_available, monkeypatch
):
    estimate = {
        "pickup_method": "外送",
        "delivery_available": delivery_available,
    }
    monkeypatch.setattr(
        server.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("non-ready delivery reached database"),
    )

    with pytest.raises(ValueError, match="delivery is not ready"):
        server.create_subscription_order("U_NOT_READY", estimate)


@pytest.mark.parametrize("delivery_available", [None, False], ids=["unknown", "out-of-range"])
def test_create_pending_subscription_form_order_rejects_non_ready_delivery_before_database(
    delivery_available, monkeypatch
):
    snapshot = {
        "pickup_method": "外送",
        "is_delivery": True,
        "delivery_info": {"delivery_available": delivery_available},
    }
    monkeypatch.setattr(
        server.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("non-ready form reached database"),
    )

    with pytest.raises(ValueError, match="delivery is not ready"):
        server.create_pending_subscription_form_order(snapshot)


@pytest.mark.parametrize(
    "raw_text",
    [
        " 開始包月估價", "開始包月估價 ", "開始包月估價\n",
        "開始 包月估價", "開始包月 估價", "開始包月估價中",
        "請開始包月估價", "開始包月估價！", "開始包月估價\u200b",
        "開始\u200b包月估價", "開始包月估價\ufe0f", "\u3000開始包月估價",
        "開始包月估价", "開始包月估價　",
    ],
)
def test_non_vip_quote_start_near_variants_remain_silent(raw_text, monkeypatch):
    replies = []
    inner_calls = []
    _install_non_vip_gate(monkeypatch, replies)
    original_inner = server._handle_message_impl

    def capture_inner(*args, **kwargs):
        inner_calls.append((args, kwargs))
        return original_inner(*args, **kwargs)

    monkeypatch.setattr(server, "_handle_message_impl", capture_inner)
    server.handle_message(_text_event(f"QUOTE-START-NEG-{repr(raw_text)}", raw_text))

    assert replies == []
    assert inner_calls == []
    assert server.pending_subscription_state == {}


@pytest.mark.parametrize(
    ("state", "raw_text"),
    [
        (None, "包月天數 3"),
        ({"step": "pickup", "days_per_week": 3}, "包月天數 3"),
        ({"step": "days"}, "包月天數 ３"),
        ({"step": "days"}, "包月天數 3 "),
        ({"step": "days"}, "包月天數　3"),
        ({"step": "pickup", "days_per_week": "3"}, "包月取餐 自取"),
        ({"step": "pickup", "days_per_week": 1}, "包月取餐 外送"),
        ({"step": "self_pickup_meals", "days_per_week": 3, "pickup_method": "外送"}, "自取餐數 2"),
        ({"step": "delivery_address", "days_per_week": 3, "pickup_method": "外送"}, "#管理指令"),
        ({"step": "delivery_address", "days_per_week": 3, "pickup_method": "外送"}, "@靜音 王小明"),
        ({"step": "delivery_address", "days_per_week": 3, "pickup_method": "外送"}, "   "),
    ],
)
def test_non_vip_continuation_rejects_missing_wrong_or_malformed_state(
    state, raw_text, monkeypatch
):
    uid = "U_QUOTE_BAD_STATE"
    replies = []
    _install_non_vip_gate(monkeypatch, replies)
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: pytest.fail("rejected address reached Maps quote"),
    )
    if state is not None:
        server.pending_subscription_state[uid] = {
            **state,
            "quote_started_at": server._subscription_quote_now(),
        }

    before = dict(server.pending_subscription_state)
    server.handle_message(_text_event(f"QUOTE-BAD-{repr(raw_text)}", raw_text, uid))

    assert replies == []
    assert server.pending_subscription_state == before


@pytest.mark.parametrize(
    "raw_text",
    [
        "#待核訂單",          # ADMIN exact namespace
        "@靜音 王小明",      # ADMIN payload prefix namespace
        "#教練",              # COACH exact namespace
        "健康回報｜體重70",   # Jason-only payload namespace
        "＃更新菜單",          # NFKC introducer variant
        "#更\u200b新菜單",    # fuzzy/invisible variant
    ],
)
def test_non_vip_delivery_address_rejects_reserved_classifier_matrix_before_maps(
    raw_text, monkeypatch
):
    uid = "U_QUOTE_RESERVED_ADDRESS"
    replies = []
    _install_non_vip_gate(monkeypatch, replies)
    assert server.is_privileged_command_intent(raw_text) is True
    server.pending_subscription_state[uid] = {
        "step": "delivery_address",
        "days_per_week": 3,
        "pickup_method": "外送",
        "quote_started_at": server._subscription_quote_now(),
    }
    before = dict(server.pending_subscription_state)
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: pytest.fail("reserved command reached Maps quote"),
    )

    server.handle_message(_text_event(f"QUOTE-RESERVED-{repr(raw_text)}", raw_text, uid))

    assert replies == []
    assert server.pending_subscription_state == before


def test_non_vip_continuation_is_uid_owned(monkeypatch):
    replies = []
    _install_non_vip_gate(monkeypatch, replies)
    server.handle_message(_text_event("QUOTE-OWNER-START", "開始包月估價", "U_OWNER"))
    owner_state = dict(server.pending_subscription_state["U_OWNER"])

    server.handle_message(_text_event("QUOTE-FOREIGN", "包月天數 3", "U_FOREIGN"))

    assert len(replies) == 1
    assert server.pending_subscription_state["U_OWNER"] == owner_state
    assert "U_FOREIGN" not in server.pending_subscription_state


def test_non_vip_expired_quote_state_cannot_continue(monkeypatch):
    uid = "U_QUOTE_EXPIRED"
    replies = []
    now = 10_000.0
    _install_non_vip_gate(monkeypatch, replies)
    monkeypatch.setattr(server, "_subscription_quote_now", lambda: now)
    server.pending_subscription_state[uid] = {
        "step": "days",
        "quote_started_at": now - server.SUBSCRIPTION_QUOTE_STATE_TTL_SECONDS - 0.001,
    }

    server.handle_message(_text_event("QUOTE-EXPIRED", "包月天數 3", uid))

    assert replies == []
    assert server.pending_subscription_state[uid]["step"] == "days"


def test_non_vip_estimate_consumes_free_text_address_exception(monkeypatch):
    uid = "U_QUOTE_ONCE"
    replies = []
    _install_non_vip_gate(monkeypatch, replies)
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda address: {
            "success": False, "delivery_available": None, "address": address,
            "distance_text": "距離需客服確認", "distance_meters": None,
            "duration_text": "", "delivery_fee": 0,
            "delivery_fee_text": "需客服確認", "hub_name": "南京店",
            "route_group": "MANUAL", "delivery_zone": "MANUAL", "carpool_hint": "",
        },
    )
    for index, text in enumerate(
        ("開始包月估價", "包月天數 2", "包月取餐 外送", "台北市測試路1號")
    ):
        server.handle_message(_text_event(f"QUOTE-ONCE-{index}", text, uid))
    reply_count = len(replies)

    server.handle_message(_text_event("QUOTE-ONCE-AFTER", "今天吃雞胸肉", uid))

    assert len(replies) == reply_count
    assert server.pending_subscription_state[uid]["step"] == "estimated"


def test_quote_state_does_not_allow_non_vip_image_or_postback(monkeypatch):
    uid = "U_QUOTE-SURFACES"
    calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    server.pending_subscription_state[uid] = {
        "step": "delivery_address", "days_per_week": 3, "pickup_method": "外送",
        "quote_started_at": server._subscription_quote_now(),
    }
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: calls.append("cleanup"))
    monkeypatch.setattr(
        server.line_bot_api, "get_message_content", lambda _id: calls.append("download")
    )
    monkeypatch.setattr(server, "handle_meal_photo_postback", lambda _event: calls.append("postback"))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_args: calls.append("reply"))

    server.handle_image_message(
        SimpleNamespace(
            message=SimpleNamespace(id="QUOTE-IMAGE"),
            source=SimpleNamespace(user_id=uid),
            reply_token="reply-image",
        )
    )
    server.handle_postback_event(
        SimpleNamespace(
            postback=SimpleNamespace(data="subscription:anything"),
            source=SimpleNamespace(user_id=uid),
            reply_token="reply-postback",
        )
    )

    assert calls == []
    assert "QUOTE-IMAGE" not in server.processed_messages
