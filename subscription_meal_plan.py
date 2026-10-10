"""Pure helpers for subscription form parsing and meal-plan composition."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


def _normalized_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        char for char in text
        if not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


_SUBSCRIPTION_UID_LABELS = frozenset(
    _normalized_label(label)
    for label in (
        "1. LINE UID (系統綁定用，請勿修改)",
        "LINE UID",
        "UID",
    )
)
_LINE_UID_RE = re.compile(r"U[0-9a-fA-F]{32}")


def get_subscription_form_uid(data: Mapping[str, Any]) -> str:
    """Return a valid UID from one explicitly registered form label only."""
    matches = [
        value
        for key, value in data.items()
        if _normalized_label(key) in _SUBSCRIPTION_UID_LABELS
    ]
    if len(matches) > 1:
        raise ValueError("multiple UID fields")
    if not matches:
        return ""
    uid = str(matches[0] or "").strip()
    return uid if _LINE_UID_RE.fullmatch(uid) else ""


def get_subscription_form_value(
    data: Mapping[str, Any],
    *aliases: str,
    excluded_fragments: Sequence[str] = (),
    allow_fuzzy: bool = True,
    reject_ambiguous: bool = False,
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

    def answer(value: Any) -> str:
        if isinstance(value, list):
            return ",".join(str(item) for item in value if str(item).strip())
        return str(value)

    # Exact labels are deterministic. A fuzzy alias is accepted only when it
    # identifies one field; ambiguous revisions fail closed instead of relying
    # on JSON insertion order.
    if reject_ambiguous:
        exact_answers = {
            answer(value)
            for key, value in candidates
            if key in normalized_aliases
        }
        if len(exact_answers) > 1:
            raise ValueError("conflicting exact form fields")
    for alias in normalized_aliases:
        exact_matches = [value for key, value in candidates if key == alias]
        fuzzy_matches = (
            [value for key, value in candidates if alias in key]
            if allow_fuzzy
            else exact_matches
        )
        if reject_ambiguous and len({answer(value) for value in fuzzy_matches}) > 1:
            raise ValueError("ambiguous form field")
        if exact_matches:
            return answer(exact_matches[0])
        if not allow_fuzzy:
            continue
        if len(fuzzy_matches) == 1:
            return answer(fuzzy_matches[0])
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
    clauses = re.split(r"[,，、;；\n/]|(?:但是|但|不過)", restrictions)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        if re.search(
            r"(?<!不)(?:可以吃|可以|可吃|能吃|不忌|不過敏|沒有過敏|無過敏)$",
            clause,
        ):
            continue
        if re.fullmatch(r"(?:無|沒有)(?:任何)?(?:禁忌|過敏|不耐受?)", clause):
            continue
        clause = re.sub(r"^(?:我|本人)?對", "", clause)
        clause = re.sub(
            r"^(?:我|本人)?(?:對)?(?:不要吃|不吃|不要|避免|禁忌|不能吃|會對)",
            "",
            clause,
        )
        clause = re.sub(
            r"(?:我)?(?:都不能吃|都不要吃|都不吃|不能吃|不要吃|不吃|避免)$",
            "",
            clause,
        )
        clause = re.sub(r"(?:過敏|不耐受?)$", "", clause).strip()
        for raw_term in re.split(r"(?:以及|還有|和|及|與|跟)", clause):
            term = raw_term.strip()
            if term and term not in {"無", "無禁忌", "沒有禁忌"}:
                terms.add(term)
                if term.endswith("肉") and len(term) > 2:
                    terms.add(term[:-1])

    expanded_terms = set(terms)
    for term in terms:
        if "海鮮" in term:
            expanded_terms.update({
                "魚", "蝦", "蟹", "花枝", "透抽", "章魚", "牡蠣", "蚵", "蛤",
                "干貝", "貝", "鮭", "鱸", "鮪", "鯖",
            })
        if "甲殼" in term:
            expanded_terms.update({"蝦", "蟹", "龍蝦"})
        if "乳製" in term or "乳糖" in term:
            expanded_terms.update({
                "奶", "奶油", "起司", "乳酪", "乳清", "優格", "鮮奶油",
            })
    terms = expanded_terms

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
        raise ValueError("preferred-protein light meal required")

    active_weeks = list(dict.fromkeys(int(row[0]) for row in result))
    replacement_number = 0
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

        replacement_pool = non_light_bentos
        for row_index, meal_position in all_light_positions:
            if (row_index, meal_position) == keeper:
                continue
            if not replacement_pool:
                raise ValueError("non-light meal required to cap 食蔬 coverage")
            row = list(result[row_index])
            other_meal = row[5 if meal_position == 4 else 4]
            distinct_pool = [dish for dish in replacement_pool if dish != other_meal]
            if not distinct_pool:
                raise ValueError("distinct non-light meal required")
            row[meal_position] = distinct_pool[
                replacement_number % len(distinct_pool)
            ]
            result[row_index] = tuple(row)
            replacement_number += 1
    return result


_MENU_DATE_LINE_RE = re.compile(r"^20\d{2}/\d{2}/\d{2}")


def build_plain_subscription_menu_summary(plan_requests: Sequence[tuple], start_date) -> str:
    """Render selected subscription dates with meal names only."""
    blocks = []
    for week_number, day_number, _week_label, day_name, lunch, dinner in plan_requests:
        target_date = start_date + timedelta(
            days=(int(week_number) - 1) * 7 + (int(day_number) - 1)
        )
        blocks.append(
            f"{target_date:%Y/%m/%d}（{day_name}）\n"
            f"午：{lunch['name']}\n"
            f"晚：{dinner['name']}"
        )
    return "\n\n".join(blocks)


_MENU_WEEKDAYS = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")


def render_schedule_menu_text(rows) -> str:
    """Render current schedule rows ``(YYYY-MM-DD, source_columns)`` as the
    customer menu, in the same block shape as the activation summary.
    Dates left empty by a reschedule are omitted."""
    blocks = []
    for day, columns in sorted(rows, key=lambda item: item[0]):
        lunch = str(columns[2] or "").strip()
        dinner = str(columns[5] or "").strip()
        if lunch in ("", "無") and dinner in ("", "無"):
            continue
        service_date = date.fromisoformat(day)
        blocks.append(
            f"{service_date:%Y/%m/%d}（{_MENU_WEEKDAYS[service_date.weekday()]}）\n"
            f"午：{lunch or '無'}\n"
            f"晚：{dinner or '無'}"
        )
    return "\n\n".join(blocks)


def sanitize_legacy_subscription_menu(summary_text: object) -> str:
    """Hide legacy generated training/carb-cycle decoration without changing meals."""
    kept = []
    for raw_line in str(summary_text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        # These ledger receipts remain in canonical storage for administrators,
        # but their before/after cells (including prices) are not customer meals.
        if stripped.startswith(("⏸ 人工查核[", "🔄 人工查核[")):
            continue
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if stripped.startswith("課："):
            continue
        if "4 週訓練課表已生成" in stripped:
            continue
        if "高強度日" in stripped and "低強度" in stripped:
            continue
        if "已依現有課表強度重新套用碳循環" in stripped:
            continue
        if _MENU_DATE_LINE_RE.match(stripped):
            line = re.sub(r"\s*[🔥🥗]\s*(?:高碳|低碳)\s*$", "", line)
        kept.append(line)
    return "\n".join(kept).strip("\n")


def chunk_subscription_menu_text(
    menu_text: object,
    *,
    heading: str,
    footer: str,
    max_chars: int = 5000,
    max_messages: int = 5,
) -> list[str]:
    """Pack complete menu paragraphs into LINE-safe UTF-16-sized messages."""
    def line_units(text: str) -> int:
        # LINE's text limit is enforced on Java/JSON string units.  Counting
        # UTF-16 prevents astral emoji from being under-counted by Python len().
        return len(text.encode("utf-16-le")) // 2

    sanitized = sanitize_legacy_subscription_menu(menu_text)
    atoms = []
    if heading.strip():
        atoms.append(heading.strip())
    atoms.extend(part.strip() for part in re.split(r"\n{2,}", sanitized) if part.strip())
    if footer.strip():
        atoms.append(footer.strip())
    chunks: list[str] = []
    current = ""
    for atom in atoms:
        if line_units(atom) > max_chars:
            raise ValueError("a subscription menu block exceeds the LINE text limit")
        candidate = atom if not current else f"{current}\n\n{atom}"
        if line_units(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        current = atom
    if current:
        chunks.append(current)
    if len(chunks) > max_messages:
        raise ValueError("subscription menu exceeds one LINE reply")
    return chunks
