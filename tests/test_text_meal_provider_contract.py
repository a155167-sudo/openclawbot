import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

import server
from meal_photo_system import build_meal_photo_estimate_bubble, get_meal_photo_draft
from tests.test_photo_batch_single_quota import _install_usage
from tests.test_photo_ingredient_controls import _action, _postback, _text
from tests.test_photo_natural_ingredient_batch import _setup


_NATIVE_HAS_ACTIVE_VIP_ACCESS = server.has_active_vip_access


class FakeCompletions:
    def __init__(self, *, content="", finish_reason="stop", refusal=None):
        self.calls = []
        self.content = content
        self.finish_reason = finish_reason
        self.refusal = refusal

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content, refusal=self.refusal)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=self.finish_reason)]
        )


class SequenceCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[len(self.calls) - 1]
        message = SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False), refusal=None
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )


def _install_fake_client(monkeypatch, *, payload=None, raw=None, finish_reason="stop", refusal=None):
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=True)
    completions = FakeCompletions(
        content=raw, finish_reason=finish_reason, refusal=refusal
    )
    monkeypatch.setattr(
        server,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return completions


def _canonical_payload(name="木耳", amount=20, unit="g"):
    return {
        "food_name": name,
        "portion_assumption": f"{amount} {unit}",
        "basis_amount": amount,
        "basis_unit": unit,
        "calories_kcal": {"estimate": 5, "min": 3, "max": 8, "unit": "kcal"},
        "protein_g": {"estimate": 0.3, "min": 0.1, "max": 0.6, "unit": "g"},
        "fat_g": {"estimate": 0.2, "min": 0.1, "max": 0.3, "unit": "g"},
        "carbohydrate_g": {"estimate": 0.7, "min": 0.4, "max": 1.0, "unit": "g"},
    }


def _install_sequence_client(monkeypatch, payloads):
    completions = SequenceCompletions(payloads)
    monkeypatch.setattr(
        server,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return completions


def _install_native_vip_usage(db, *, quota):
    """Seed the real admission/quota contract instead of bypassing authorization."""
    _install_usage(db, quota=quota)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE usage SET status='vip', expiry_date='2099-12-31' WHERE user_id='U1'"
        )
        conn.commit()


def test_provider_adapter_requests_strict_range_schema_and_matching_prompt(monkeypatch):
    fake = _install_fake_client(monkeypatch, payload=_canonical_payload())

    estimate = server.estimate_text_meal_nutrition(
        {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
    )

    assert estimate["calories_kcal"] == {"estimate": 5.0, "min": 3.0, "max": 8.0}
    assert estimate["protein_g"] == {"estimate": 0.3, "min": 0.1, "max": 0.6}
    call = fake.calls[0]
    response_format = call["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert set(schema["required"]) == {
        "food_name", "portion_assumption", "basis_amount", "basis_unit",
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g",
    }
    for field, unit in (("calories_kcal", "kcal"), ("protein_g", "g"), ("fat_g", "g"), ("carbohydrate_g", "g")):
        nutrient = schema["properties"][field]
        assert set(nutrient["required"]) == {"estimate", "min", "max", "unit"}
        assert nutrient["properties"]["unit"]["enum"] == [unit]
    prompt = call["messages"][0]["content"]
    assert all(token in prompt for token in ("estimate", "min", "max", "basis_amount", "四項營養"))


def test_provider_adapter_normalizes_explicit_equivalent_wrapper_and_range_shape(monkeypatch):
    raw = {
        "result": {
            "food_name": "木耳",
            "portion_assumption": "20 g",
            "basis_amount": 20,
            "basis_unit": "g",
            "nutrition": {
                "calories_kcal": 5,
                "calories_kcal_range": {"min": 3, "max": 8, "unit": "kcal"},
                "protein_g": {"estimate": 0.3, "range": {"min": 0.1, "max": 0.6}, "unit": "g"},
                "fat_g": {"estimate": 0.2, "min": 0.1, "max": 0.3, "unit": "g"},
                "carbohydrate_g": {"estimate": 0.7, "min": 0.4, "max": 1.0, "unit": "g"},
                "confidence": 0.8,
            },
            "metadata": {"trace": "ignored"},
        }
    }
    _install_fake_client(monkeypatch, payload=raw)

    estimate = server.estimate_text_meal_nutrition(
        {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
    )

    assert estimate["food_name"] == "木耳"
    assert estimate["calories_kcal"] == {"estimate": 5.0, "min": 3.0, "max": 8.0}
    assert estimate["protein_g"] == {"estimate": 0.3, "min": 0.1, "max": 0.6}
    assert estimate["provenance"]["provider"] == "openai"
    assert estimate["provenance"]["model"] == "gpt-4o-mini"
    assert estimate["provenance"]["method"] == "text_meal_estimate"
    assert estimate["provenance"]["trace_id"]


def test_raw_http_adapter_rejects_conflicting_top_level_and_nested_nutrition(monkeypatch):
    requests = []
    conflicting = _canonical_payload() | {
        "calories_kcal": {"estimate": 100, "min": 90, "max": 110, "unit": "kcal"},
        "protein_g": {"estimate": 10, "min": 9, "max": 11, "unit": "g"},
        "nutrition": {
            "calories_kcal": {"estimate": 5, "min": 3, "max": 8, "unit": "kcal"},
            "protein_g": {"estimate": 0.3, "min": 0.1, "max": 0.6, "unit": "g"},
        },
    }

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, request=request, json={
            "id": "chatcmpl_conflict",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(conflicting, ensure_ascii=False),
                    "refusal": None,
                },
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(server, "client", OpenAI(
        api_key="test-only",
        base_url="https://provider.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ))

    with pytest.raises(server.TextMealProviderError, match="矛盾"):
        server.estimate_text_meal_nutrition(
            {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
        )
    assert len(requests) == 1
    assert requests[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    "provider_payload",
    [
        {
            "food_name": "木耳",
            "portion_assumption": "20 g",
            "calories_kcal": {"estimate": 5},
            "protein_g": {
                "estimate": 0.3, "min": 0.1, "max": 0.6, "unit": "g",
            },
            "nutrition": {
                "calories_kcal_range": {"min": 3, "max": 8, "unit": "kcal"},
            },
        },
        _canonical_payload() | {
            "nutrition": {
                "calories_kcal": {"range": {"low": 999, "high": 1000}},
            },
        },
    ],
    ids=["cross-scope-stitching", "malformed-shadow"],
)
def test_raw_http_adapter_rejects_incomplete_or_malformed_recognized_representation(
    monkeypatch, provider_payload
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, request=request, json={
            "id": "chatcmpl_invalid_representation",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(provider_payload, ensure_ascii=False),
                    "refusal": None,
                },
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(server, "client", OpenAI(
        api_key="test-only",
        base_url="https://provider.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ))

    with pytest.raises(server.TextMealProviderError, match="區間|格式"):
        server.estimate_text_meal_nutrition(
            {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
        )
    assert len(requests) == 1
    assert requests[0]["response_format"]["json_schema"]["strict"] is True


def test_provider_adapter_accepts_equivalent_duplicate_representations(monkeypatch):
    raw = _canonical_payload() | {
        "nutrition": {
            "calories_kcal": {
                "estimate": 5.0,
                "range": {"min": 3.0, "max": 8.0},
                "unit": "KCAL",
            },
            "protein_g": 0.3,
            "protein_g_range": {"min": 0.1, "max": 0.6, "unit": "grams"},
            "metadata": {"trace": "ignored"},
        },
        "metadata": {"other": "ignored"},
    }
    _install_fake_client(monkeypatch, payload={"result": raw, "metadata": {"trace": "outer"}})

    estimate = server.estimate_text_meal_nutrition(
        {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
    )

    assert estimate["calories_kcal"] == {"estimate": 5.0, "min": 3.0, "max": 8.0}
    assert estimate["protein_g"] == {"estimate": 0.3, "min": 0.1, "max": 0.6}


def test_raw_http_adapter_accepts_two_complete_equivalent_representations(monkeypatch):
    requests = []
    equivalent = _canonical_payload() | {
        "nutrition": {
            "calories_kcal": {
                "unit": "KCAL", "max": 8.0, "estimate": 5.0, "min": 3.0,
            },
            "protein_g": {
                "range": {"max": 0.6, "min": 0.1},
                "unit": "grams",
                "estimate": 0.3,
            },
        },
    }

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, request=request, json={
            "id": "chatcmpl_equivalent",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(equivalent, ensure_ascii=False),
                    "refusal": None,
                },
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(server, "client", OpenAI(
        api_key="test-only",
        base_url="https://provider.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ))

    estimate = server.estimate_text_meal_nutrition(
        {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
    )

    assert estimate["calories_kcal"] == {"estimate": 5.0, "min": 3.0, "max": 8.0}
    assert estimate["protein_g"] == {"estimate": 0.3, "min": 0.1, "max": 0.6}
    assert len(requests) == 1
    assert requests[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    "nested",
    [
        {"calories_kcal": {"estimate": 6}},
        {"calories_kcal": {"estimate": 5, "min": 2, "max": 8, "unit": "kcal"}},
        {"calories_kcal": {"estimate": 5, "min": 3, "max": 9, "unit": "kcal"}},
        {"calories_kcal": {"estimate": 5, "min": 3, "max": 8, "unit": "g"}},
    ],
    ids=["partial-overlap", "different-min", "different-max", "wrong-dimension"],
)
def test_provider_adapter_rejects_any_conflicting_duplicate_component(monkeypatch, nested):
    _install_fake_client(
        monkeypatch,
        payload=_canonical_payload() | {"nutrition": nested},
    )

    with pytest.raises(server.TextMealProviderError, match="矛盾|單位|區間"):
        server.estimate_text_meal_nutrition(
            {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
        )


@pytest.mark.parametrize(
    "payload, raw, finish_reason, refusal, message",
    [
        (_canonical_payload() | {"calories_kcal": 5}, None, "stop", None, "區間"),
        (_canonical_payload() | {"protein_g": {"estimate": -1, "min": -2, "max": 0, "unit": "g"}}, None, "stop", None, "非負有限"),
        (_canonical_payload() | {"protein_g": {"estimate": float("nan"), "min": 0, "max": 1, "unit": "g"}}, None, "stop", None, "JSON數值"),
        (None, "not json", "stop", None, "JSON"),
        (_canonical_payload(), None, "length", None, "不完整"),
        (_canonical_payload(), None, "stop", "cannot comply", "拒絕"),
        (_canonical_payload() | {"calories_kcal": {"estimate": 5, "min": 3, "max": 8, "unit": "kJ"}}, None, "stop", None, "單位"),
    ],
)
def test_provider_adapter_fails_closed_for_invalid_or_incomplete_output(
    monkeypatch, payload, raw, finish_reason, refusal, message
):
    _install_fake_client(
        monkeypatch, payload=payload, raw=raw, finish_reason=finish_reason, refusal=refusal
    )
    with pytest.raises(ValueError, match=message):
        server.estimate_text_meal_nutrition(
            {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}
        )


def test_missing_ranges_keeps_batch_atomic_refunds_real_quota_and_blames_system(
    tmp_path, monkeypatch
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_native_vip_usage(db, quota=1)
    assert _NATIVE_HAS_ACTIVE_VIP_ACCESS("U1") is True
    monkeypatch.setattr(server, "has_active_vip_access", _NATIVE_HAS_ACTIVE_VIP_ACCESS)
    monkeypatch.setattr(
        server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA
    )
    fake = _install_sequence_client(monkeypatch, [
        _canonical_payload("木耳", 1, "cup"),
        {
            "food_name": "青菜", "portion_assumption": "1 cup",
            "calories_kcal": 5, "protein_g": 0.3,
        },
    ])
    card = build_meal_photo_estimate_bubble(draft)
    server.handle_postback_event(
        _postback(_action(card, "➕ 新增食材")["data"], "REQUEST-PROVIDER-CONTRACT")
    )

    server.handle_message(_text("木耳1杯、青菜1杯", "MISSING-RANGES"))

    assert "系統估算未完成" in replies[-1].text
    assert "原草稿保留" in replies[-1].text
    assert "本次額度已退回" in replies[-1].text
    assert "請修正" not in replies[-1].text
    assert len(fake.calls) == 2
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "awaiting_item_name"
        assert current["version"] == draft["version"] + 1
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status"
        ).fetchall() == [("refunded", 1)]


def test_four_item_batch_uses_real_adapter_four_times_but_one_net_quota(
    tmp_path, monkeypatch
):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_native_vip_usage(db, quota=1)
    assert _NATIVE_HAS_ACTIVE_VIP_ACCESS("U1") is True
    monkeypatch.setattr(server, "has_active_vip_access", _NATIVE_HAS_ACTIVE_VIP_ACCESS)
    monkeypatch.setattr(
        server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA
    )
    names = ["木耳", "牛番茄", "金針菇", "青菜"]
    fake = _install_sequence_client(
        monkeypatch, [_canonical_payload(name, 1, "cup") for name in names]
    )
    card = build_meal_photo_estimate_bubble(draft)
    server.handle_postback_event(
        _postback(_action(card, "➕ 新增食材")["data"], "REQUEST-ADAPTER-BATCH")
    )

    server.handle_message(
        _text("木耳1杯、牛番茄1杯、金針菇1杯、青菜1杯", "ADAPTER-BATCH-FOUR")
    )

    assert len(fake.calls) == 4
    for call in fake.calls:
        assert call["response_format"]["type"] == "json_schema"
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "estimated"
        added = {
            item["name"]: item
            for item in current["estimate"]["estimate_items"]
            if item["name"] in names
        }
        assert set(added) == set(names)
        assert all(
            item["calories_kcal_range"]["max"]
            > item["calories_kcal_range"]["min"]
            for item in added.values()
        )
        assert all(
            item["protein_g_range"]["max"] > item["protein_g_range"]["min"]
            for item in added.values()
        )
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status"
        ).fetchall() == [("charged", 1)]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
