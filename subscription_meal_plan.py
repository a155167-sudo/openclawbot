"""Pure helpers for subscription form parsing and meal-plan composition."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


def _normalized_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        char for char in text
        if not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


def get_subscription_form_value(
    data: Mapping[str, Any],
    *aliases: str,
    excluded_fragments: Sequence[str] = (),
) -> str:
    """Read a form answer without confusing positive and negative preference fields."""
    normalized_aliases = [
        _normalized_label(alias) for alias in aliases if _normalized_label(alias)
    ]
    normalized_exclusions = [
        _normalized_label(fragment)
        for fragment in excluded_fragments
        if _normalized_label(fragment)
    ]

    candidates: list[tuple[str, Any]] = []
    for key, value in data.items():
        normalized_key = _normalized_label(key)
        if not value or any(part in normalized_key for part in normalized_exclusions):
            continue
        candidates.append((normalized_key, value))

    for alias in normalized_aliases:
        for exact in (True, False):
            for normalized_key, value in candidates:
                matches = normalized_key == alias if exact else alias in normalized_key
                if not matches:
                    continue
                if isinstance(value, list):
                    return ",".join(str(item) for item in value if str(item).strip())
                return str(value)
    return ""


def dish_matches_restrictions(
    dish: Mapping[str, Any], restriction_text: object
) -> bool:
    """Return whether a dish name or ingredient list contains a stated restriction."""
    restrictions = unicodedata.normalize(
        "NFKC", str(restriction_text or "")
    ).casefold()
    if not restrictions.strip():
        return False

    terms: set[str] = set()
    for raw_term in re.split(
        r"[,，、;；\n/]|(?:以及|還有|和|及|與|跟)", restrictions
    ):
        term = raw_term.strip()
        term = re.sub(r"^(?:我|本人)?對", "", term)
        term = re.sub(
            r"^(?:我|本人)?(?:對)?(?:不要吃|不吃|不要|避免|禁忌|不能吃|會對)", "", term
        )
        term = re.sub(r"(?:過敏|不耐受?|都不吃)$", "", term).strip()
        if term and term not in {"無", "無禁忌", "沒有禁忌"}:
            terms.add(term)
            if term.endswith("肉") and len(term) > 2:
                terms.add(term[:-1])

    known_terms = {
        "牛", "豬", "雞", "羊", "魚", "海鮮", "蝦", "蟹", "豆腐", "豆",
        "蛋", "奶", "起司", "乳酪", "花生", "堅果",
    }
    terms.update(term for term in known_terms if term in restrictions)
    if "海鮮" in restrictions:
        terms.update({
            "魚", "蝦", "蟹", "花枝", "透抽", "章魚", "牡蠣", "鮭", "鱸", "鮪", "鯖",
        })
    if "乳製品" in restrictions:
        terms.update({"奶", "起司", "乳酪", "優格", "鮮奶油"})

    dish_text = _normalized_label(
        f"{dish.get('name') or ''} {dish.get('ingredients') or ''}"
    )
    return any(_normalized_label(term) in dish_text for term in terms)


def ensure_light_bento_coverage(
    plan_requests: Sequence[tuple],
    *,
    safe_menu: Sequence[Mapping[str, Any]],
    pref_staple: str,
    liked_proteins: Sequence[str],
) -> list[tuple]:
    """For all-eater plans, include one safe 食蔬 (light bento) per active week."""
    result = list(plan_requests)
    if "都不挑食" not in str(pref_staple or "") or not result:
        return result

    eligible_menu = list(safe_menu)
    if liked_proteins:
        eligible_menu = [
            dish for dish in eligible_menu
            if any(
                protein in (
                    str(dish.get("name") or "")
                    + str(dish.get("ingredients") or "")
                )
                for protein in liked_proteins
            )
        ]
    light_bentos = [
        dish for dish in eligible_menu
        if "食蔬" in str(dish.get("name") or "")
    ]
    non_light_bentos = [
        dish for dish in eligible_menu
        if "食蔬" not in str(dish.get("name") or "")
    ]
    if not light_bentos:
        return result

    active_weeks = list(dict.fromkeys(int(row[0]) for row in result))
    replacement_number = 0
    fallback_non_light = [
        dish for dish in safe_menu
        if "食蔬" not in str(dish.get("name") or "")
    ]
    for week in active_weeks:
        week_indexes = [index for index, row in enumerate(result) if int(row[0]) == week]
        all_light_positions = [
            (index, meal_position)
            for index in week_indexes
            for meal_position in (4, 5)
            if "食蔬" in str(result[index][meal_position].get("name") or "")
        ]
        matching_positions = [
            position for position in all_light_positions
            if result[position[0]][position[1]] in light_bentos
        ]
        if matching_positions:
            keeper = matching_positions[0]
        else:
            keeper = all_light_positions[0] if all_light_positions else (week_indexes[0], 4)
            row = list(result[keeper[0]])
            row[keeper[1]] = light_bentos[replacement_number % len(light_bentos)]
            result[keeper[0]] = tuple(row)
            replacement_number += 1

        replacement_pool = non_light_bentos or fallback_non_light
        for row_index, meal_position in all_light_positions:
            if (row_index, meal_position) == keeper:
                continue
            if not replacement_pool:
                raise ValueError("non-light meal required to cap 食蔬 coverage")
            row = list(result[row_index])
            row[meal_position] = replacement_pool[
                replacement_number % len(replacement_pool)
            ]
            result[row_index] = tuple(row)
            replacement_number += 1
    return result
