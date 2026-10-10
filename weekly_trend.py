"""Pure weekly-trend rendering and read-only SQLite projection."""

from contextlib import closing
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from pathlib import Path
import sqlite3
from urllib.parse import quote


_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]
_GRAY = "#E6E9E7"


def _known_nonnegative(value):
    """Return a finite non-negative float, or None for unknown nutrition."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _positive_goal(value):
    number = _known_nonnegative(value)
    return number if number is not None and number > 0 else None


def _display_number(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "NA"
    if not number.is_finite():
        return "NA"
    rounded = number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.1f}".rstrip("0").rstrip(".")


def _readonly_uri(db_path):
    absolute = Path(db_path).expanduser().resolve()
    return f"file:{quote(absolute.as_posix(), safe='/:')}?mode=ro"


def read_weekly_trend(*, db_path, user_id, today, project_items, days=7):
    """Read at most one week through the caller's canonical item projection.

    ``today`` is supplied by the caller so its Taipei-local date boundary remains
    authoritative. The database is opened in SQLite read-only and query-only modes.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
        raise ValueError("days must be an integer from 1 through 7")

    result_days = []
    uri = _readonly_uri(db_path)
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        profile = conn.execute(
            "SELECT tdee,protein FROM health_profile WHERE user_id=?", (user_id,)
        ).fetchone()

        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            items = list(project_items(conn, user_id, day.isoformat()) or [])
            calorie_total = 0.0
            protein_total = 0.0
            calorie_unknown = False
            protein_unknown = False

            for item in items:
                nutrition = item.get("nutrition") or {}
                calories = _known_nonnegative(nutrition.get("calories_kcal"))
                protein = _known_nonnegative(nutrition.get("protein_g"))
                if calories is None:
                    calorie_unknown = True
                else:
                    calorie_total += calories
                if protein is None:
                    protein_unknown = True
                else:
                    protein_total += protein

            result_days.append({
                "date": day.isoformat(),
                "label": "今" if offset == 0 else _WEEKDAYS[day.weekday()],
                "logged": bool(items),
                "calories_kcal": None if not items or calorie_unknown else round(calorie_total, 1),
                "protein_g": None if not items or protein_unknown else round(protein_total, 1),
            })

    return {
        "days": result_days,
        "calorie_goal": _positive_goal(profile[0]) if profile else None,
        "protein_goal": _positive_goal(profile[1]) if profile else None,
    }


def build_weekly_trend_contents(days, *, calorie_goal=None, protein_goal=None):
    """Build the v52-style weekly calories/protein Flex bubble from inputs only."""
    calorie_goal = _positive_goal(calorie_goal)
    protein_goal = _positive_goal(protein_goal)

    def known_value(day, key):
        if not day.get("logged"):
            return None
        return _known_nonnegative(day.get(key))

    def bar_column(day, key, scale_max, color):
        value = known_value(day, key)
        height = 2
        if value is not None and scale_max > 0:
            height = max(
                2,
                int((Decimal(str(value)) * 60 / scale_max).to_integral_value(ROUND_HALF_UP)),
            )
        bar = {
            "type": "box", "layout": "vertical", "height": f"{height}px",
            "backgroundColor": color if value is not None else _GRAY,
            "cornerRadius": "3px", "contents": [],
        }
        return {
            "type": "box", "layout": "vertical", "flex": 1, "spacing": "xs",
            "contents": [
                {"type": "box", "layout": "vertical", "height": "62px",
                 "justifyContent": "flex-end", "contents": [bar]},
                {"type": "text", "text": str(day.get("label") or ""), "size": "xxs",
                 "color": "#66736B", "align": "center"},
            ],
        }

    def section(title, key, goal, unit, color):
        values = [known_value(day, key) for day in days]
        logged = [value for value in values if value is not None]
        scale_values = [Decimal(str(value)) for value in logged]
        if goal is not None:
            scale_values.append(Decimal(str(goal)))
        scale_max = max(scale_values) if scale_values else Decimal("0")
        average = (
            sum((Decimal(str(value)) for value in logged), Decimal("0")) / len(logged)
            if logged else None
        )
        if average is None:
            summary = "這週還沒有紀錄"
        else:
            summary = f"有紀錄 {len(logged)} 天・平均 {_display_number(average.quantize(Decimal('1')))} {unit}"
            if goal is not None:
                summary += f"（目標 {_display_number(goal)} {unit}）"
        return [
            {"type": "text", "text": title, "size": "sm", "weight": "bold",
             "color": "#243B2D", "margin": "lg"},
            {"type": "text", "text": summary, "size": "xs", "color": "#66736B",
             "wrap": True, "margin": "xs"},
            {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "sm",
             "contents": [bar_column(day, key, scale_max, color) for day in days]},
        ]

    return {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "18px",
            "backgroundColor": "#FFFDF8", "contents": [
                {"type": "text", "text": "一日樂食・一週趨勢", "size": "sm",
                 "weight": "bold", "color": "#6B8F71"},
                {"type": "text", "text": "最近 7 天", "size": "lg",
                 "weight": "bold", "color": "#243B2D", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "contents": [
                *section("🔥 熱量", "calories_kcal", calorie_goal, "kcal", "#6B8F71"),
                *section("🥩 蛋白質", "protein_g", protein_goal, "g", "#E3B341"),
                {"type": "text", "text": "只計入已確認的飲食紀錄；沒記錄的日子顯示為灰色。",
                 "size": "xxs", "color": "#8A8F8B", "wrap": True, "margin": "lg"},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm", "paddingAll": "16px",
            "contents": [{
                "type": "button",
                "action": {"type": "message", "label": "回今日總覽", "text": "首頁"},
                "style": "primary", "color": "#6B8F71", "flex": 1, "height": "sm",
            }],
        },
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
