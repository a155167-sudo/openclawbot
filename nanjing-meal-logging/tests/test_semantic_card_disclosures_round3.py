import copy

from linebot.models import FlexSendMessage

import server


NUMERIC_TEXTS = [
    "熱量：200 kcal（確認後記錄）｜估算範圍 180–220",
    "蛋白質：20.0 g（確認後記錄）｜估算範圍 18.0–22.0",
    "脂肪：10.0 g（確認後記錄）｜估算範圍 9.0–11.0",
    "碳水：30.0 g（確認後記錄）｜估算範圍 27.0–33.0",
]


def _draft(items=None):
    provenance = {
        "provider": "openai",
        "model": "semantic-meal-v1",
        "method": "semantic_meal_estimate",
        "source_label": "官方資料＋AI估算",
    }
    if items is not None:
        provenance["items"] = items
    return {
        "token": "a" * 40,
        "version": 7,
        "meal_slot": "午餐",
        "portion_multiplier": 1,
        "estimate": {
            "food_name": "傳統豆腐、燕麥奶",
            "portion_assumption": "多品項：傳統豆腐 100 g；燕麥奶 300 ml",
            "calories_kcal": {"estimate": 199, "min": 180, "max": 220, "unit": "kcal"},
            "protein_g": {"estimate": 19, "min": 18, "max": 22, "unit": "g"},
            "fat_g": {"estimate": 9, "min": 9, "max": 11, "unit": "g"},
            "carbohydrate_g": {"estimate": 29, "min": 27, "max": 33, "unit": "g"},
            "assessment": {},
            "provenance": provenance,
        },
    }


def _payload_and_replay(draft):
    payload = server.build_text_meal_estimate_flex(draft).as_json_dict()
    replay = FlexSendMessage.new_from_json_dict(payload).as_json_dict()
    assert replay == payload
    return payload, replay


def _texts(node):
    found = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            found.append(node)
        for value in node.values():
            found.extend(_texts(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_texts(value))
    return found


def _text_map(payload):
    return {component["text"]: component for component in _texts(payload)}


def _actions(payload):
    return [
        component["action"]
        for component in payload["contents"]["footer"]["contents"]
    ]


def test_unit_mismatch_warning_survives_sdk_replay_untruncated_and_numbers_actions_stay_fixed():
    warning = (
        "「燕麥奶」的 ml 與官方 g 不相容；本草稿改用AI每100ml估算，"
        "未把g與ml互換。這段刻意加長以驗證完整揭露，不得截字或省略。"
    )
    item = {
        "request": {"food_name": "燕麥奶", "amount": 300, "unit": "ml"},
        "source_label": "AI估算",
        "source": {"provider": "openai", "model": "nutrition-v1", "raw_trace_id": "trace-secret"},
        "unit_warning": warning,
        "nutrition": {},
    }

    payload, replay = _payload_and_replay(_draft([item]))
    for artifact in (payload, replay):
        text_map = _text_map(artifact)
        warning_text = f"⚠️ {warning}"
        assert warning_text in text_map
        assert text_map[warning_text]["wrap"] is True
        assert "AI估算｜燕麥奶" in text_map
        assert all(text in text_map for text in NUMERIC_TEXTS)
        rendered = str(artifact)
        assert "trace-secret" not in rendered
        assert _actions(artifact) == [
            {"type": "postback", "label": "確認記錄", "data": f"tmest:v3:{'a' * 40}:7:confirm"},
            {"type": "uri", "label": "修改", "uri": f"https://liff.line.me/{server.CUSTOMER_RESCHEDULE_LIFF_ID}?view=meal-edit&token={'a' * 40}"},
            {"type": "postback", "label": "取消", "data": f"tmest:v3:{'a' * 40}:7:cancel"},
        ]


def test_official_generic_reference_discloses_tfda_name_scope_assumptions_and_readable_version():
    source = {
        "type": "reference",
        "publisher": "TFDA",
        "source_name": "傳統豆腐(2022年取樣)",
        "source_row_id": "R4700902",
        "dataset_version": "2026-10-05 08:34:48",
        "reference_scope": "latest_dataset_representative_benchmark",
        "reference_label": "TFDA最新2022代表參考值，非實測、非唯一食品真值",
        "preparation_assumption": "依TFDA來源列之前處理描述，以混合均勻打碎樣本的每100克為基準。",
        "variety_assumption": "通用名稱沿用傳統豆腐／板豆腐常見稱呼；個別產品配方可能不同，非實測。",
        "version_assumption": "資料集政策固定採用目前目錄中最新的2022年取樣代表列；亦非使用者食品實測。",
        "archive_sha256": "hash-must-not-render",
    }
    item = {
        "request": {"food_name": "傳統豆腐", "amount": 100, "unit": "g"},
        "source_label": "官方資料",
        "source": source,
        "nutrition": {},
    }

    payload, replay = _payload_and_replay(_draft([item]))
    for artifact in (payload, replay):
        text_map = _text_map(artifact)
        expected = [
            "官方參考（TFDA一般參考）｜傳統豆腐：傳統豆腐(2022年取樣)",
            "參考範圍：TFDA最新2022代表參考值，非實測、非唯一食品真值",
            "處理／可食部：依TFDA來源列之前處理描述，以混合均勻打碎樣本的每100克為基準。",
            "品種假設：通用名稱沿用傳統豆腐／板豆腐常見稱呼；個別產品配方可能不同，非實測。",
            "版本／選樣：資料集政策固定採用目前目錄中最新的2022年取樣代表列；亦非使用者食品實測。（資料版本 2026-10-05）",
        ]
        assert all(text in text_map for text in expected)
        assert all(text_map[text]["wrap"] is True for text in expected)
        rendered = str(artifact)
        assert "hash-must-not-render" not in rendered
        assert "R4700902" not in rendered
        assert all(text in text_map for text in NUMERIC_TEXTS)


def test_mixed_items_keep_each_disclosure_bound_to_its_food_and_legacy_card_remains_compatible():
    items = [
        {
            "request": {"food_name": "生去皮雞胸肉", "amount": 120, "unit": "g"},
            "source_label": "官方資料",
            "source": {
                "publisher": "TFDA", "source_name": "去皮清肉平均值",
                "reference_label": "TFDA一般參考值，非個別品牌實測",
                "preparation_assumption": "生鮮、去皮、可食部每100克。",
            },
            "nutrition": {},
        },
        {
            "request": {"food_name": "燕麥奶", "amount": 300, "unit": "ml"},
            "source_label": "AI估算",
            "source": {"provider": "openai", "model": "nutrition-v1"},
            "unit_warning": "燕麥奶 ml 與官方 g 不相容；未把g與ml互換。",
            "nutrition": {},
        },
    ]
    payload, _ = _payload_and_replay(_draft(items))
    text_map = _text_map(payload)
    assert "官方參考（TFDA一般參考）｜生去皮雞胸肉：去皮清肉平均值" in text_map
    assert "處理／可食部：生鮮、去皮、可食部每100克。" in text_map
    assert "AI估算｜燕麥奶" in text_map
    assert "⚠️ 燕麥奶 ml 與官方 g 不相容；未把g與ml互換。" in text_map

    legacy = _draft()
    legacy["estimate"]["provenance"] = {"source_label": "舊卡AI估算"}
    legacy_payload, legacy_replay = _payload_and_replay(legacy)
    for artifact in (legacy_payload, legacy_replay):
        legacy_texts = _text_map(artifact)
        assert "來源：舊卡AI估算" in legacy_texts
        assert all(text in legacy_texts for text in NUMERIC_TEXTS)
        assert not any(text.startswith(("官方參考（", "AI估算｜", "⚠️ ")) for text in legacy_texts)
