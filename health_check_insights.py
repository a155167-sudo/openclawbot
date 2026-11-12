"""三日飲食分析：先由程式把指標算好，AI 只負責把結果寫成人話。

為什麼不讓 AI 自己看原始紀錄：它會算錯、漏看，而且每次寫的重點不一樣。
這裡算的東西都只用到已確認的飲食紀錄與客人自己的目標，不做任何疾病或體重預測。

分析角度（Jason 2026-11-12 定）：
1. 餐次分布：蛋白質集中在哪一餐、早餐有沒有吃、最後一餐幾點。
2. 自家餐 vs 外食：便當的數字是準的，外食是估的；問題出在哪一邊。
3. 飲料與點心：幾次、佔總熱量多少。
4. 只給一個最該改的重點（focus），其餘是次要。
另外附：蛋白質差距換成食物、資料可信度。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from nutrition_plausibility import _category

OWN_MENU_SOURCES = frozenset({"planned_meal", "official_menu", "qa_health_check_seed"})
DRINK_CATEGORIES = frozenset({"飲料類", "豆漿類", "乳品飲料類"})
SLOTS = ("早餐", "午餐", "晚餐", "點心")
# 補蛋白質用的常見食物（每份大約的蛋白質克數），由大到小；只用來把「差幾克」換成看得懂的份量。
PROTEIN_FOODS = (("雞胸肉一份（約 100 克）", 23), ("無糖豆漿一杯（約 400 毫升）", 14), ("茶葉蛋一顆", 7))


def _num(value) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part: float, whole: float) -> int | None:
    return round(part / whole * 100) if whole and whole > 0 else None


def protein_gap_as_food(gap_g: float) -> str:
    """把每天少的蛋白質克數換成食物份量，例如 35 → 「雞胸肉一份＋無糖豆漿一杯」。"""
    remaining = max(0.0, float(gap_g))
    if remaining < 5:
        return ""
    picks = []
    for name, grams in PROTEIN_FOODS:
        if remaining >= grams * 0.8 and len(picks) < 3:
            picks.append(name)
            remaining -= grams
    if not picks:
        picks.append(PROTEIN_FOODS[-1][0])
    return "＋".join(picks)


def compute_insights(
    meals: Sequence[Mapping[str, Any]], *, kcal_target: float | None, protein_target: float | None,
    complete_days: int,
) -> dict:
    """meals：每筆 {date, slot, name, kcal, protein, own_menu(bool), estimated(bool), hour(int|None)}。"""
    days: dict[str, list[Mapping]] = defaultdict(list)
    for meal in meals:
        days[str(meal["date"])].append(meal)
    n_days = len(days)
    total_kcal = sum(_num(m.get("kcal")) or 0 for m in meals)
    total_protein = sum(_num(m.get("protein")) or 0 for m in meals)
    avg_kcal = round(total_kcal / n_days) if n_days else 0
    avg_protein = round(total_protein / n_days) if n_days else 0

    # 1. 餐次分布
    slot_protein = {slot: 0.0 for slot in SLOTS}
    slot_kcal = {slot: 0.0 for slot in SLOTS}
    for meal in meals:
        slot = meal.get("slot") if meal.get("slot") in SLOTS else "點心"
        slot_protein[slot] += _num(meal.get("protein")) or 0
        slot_kcal[slot] += _num(meal.get("kcal")) or 0
    breakfast_days = sum(1 for items in days.values() if any(m.get("slot") == "早餐" for m in items))
    last_hours = [max(h for h in (m.get("hour") for m in items) if isinstance(h, int))
                  for items in days.values() if any(isinstance(m.get("hour"), int) for m in items)]
    top_slot = max(SLOTS, key=lambda s: slot_protein[s]) if total_protein else None
    distribution = {
        "各餐蛋白質佔比_%": {s: _pct(slot_protein[s], total_protein) for s in SLOTS if slot_protein[s]},
        "蛋白質最集中的餐": top_slot,
        "有記早餐的天數": breakfast_days,
        "記錄天數": n_days,
        "早餐平均蛋白質_g": round(slot_protein["早餐"] / breakfast_days) if breakfast_days else 0,
        "最晚一餐的時間_點": max(last_hours) if last_hours else None,
    }

    # 2. 自家餐 vs 外食
    own = [m for m in meals if m.get("own_menu")]
    outside = [m for m in meals if not m.get("own_menu")]
    own_kcal = sum(_num(m.get("kcal")) or 0 for m in own)
    out_kcal = sum(_num(m.get("kcal")) or 0 for m in outside)
    source_split = {
        "包月便當_餐數": len(own),
        "便當以外_筆數": len(outside),
        "包月便當_平均每餐熱量": round(own_kcal / len(own)) if own else None,
        "便當以外_平均每筆熱量": round(out_kcal / len(outside)) if outside else None,
        "便當以外佔總熱量_%": _pct(out_kcal, total_kcal),
        "便當以外佔總蛋白質_%": _pct(sum(_num(m.get("protein")) or 0 for m in outside), total_protein),
    }

    # 3. 飲料與點心
    def is_drink(meal):
        found = _category(str(meal.get("name") or ""))
        return bool(found and found[0] in DRINK_CATEGORIES)

    drinks = [m for m in meals if is_drink(m)]
    snacks = [m for m in meals if m.get("slot") == "點心" and not is_drink(m)]
    extra_kcal = sum(_num(m.get("kcal")) or 0 for m in drinks + snacks)
    extras = {
        "飲料_次數": len(drinks),
        "飲料_品名": [str(m.get("name"))[:20] for m in drinks][:6],
        "飲料_總熱量": round(sum(_num(m.get("kcal")) or 0 for m in drinks)),
        "點心_次數": len(snacks),
        "飲料加點心佔總熱量_%": _pct(extra_kcal, total_kcal),
        "飲料加點心_平均每天熱量": round(extra_kcal / n_days) if n_days else 0,
    }

    # 資料可信度
    estimated = sum(1 for m in meals if m.get("estimated"))
    meals_per_day = len(meals) / n_days if n_days else 0
    if complete_days >= 3 and meals_per_day >= 3 and (not meals or estimated / len(meals) <= 0.5):
        level = "高"
    elif complete_days >= 3 and meals_per_day >= 2:
        level = "中"
    else:
        level = "低"
    reliability = {
        "等級": level, "完整記錄天數": complete_days, "總筆數": len(meals),
        "照片或文字估算的筆數": estimated, "平均每天筆數": round(meals_per_day, 1),
    }

    kcal_pct = _pct(avg_kcal, kcal_target or 0)
    protein_pct = _pct(avg_protein, protein_target or 0)
    protein_gap = round((protein_target or 0) - avg_protein) if protein_target else 0
    summary = {
        "平均每天熱量": avg_kcal, "每日熱量目標": round(kcal_target) if kcal_target else None,
        "熱量達成率_%": kcal_pct,
        "平均每天蛋白質_g": avg_protein, "每日蛋白質目標_g": round(protein_target) if protein_target else None,
        "蛋白質達成率_%": protein_pct,
        "蛋白質每天差距_g": protein_gap if protein_gap > 0 else 0,
        "蛋白質差距約等於": protein_gap_as_food(protein_gap) if protein_gap > 0 else "",
    }

    return {
        "總覽": summary, "餐次分布": distribution, "便當與外食": source_split,
        "飲料與點心": extras, "資料可信度": reliability,
        "本週重點": _pick_focus(summary, distribution, source_split, extras, reliability),
    }


def _pick_focus(summary, distribution, source_split, extras, reliability) -> dict:
    """只挑一件最該先做的事；其餘列為次要。順序＝先確定資料可信，再看熱量超標的來源，再看蛋白質。"""
    kcal_pct = summary["熱量達成率_%"]
    protein_pct = summary["蛋白質達成率_%"]
    extra_pct = extras["飲料加點心佔總熱量_%"] or 0
    outside_pct = source_split["便當以外佔總熱量_%"] or 0
    candidates: list[tuple[str, str]] = []

    if reliability["等級"] == "低":
        candidates.append(("補齊紀錄", "這三天記錄的餐數偏少，先把每天三餐（含飲料）都記下來，下一次分析才準。"))
    if kcal_pct is not None and kcal_pct > 110:
        if extra_pct >= 15:
            candidates.append(("飲料與點心", f"熱量超過目標，其中飲料和點心就佔了 {extra_pct}%，"
                                         f"平均每天約 {extras['飲料加點心_平均每天熱量']} 大卡。"))
        if outside_pct >= 40 and source_split["便當以外_平均每筆熱量"]:
            candidates.append(("便當以外那一餐", f"熱量超過目標，而便當以外的部分佔總熱量 {outside_pct}%，"
                                            f"平均每筆 {source_split['便當以外_平均每筆熱量']} 大卡。"))
        if not candidates or candidates[-1][0] == "補齊紀錄":
            candidates.append(("整體份量", f"平均每天 {summary['平均每天熱量']} 大卡，是目標的 {kcal_pct}%。"))
    if protein_pct is not None and protein_pct < 80:
        where = ""
        if distribution["有記早餐的天數"] == 0:
            where = "三天都沒有記早餐，從早餐補最容易。"
        elif distribution["早餐平均蛋白質_g"] < 15:
            where = f"早餐平均只有 {distribution['早餐平均蛋白質_g']} 克，從早餐補最容易。"
        food = summary["蛋白質差距約等於"]
        candidates.append(("蛋白質", f"蛋白質平均每天 {summary['平均每天蛋白質_g']} 克，只到目標的 {protein_pct}%，"
                                  f"每天大約少 {summary['蛋白質每天差距_g']} 克"
                                  + (f"（約等於{food}）" if food else "") + "。" + where))
    if kcal_pct is not None and kcal_pct < 75:
        candidates.append(("吃得太少", f"平均每天 {summary['平均每天熱量']} 大卡，只到目標的 {kcal_pct}%，"
                                    "長期吃太少容易沒精神、也不容易維持。"))
    if not candidates:
        candidates.append(("維持現在的節奏", "熱量和蛋白質都在目標附近，這三天的安排可以繼續。"))
    return {
        "最該先做的一件事": candidates[0][0], "依據": candidates[0][1],
        "次要": [f"{name}：{why}" for name, why in candidates[1:3]],
    }
