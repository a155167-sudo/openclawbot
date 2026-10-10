"""包月餐三狀態：待吃 → 吃了嗎 → 已吃（餐期過後自動）；顧客可在當天改成「沒吃」.

Product rules (Jason, 2026-10-10):
- 午餐 11:30 起顯示「吃了嗎」，14:00 後自動算已吃；晚餐 17:30／20:30。
- 顧客沒回應視為有吃；自動已吃會寫入飲食紀錄（算進熱量、蛋白質與報告）。
- 「沒吃」只能在當天 23:59 前修改。
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

MEAL_WINDOWS = {
    "午餐": (time(11, 30), time(14, 0)),
    "晚餐": (time(17, 30), time(20, 30)),
}

PHASE_BEFORE = "before"   # 待吃
PHASE_ASKING = "asking"   # 吃了嗎
PHASE_AFTER = "after"     # 餐期已過 → 自動已吃

SKIP_POSTBACK_PREFIX = "pm:v1:skip:"


def _taipei(now: datetime) -> datetime:
    return now.replace(tzinfo=TAIPEI) if now.tzinfo is None else now.astimezone(TAIPEI)


def meal_phase(slot: str, now: datetime) -> str:
    """Phase of today's planned meal for ``slot`` at ``now`` (Taipei)."""
    window = MEAL_WINDOWS.get(slot)
    if window is None:
        return PHASE_BEFORE
    clock = _taipei(now).time()
    start, cutoff = window
    if clock < start:
        return PHASE_BEFORE
    if clock < cutoff:
        return PHASE_ASKING
    return PHASE_AFTER


def can_skip(meal_date: str, now: datetime) -> bool:
    """「沒吃」 is allowed only for today's meals (until 23:59 Taipei)."""
    try:
        return date.fromisoformat(str(meal_date)) == _taipei(now).date()
    except ValueError:
        return False


def skip_postback_data(slot: str, meal_date: str) -> str:
    return f"{SKIP_POSTBACK_PREFIX}{slot}:{meal_date}"


def parse_skip_postback(data: str):
    """Return (slot, meal_date) or None."""
    if not str(data or "").startswith(SKIP_POSTBACK_PREFIX):
        return None
    rest = str(data)[len(SKIP_POSTBACK_PREFIX):]
    slot, _, meal_date = rest.partition(":")
    if slot not in MEAL_WINDOWS:
        return None
    try:
        date.fromisoformat(meal_date)
    except ValueError:
        return None
    return slot, meal_date
