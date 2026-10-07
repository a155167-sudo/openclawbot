import json

from linebot.models import BubbleContainer, FlexSendMessage

from meal_photo_system import build_meal_photo_estimate_bubble


TOKEN = "abc123def456"
ITEMS = [
    {
        "item_id": "itm_chicken",
        "name": "雞肉",
        "portion": "約1掌",
        "calories_kcal": 220,
        "protein_g": 30,
        "nutrition_source": "ai_vision_estimate",
    },
    {
        "item_id": "itm_egg",
        "name": "水煮蛋",
        "portion": "1顆",
        "calories_kcal": 77,
        "protein_g": 6,
        "nutrition_source": "ai_vision_estimate",
    },
    {
        "item_id": "itm_sweet_potato",
        "name": "地瓜",
        "portion": "約半顆",
        "calories_kcal": 100,
        "protein_g": 2,
        "nutrition_source": "ai_vision_estimate",
    },
]


def _draft():
    return {
        "status": "estimated",
        "token": TOKEN,
        "version": 7,
        "workflow_version": "user_confirmed_ai_nutrition_v2",
        "meal_slot": "午餐",
        "estimate": {
            "rule_version": "ai-vision-nutrition-estimate-v1",
            "estimate_items": ITEMS,
            "calories_kcal": 397,
            "protein_g": 38,
            "calories_kcal_range": {"min": 337, "max": 457},
            "protein_g_range": {"min": 32, "max": 44},
        },
    }


def _ingredient_rows(bubble):
    return [
        component
        for component in bubble["body"]["contents"]
        if component.get("type") == "box"
        and any(
            child.get("action", {}).get("label") == "移除"
            or any(
                grandchild.get("action", {}).get("label") == "移除"
                for grandchild in child.get("contents", [])
            )
            for child in component.get("contents", [])
        )
    ]


def test_estimate_rows_reserve_readable_left_column_and_narrow_remove_control():
    bubble = build_meal_photo_estimate_bubble(_draft())
    rows = _ingredient_rows(bubble)

    assert len(rows) == 3
    first = rows[0]
    left, remove_wrapper = first["contents"]
    assert left == {
        "type": "box",
        "layout": "vertical",
        "flex": 1,
        "contents": [
            {
                "type": "text",
                "text": "雞肉：約1掌（約220 kcal／蛋白質30 g）",
                "wrap": True,
                "size": "sm",
                "color": "#333333",
            },
        ],
    }
    assert remove_wrapper["type"] == "box"
    assert remove_wrapper["layout"] == "vertical"
    assert remove_wrapper["width"] == "64px"
    assert remove_wrapper["flex"] == 0
    assert remove_wrapper["height"] == "44px"
    button = remove_wrapper["contents"][0]
    assert button["height"] == "sm"
    assert button["action"] == {
        "type": "postback",
        "label": "移除",
        "data": "mp:v1:abc123def456:7:remove_item:itm_chicken",
        "displayText": "移除雞肉",
    }


def test_estimate_bubble_is_accepted_by_installed_line_flex_sdk():
    bubble = build_meal_photo_estimate_bubble(_draft())

    container = BubbleContainer.new_from_json_dict(bubble)
    message = FlexSendMessage(alt_text="照片估算", contents=container)
    serialized = message.as_json_dict()

    assert serialized["type"] == "flex"
    assert serialized["contents"]["type"] == "bubble"
    serialized_rows = _ingredient_rows(serialized["contents"])
    serialized_left, serialized_remove = serialized_rows[0]["contents"]
    assert serialized_left["flex"] == 1
    assert serialized_remove["width"] == "64px"
    assert serialized_remove["flex"] == 0
    assert serialized_remove["height"] == "44px"
    assert serialized_remove["contents"][0]["action"]["data"] == (
        "mp:v1:abc123def456:7:remove_item:itm_chicken"
    )
    assert json.dumps(serialized, ensure_ascii=False)
