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

    food_entries = [entry for entry in (data.get("food_entries") or []) if str(entry.get("name") or "").strip()]
    food_log_nodes = _food_log_nodes(food_entries) if food_entries else []
    food_list = [str(item or "").strip() for item in data.get("food_list", []) if str(item or "").strip()]
    if food_list:
        shown_foods = food_list[:3]
        log_summary = "、".join(shown_foods)
        if len(food_list) > len(shown_foods):
            log_summary += f"，另有 {len(food_list) - len(shown_foods)} 筆"
    else:
        log_summary = "今天尚無已確認的飲食紀錄"

    planned_meals = []
    any_planned = any(
        str(data.get(key) or "").strip() not in {"", "無", "尚未安排"}
        for key in ("today_lunch", "today_dinner")
    )
    for slot, key, checked_key, calorie_key, protein_key, emoji in (
        ("午餐", "today_lunch", "lunch_checked", "lunch_cal", "lunch_pro", "☀️"),
        ("晚餐", "today_dinner", "dinner_checked", "dinner_cal", "dinner_pro", "🌙"),
    ):
        raw_meal = str(data.get(key) or "").strip()
        meal = _meal_name_without_explicit_price(raw_meal) or "尚未安排"
        has_planned_meal = raw_meal not in {"", "無", "尚未安排"}
        checked = bool(data.get(checked_key))
        if not any_planned:
            continue
        status_node = (
            {"type": "text", "text": "已吃" if checked else "未吃", "size": "xxs", "color": "#6B8F71" if checked else "#8A8F8B", "align": "end", "flex": 2}
            if has_planned_meal else
            {"type": "text", "text": " ", "size": "xxs", "flex": 2}
        )
        planned_meals.append({
            "type": "box", "layout": "horizontal", "spacing": "sm", "margin": "sm",
            "contents": [
                {"type": "text", "text": f"{emoji} {slot}", "size": "sm", "color": "#243B2D", "weight": "bold", "flex": 2},
                {"type": "text", "text": meal if has_planned_meal else "未安排", "size": "sm", "color": "#33443A" if has_planned_meal else "#8A8F8B", "wrap": True, "flex": 5},
                status_node,
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

    if not any_planned:
        planned_meals.append({
            "type": "text", "text": "今天沒有安排包月餐點", "size": "sm", "color": "#8A8F8B", "wrap": True, "margin": "sm",
        })

    next_meal_tip = build_next_meal_tip_nodes(data)

    contents = [
        {"type": "text", "text": "今日總覽", "size": "xl", "weight": "bold", "color": "#243B2D"},
        {"type": "text", "text": str(data.get("today_label") or "今天"), "size": "sm", "color": "#66736B", "margin": "xs"},
        {"type": "box", "layout": "vertical", "margin": "sm", "contents": [
            metric("🔥 熱量", calories, calorie_goal, calorie_left, "kcal", "#6B8F71"),
            metric("🥩 蛋白質", protein, protein_goal, protein_left, "g", "#E3B341"),
            # Fat is shown only when every counted item has a known fat value;
            # AI text estimates carry no fat, so an always-unknown row is noise.
            *([intake_only_metric("🥑 脂肪", fat, "g")] if fat is not None else []),
        ]},
        *next_meal_tip,
        {"type": "separator", "margin": "md", "color": "#E8E8E2"},
        {"type": "text", "text": "今日飲食紀錄", "size": "sm", "weight": "bold", "color": "#243B2D", "margin": "lg"},
        *(food_log_nodes or [{"type": "text", "text": log_summary, "size": "sm", "color": "#33443A", "wrap": True, "margin": "sm"}]),
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
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                    {"type": "button", "action": {"type": "message", "label": "一週趨勢", "text": "一週趨勢"}, "style": "secondary", "flex": 1, "height": "sm"},
                    {"type": "button", "action": {"type": "message", "label": "功能選單", "text": "功能選單"}, "style": "secondary", "color": "#EAA75B", "flex": 1, "height": "sm"},
                ]},
            ],
        },
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }


_SLOT_ORDER = ("早餐", "午餐", "晚餐", "點心")


def _normalize_slot(value) -> str:
    text = str(value or "").strip()
    for slot in _SLOT_ORDER:
        if slot in text:
            return slot
    if text in {"宵夜", "下午茶", "零食"}:
        return "點心"
    return "其他"


def _food_log_nodes(entries) -> list:
    """Group today's confirmed items by meal slot, each with its calories."""
    groups = {}
    for entry in entries:
        groups.setdefault(_normalize_slot(entry.get("meal_slot")), []).append(entry)
    nodes = []
    for slot in (*_SLOT_ORDER, "其他"):
        items = groups.get(slot)
        if not items:
            continue
        known = [item.get("calories_kcal") for item in items if item.get("calories_kcal") is not None]
        subtotal = f"{_display_number(sum(Decimal(str(v)) for v in known))} kcal" if known else ""
        nodes.append({
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {"type": "text", "text": slot, "size": "xs", "weight": "bold", "color": "#6B8F71", "flex": 3},
                {"type": "text", "text": subtotal or " ", "size": "xs", "color": "#6B8F71", "align": "end", "flex": 4},
            ],
        })
        shown = items[:4]
        for item in shown:
            kcal = item.get("calories_kcal")
            nodes.append({
                "type": "box", "layout": "horizontal", "margin": "xs",
                "contents": [
                    {"type": "text", "text": str(item.get("name")).strip(), "size": "sm", "color": "#33443A", "wrap": True, "flex": 5},
                    {"type": "text", "text": "未知" if kcal is None else f"{_display_number(kcal)} kcal", "size": "xs", "color": "#66736B", "align": "end", "flex": 2},
                ],
            })
        if len(items) > len(shown):
            nodes.append({"type": "text", "text": f"另有 {len(items) - len(shown)} 筆", "size": "xxs", "color": "#8A8F8B", "margin": "xs"})
    return nodes


def _next_slots(hour) -> tuple:
    if hour is None:
        return ()
    if hour < 10:
        return ("早餐", "午餐", "晚餐")
    if hour < 14:
        return ("午餐", "晚餐")
    if hour < 20:
        return ("晚餐",)
    return ()


def build_next_meal_tip_nodes(data: dict) -> list:
    """Rule-based next-meal guidance; no AI call, no promise beyond the numbers."""
    try:
        goal_cal = Decimal(str(data.get("tdee"))) if data.get("tdee") is not None else None
        goal_pro = Decimal(str(data.get("protein_goal"))) if data.get("protein_goal") is not None else None
        ate_cal = Decimal(str(data.get("extra_cal"))) if data.get("extra_cal") is not None else None
        ate_pro = Decimal(str(data.get("extra_pro"))) if data.get("extra_pro") is not None else None
    except (InvalidOperation, TypeError, ValueError):
        return []
    if not goal_cal or not goal_pro or goal_cal <= 0 or goal_pro <= 0 or ate_cal is None or ate_pro is None:
        return []
    slots = _next_slots(data.get("now_hour"))
    if not slots:
        return []

    left_cal, left_pro = goal_cal - ate_cal, goal_pro - ate_pro
    planned = {}
    for slot, key, checked_key, cal_key, pro_key in (
        ("午餐", "today_lunch", "lunch_checked", "lunch_cal", "lunch_pro"),
        ("晚餐", "today_dinner", "dinner_checked", "dinner_cal", "dinner_pro"),
    ):
        meal = str(data.get(key) or "").strip()
        if slot in slots and meal not in {"", "無", "尚未安排"} and not data.get(checked_key):
            planned[slot] = (
                _meal_name_without_explicit_price(meal),
                data.get(cal_key), data.get(pro_key),
            )

    next_slot = slots[0]
    lines = []
    if next_slot in planned:
        name, cal, pro = planned[next_slot]
        if cal is not None and pro is not None:
            lines.append(f"{next_slot}是「{name}」，約 {_display_number(cal)} kcal、蛋白質 {_display_number(pro)} g。")
        else:
            lines.append(f"{next_slot}是「{name}」。")
    for _slot, (_name, cal, pro) in planned.items():
        if cal is not None:
            left_cal -= Decimal(str(cal))
        if pro is not None:
            left_pro -= Decimal(str(pro))
    free_slots = [slot for slot in slots if slot not in planned]

    if left_cal <= 0:
        lines.append("今天熱量已接近目標，接下來以蔬菜和瘦肉為主、份量放小就好。")
    elif free_slots:
        per_cal = left_cal / len(free_slots)
        per_pro = max(left_pro, Decimal("0")) / len(free_slots)
        target = "下一餐" if free_slots[0] == next_slot else f"{free_slots[0]}"
        if per_pro >= 1:
            lines.append(f"{target}建議約 {_display_number(per_cal.quantize(Decimal('10')))} kcal、蛋白質 {_display_number(per_pro.quantize(Decimal('1')))} g。")
        else:
            lines.append(f"{target}建議約 {_display_number(per_cal.quantize(Decimal('10')))} kcal，蛋白質已達標。")
    elif left_pro >= 10:
        lines.append(f"吃完排餐後蛋白質還差約 {_display_number(left_pro.quantize(Decimal('1')))} g，可以補一份高蛋白點心，例如無糖豆漿或茶葉蛋。")
    if not lines:
        return []
    return [{
        "type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "12px",
        "backgroundColor": "#F4F8F4", "cornerRadius": "10px",
        "contents": [
            {"type": "text", "text": "💡 下一餐建議", "size": "sm", "weight": "bold", "color": "#2F5D3A"},
            *[{"type": "text", "text": line, "size": "sm", "color": "#33443A", "wrap": True, "margin": "xs"} for line in lines],
        ],
    }]


def build_weekly_trend_contents(days: list, *, calorie_goal=None, protein_goal=None) -> dict:
    """Seven-day calories/protein bars. days: [{label, calories_kcal, protein_g, logged}] oldest first."""
    def bar_column(day, key, scale_max, color):
        value = day.get(key)
        known = value is not None and day.get("logged")
        height = 2
        if known and scale_max > 0:
            height = max(2, int((Decimal(str(value)) * 60 / scale_max).to_integral_value(ROUND_HALF_UP)))
        bar = {"type": "box", "layout": "vertical", "height": f"{height}px",
               "backgroundColor": color if known else "#E6E9E7", "cornerRadius": "3px", "contents": []}
        return {
            "type": "box", "layout": "vertical", "flex": 1, "spacing": "xs",
            "contents": [
                {"type": "box", "layout": "vertical", "height": "62px", "justifyContent": "flex-end", "contents": [bar]},
                {"type": "text", "text": str(day.get("label") or ""), "size": "xxs", "color": "#66736B", "align": "center"},
            ],
        }

    def section(title, key, goal, unit, color):
        logged = [d for d in days if d.get(key) is not None and d.get("logged")]
        scale_values = [Decimal(str(d[key])) for d in logged]
        if goal:
            scale_values.append(Decimal(str(goal)))
        scale_max = max(scale_values) if scale_values else Decimal("0")
        avg = (sum(Decimal(str(d[key])) for d in logged) / len(logged)) if logged else None
        summary = f"有紀錄 {len(logged)} 天・平均 {_display_number(avg.quantize(Decimal('1')))} {unit}" if avg is not None else "這週還沒有紀錄"
        if goal and avg is not None:
            summary += f"（目標 {_display_number(goal)} {unit}）"
        return [
            {"type": "text", "text": title, "size": "sm", "weight": "bold", "color": "#243B2D", "margin": "lg"},
            {"type": "text", "text": summary, "size": "xs", "color": "#66736B", "wrap": True, "margin": "xs"},
            {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "sm",
             "contents": [bar_column(day, key, scale_max, color) for day in days]},
        ]

    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "paddingAll": "18px", "backgroundColor": "#FFFDF8", "contents": [
            {"type": "text", "text": "一日樂食・一週趨勢", "size": "sm", "weight": "bold", "color": "#6B8F71"},
            {"type": "text", "text": "最近 7 天", "size": "lg", "weight": "bold", "color": "#243B2D", "margin": "sm"},
        ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "18px", "contents": [
            *section("🔥 熱量", "calories_kcal", calorie_goal, "kcal", "#6B8F71"),
            *section("🥩 蛋白質", "protein_g", protein_goal, "g", "#E3B341"),
            {"type": "text", "text": "只計入已確認的飲食紀錄；沒記錄的日子顯示為灰色。", "size": "xxs", "color": "#8A8F8B", "wrap": True, "margin": "lg"},
        ]},
        "footer": {"type": "box", "layout": "horizontal", "spacing": "sm", "paddingAll": "16px", "contents": [
            {"type": "button", "action": {"type": "message", "label": "回今日總覽", "text": "首頁"}, "style": "primary", "color": "#6B8F71", "flex": 1, "height": "sm"},
        ]},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
