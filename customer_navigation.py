"""Customer-facing navigation and today-first LINE Flex views."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


_ENTRIES = (
    ("今日總覽", "首頁", "#6B8F71"),
    ("搜尋餐點", "搜尋", "#EAA75B"),
    ("記一餐", "我要紀錄飲食", "#EAA75B"),
    ("常吃・食品", "重選常吃", "#6B8F71"),
    ("我的排餐", "查看菜單", "#EAA75B"),
)


def _entry_button(label: str, text: str, color: str) -> dict:
    return {
        "type": "button",
        "style": "primary",
        "color": color,
        "height": "sm",
        "flex": 1,
        "action": {"type": "message", "label": label, "text": text},
    }


def build_customer_function_menu_contents(health_services_available: bool = False) -> dict:
    """Return only currently usable customer entries; do not add filler tiles."""
    entries = list(_ENTRIES)
    if health_services_available:
        entries.append(("健康服務", "健康服務", "#6B8F71"))
    rows = []
    for index in range(0, len(entries), 2):
        pair = entries[index:index + 2]
        row_contents = [_entry_button(*entry) for entry in pair]
        rows.append({
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "contents": row_contents,
        })

    return {
        "type": "bubble",
        "size": "giga",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "功能選單", "weight": "bold", "size": "xl", "color": "#243B2D"},
                {"type": "text", "text": "常用功能・快速前往", "size": "sm", "color": "#66736B", "wrap": True},
                {"type": "separator", "margin": "sm", "color": "#E8E8E2"},
                *rows,
            ],
        },
        "styles": {"body": {"backgroundColor": "#FFFDF8"}},
    }


def build_customer_health_services_contents(services: dict) -> dict | None:
    """Only render services supplied by the entitlement-checking caller."""
    actions = []
    if services.get("training_available"):
        actions.append({"type": "button", "style": "primary", "color": "#6B8F71", "height": "sm", "flex": 1,
                        "action": {"type": "message", "label": "運動專區", "text": "運動"}})
    health_url = str(services.get("health_check_url") or "").strip()
    if health_url.startswith("https://liff.line.me/"):
        actions.append({"type": "button", "style": "secondary", "height": "sm", "flex": 1,
                        "action": {"type": "uri", "label": "三日飲食健檢", "uri": health_url}})

    coaching_status_copy = {
        "payment_pending": "等待付款",
        "payment_reported": "已回報付款，等待人工確認",
        "coaching_active": "陪跑進行中",
        "coaching_paused": "陪跑已暫停",
        "coaching_completed": "陪跑已完成",
        "coaching_refunded": "陪跑已退款",
        "coaching_cancelled": "陪跑已取消",
        "payment_rejected": "付款未通過",
    }
    coaching_order = services.get("coaching_order")
    coaching_copy = None
    if isinstance(coaching_order, dict):
        coaching_copy = coaching_status_copy.get(coaching_order.get("status"))
    if not actions and coaching_copy is None:
        return None

    body_contents = [
        {"type": "text", "text": "以下入口依你的有效資格顯示。", "size": "sm", "color": "#66736B", "wrap": True}
    ]
    if coaching_copy is not None:
        body_contents.append({
            "type": "box", "layout": "vertical", "margin": "md",
            "backgroundColor": "#F7FAF7", "cornerRadius": "10px", "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "4 週營養師陪跑", "size": "sm", "weight": "bold", "color": "#243B2D"},
                {"type": "text", "text": coaching_copy, "size": "sm", "color": "#66736B", "wrap": True, "margin": "xs"},
            ],
        })
    contents = {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical", "paddingAll": "18px", "backgroundColor": "#FFFDF8",
                   "contents": [{"type": "text", "text": "健康服務", "size": "xl", "weight": "bold", "color": "#243B2D"}]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "18px",
                 "contents": body_contents},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
    if actions:
        contents["footer"] = {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "paddingAll": "16px", "contents": actions,
        }
    return contents


def _display_number(value) -> str:
    if value is None:
        return "NA"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value or "0")
    if not number.is_finite():
        return str(value)
    rounded = number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.1f}".rstrip("0").rstrip(".")


_EXPLICIT_PRICE_SUFFIXES = (
    re.compile(r"\s*(?:[（(]\s*)?(?:(?:NTD?|新台幣)\s*)?\$\s*[\d,]+(?:\.\d+)?\s*(?:元)?\s*[）)]?\s*$", re.IGNORECASE),
    re.compile(r"\s*(?:[（(]\s*)?[\d,]+(?:\.\d+)?\s*元\s*[）)]?\s*$"),
)


def _meal_name_without_explicit_price(value) -> str:
    """Strip only an explicit trailing currency amount, never an unlabeled number."""
    text = str(value or "").strip()
    for pattern in _EXPLICIT_PRICE_SUFFIXES:
        cleaned = pattern.sub("", text).rstrip(" ｜|·,/，")
        if cleaned != text:
            return cleaned or text
    return text


def build_customer_home_contents(data: dict) -> dict:
    """Render the customer's today view; keep feature navigation elsewhere."""
    calories = data.get("extra_cal")
    calorie_goal = data.get("tdee")
    protein = data.get("extra_pro")
    protein_goal = data.get("protein_goal")
    fat = data.get("extra_fat")

    def remaining(goal, consumed):
        if goal is None or consumed is None or Decimal(str(goal)) <= 0:
            return None
        # Display-only arithmetic: the visible operands and remainder share
        # the same precision. Canonical nutrition values remain untouched.
        precision = Decimal("0.1")
        shown_goal = Decimal(str(goal)).quantize(precision, rounding=ROUND_HALF_UP)
        shown_consumed = Decimal(str(consumed)).quantize(precision, rounding=ROUND_HALF_UP)
        return shown_goal - shown_consumed

    calorie_left = remaining(calorie_goal, calories)
    protein_left = remaining(protein_goal, protein)

    def metric(label, consumed, goal, remaining, unit, color):
        has_goal = goal is not None and Decimal(str(goal)) > 0
        if consumed is None:
            value_text = f"未知 / {_display_number(goal)} {unit}" if has_goal else "未知 / 未設目標"
            status_text = "剩餘無法計算" if has_goal else "剩餘無法計算（尚未設定目標）"
            progress = None
        elif not has_goal:
            value_text = f"{_display_number(consumed)} {unit} / 未設目標"
            status_text = "剩餘無法計算（尚未設定目標）"
            progress = None
        else:
            value_text = f"{_display_number(consumed)} / {_display_number(goal)} {unit}"
            progress = max(Decimal("0"), min(Decimal("100"), Decimal(str(consumed)) * 100 / Decimal(str(goal))))
            progress = int(progress.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if remaining < 0:
                status_text = f"超出 {_display_number(-remaining)} {unit}"
                color = "#EAA75B"
            else:
                status_text = f"剩 {_display_number(remaining)} {unit}"

        track_contents = [] if not progress else [{
            "type": "box", "layout": "vertical", "width": f"{progress}%",
            "height": "8px", "backgroundColor": color, "cornerRadius": "4px", "contents": [],
        }]
        return {
            "type": "box", "layout": "vertical", "margin": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": label, "size": "sm", "weight": "bold", "color": "#243B2D", "flex": 2},
                    {"type": "text", "text": value_text, "size": "sm", "weight": "bold", "color": "#243B2D", "align": "end", "flex": 5, "wrap": True},
                ]},
                {"type": "box", "layout": "horizontal", "height": "8px", "margin": "sm", "backgroundColor": "#E6E9E7", "cornerRadius": "4px", "contents": track_contents},
                {"type": "text", "text": status_text, "size": "xxs", "color": "#EAA75B" if status_text.startswith("超出") else "#66736B", "align": "end", "margin": "xs", "wrap": True},
            ],
        }

    def intake_only_metric(label, consumed, unit):
        value_text = "未知 / 未設目標" if consumed is None else f"{_display_number(consumed)} {unit} / 未設目標"
        status_text = "無法計算" if consumed is None else "僅顯示今日攝取"
        return {
            "type": "box", "layout": "vertical", "margin": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": label, "size": "sm", "weight": "bold", "color": "#243B2D", "flex": 2},
                    {"type": "text", "text": value_text, "size": "sm", "weight": "bold", "color": "#243B2D", "align": "end", "flex": 5, "wrap": True},
                ]},
                {"type": "box", "layout": "horizontal", "height": "8px", "margin": "sm", "backgroundColor": "#E6E9E7", "cornerRadius": "4px", "contents": []},
                {"type": "text", "text": status_text, "size": "xxs", "color": "#66736B", "align": "end", "margin": "xs"},
            ],
        }

    food_list = [str(item or "").strip() for item in data.get("food_list", []) if str(item or "").strip()]
    if food_list:
        shown_foods = food_list[:3]
        log_summary = "、".join(shown_foods)
        if len(food_list) > len(shown_foods):
            log_summary += f"，另有 {len(food_list) - len(shown_foods)} 筆"
    else:
        log_summary = "今天尚無已確認的飲食紀錄"

    planned_meals = []
    for slot, key, checked_key, calorie_key, protein_key, emoji in (
        ("午餐", "today_lunch", "lunch_checked", "lunch_cal", "lunch_pro", "☀️"),
        ("晚餐", "today_dinner", "dinner_checked", "dinner_cal", "dinner_pro", "🌙"),
    ):
        raw_meal = str(data.get(key) or "").strip()
        meal = _meal_name_without_explicit_price(raw_meal) or "尚未安排"
        has_planned_meal = raw_meal not in {"", "無", "尚未安排"}
        checked = bool(data.get(checked_key))
        planned_meals.append({
            "type": "box", "layout": "horizontal", "spacing": "sm", "margin": "sm",
            "contents": [
                {"type": "text", "text": f"{emoji} {slot}", "size": "sm", "color": "#243B2D", "weight": "bold", "flex": 2},
                {"type": "text", "text": meal, "size": "sm", "color": "#33443A", "wrap": True, "flex": 5},
                {"type": "text", "text": "已吃" if checked else "未吃", "size": "xxs", "color": "#6B8F71" if checked else "#8A8F8B", "align": "end", "flex": 2},
            ],
        })
        if has_planned_meal:
            calories_text = "未知" if data.get(calorie_key) is None else f"{_display_number(data.get(calorie_key))} kcal"
            protein_text = "未知" if data.get(protein_key) is None else f"{_display_number(data.get(protein_key))} g"
            planned_meals.append({
                "type": "text", "text": f"熱量 {calories_text}｜蛋白質 {protein_text}",
                "size": "xxs", "color": "#66736B", "wrap": True, "margin": "xs",
            })
        if has_planned_meal and not checked:
            planned_meals.append({
                "type": "button", "style": "secondary", "height": "sm", "margin": "sm",
                "color": "#6B8F71",
                "action": {"type": "message", "label": f"{slot}已吃", "text": f"{slot}已吃"},
            })

    contents = [
        {"type": "text", "text": "今日總覽", "size": "xl", "weight": "bold", "color": "#243B2D"},
        {"type": "text", "text": str(data.get("today_label") or "今天"), "size": "sm", "color": "#66736B", "margin": "xs"},
        {"type": "box", "layout": "vertical", "margin": "sm", "contents": [
            metric("🔥 熱量", calories, calorie_goal, calorie_left, "kcal", "#6B8F71"),
            metric("🥩 蛋白質", protein, protein_goal, protein_left, "g", "#E3B341"),
            intake_only_metric("🥑 脂肪", fat, "g"),
        ]},
        {"type": "separator", "margin": "md", "color": "#E8E8E2"},
        {"type": "text", "text": "今日飲食紀錄", "size": "sm", "weight": "bold", "color": "#243B2D", "margin": "lg"},
        {"type": "text", "text": log_summary, "size": "sm", "color": "#33443A", "wrap": True, "margin": "sm"},
        {"type": "text", "text": "今日安排", "size": "sm", "weight": "bold", "color": "#243B2D", "margin": "lg"},
        *planned_meals,
    ]
    if data.get("ai_estimated_count", 0):
        contents.append({"type": "text", "text": "含 AI 估算紀錄，非營養師審核結果", "size": "xxs", "color": "#8A6D3B", "margin": "md", "wrap": True})

    return {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "backgroundColor": "#FFFDF8",
            "contents": [
                {"type": "text", "text": "一日樂食・今天", "size": "sm", "weight": "bold", "color": "#6B8F71"},
                {"type": "text", "text": f"{str(data.get('name') or '會員')}，今天吃得如何？", "size": "lg", "weight": "bold", "color": "#243B2D", "margin": "sm", "wrap": True},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "paddingAll": "18px", "contents": contents},
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px",
            "contents": [
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                    {"type": "button", "action": {"type": "message", "label": "記一餐", "text": "我要紀錄飲食"}, "style": "primary", "color": "#6B8F71", "flex": 1, "height": "sm"},
                    {"type": "button", "action": {"type": "message", "label": "今日明細", "text": "我要修改飲食紀錄"}, "style": "secondary", "flex": 1, "height": "sm"},
                ]},
                {"type": "button", "action": {"type": "message", "label": "功能選單", "text": "功能選單"}, "style": "secondary", "color": "#EAA75B", "height": "sm"},
            ],
        },
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
