"""Pure, read-only assembler for a dietitian health-check source summary.

This is deliberately not an AI report generator and has no persistence or delivery
capability.  It consumes only the already-projected dietitian detail DTO.  The
caller must keep the existing versioned save/approve flow as the sole mutation
boundary.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any


_KIND = "system_data_summary_v1"
_DRAFT_WRITABLE_STATUSES = frozenset({"ready_for_review", "needs_more_info"})
_TOKEN = re.compile(r"[0-9a-f]{64}")
_NUTRIENTS: tuple[tuple[str, str, str], ...] = (
    ("calories_kcal", "熱量", "kcal"),
    ("protein_g", "蛋白質", "g"),
    ("fat_g", "脂肪", "g"),
    ("carbohydrate_g", "碳水化合物", "g"),
    ("fiber_g", "膳食纖維", "g"),
    ("sugar_g", "糖", "g"),
    ("sodium_mg", "鈉", "mg"),
    ("potassium_mg", "鉀", "mg"),
    ("calcium_mg", "鈣", "mg"),
    ("iron_mg", "鐵", "mg"),
)


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "kind": "preview_unavailable",
        "reason": reason,
        "may_generate_preview": False,
    }


def _existing_review(detail: Mapping[str, object], version: int) -> dict[str, object]:
    review = detail.get("latest_review")
    status = review.get("status") if isinstance(review, Mapping) else None
    return {
        "kind": "existing_review_preserved",
        "reason": "saved_review_exists",
        "review_version": version,
        "review_status": status,
        "may_generate_preview": False,
    }


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {name}")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"invalid {name}")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {name}")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1_000_000:
        raise ValueError(f"invalid {name}")
    return number


def _display_number(value: float) -> int | float:
    rounded = round(value, 2)
    return int(rounded) if rounded.is_integer() else rounded


def assemble_system_data_summary(detail: Mapping[str, object]) -> dict[str, object]:
    """Build an ephemeral source summary without mutating *detail* or any store.

    Fail-closed rules:
    * Any saved review version wins, even when its content is stale/hidden.
    * Only source-complete, source-token-bound, review-version-zero cases produce
      a preview.
    * Missing values remain absent/``None`` and never become zero.
    * Output is explicitly non-customer-visible and non-deliverable.
    """
    if not isinstance(detail, Mapping):
        raise ValueError("invalid detail")

    review_version = _nonnegative_int(
        detail.get("current_review_version"), "current_review_version"
    )
    if review_version > 0:
        return _existing_review(detail, review_version)
    if detail.get("latest_review") is not None or detail.get("latest_review_available") is True:
        raise ValueError("review projection conflicts with version zero")

    if detail.get("status") not in _DRAFT_WRITABLE_STATUSES:
        return _unavailable("case_not_draft_writable")

    source_token = detail.get("source_token")
    if not isinstance(source_token, str) or _TOKEN.fullmatch(source_token) is None:
        return _unavailable("source_binding_unavailable")

    integrity = _mapping(detail.get("source_integrity"), "source_integrity")
    referenced = _nonnegative_int(integrity.get("referenced_count"), "referenced_count")
    available = _nonnegative_int(
        integrity.get("available_snapshot_count"), "available_snapshot_count"
    )
    all_available = integrity.get("all_snapshots_available")
    if not isinstance(all_available, bool):
        raise ValueError("invalid source availability flag")
    if all_available and referenced != available:
        raise ValueError("source availability flag conflicts with counts")
    if not all_available or referenced != available:
        return _unavailable("source_projection_incomplete")

    source_logs = _sequence(detail.get("source_logs"), "source_logs")
    if len(source_logs) != referenced:
        raise ValueError("source count conflicts with integrity projection")
    valid_days = _sequence(detail.get("valid_days"), "valid_days")

    qualified_dates: list[str] = []
    all_dates: list[str] = []
    day_metadata: dict[str, dict[str, object]] = {}
    qualifying_meals = 0
    for index, raw_day in enumerate(valid_days):
        day = _mapping(raw_day, f"valid_days[{index}]")
        local_date = day.get("local_date")
        completeness = day.get("completeness_status")
        if not isinstance(local_date, str) or not local_date or local_date in day_metadata:
            raise ValueError("invalid or duplicate valid-day date")
        if completeness not in {"qualified", "incomplete"}:
            raise ValueError("invalid completeness status")
        meal_count = _nonnegative_int(
            day.get("qualifying_meal_count"), "qualifying_meal_count"
        )
        all_dates.append(local_date)
        day_metadata[local_date] = {
            "local_date": local_date,
            "completeness_status": completeness,
            "qualifying_meal_count": meal_count,
        }
        if completeness == "qualified":
            qualified_dates.append(local_date)
            qualifying_meals += meal_count
    if not qualified_dates:
        return _unavailable("no_qualified_days")

    seen_ids: set[str] = set()
    source_counts_by_date = {local_date: 0 for local_date in all_dates}
    totals_by_key: dict[str, dict[str, float]] = {key: {} for key, _label, _unit in _NUTRIENTS}
    record_counts: dict[str, int] = {key: 0 for key, _label, _unit in _NUTRIENTS}
    value_counts_by_key: dict[str, dict[str, int]] = {
        key: {} for key, _label, _unit in _NUTRIENTS
    }
    for index, raw_log in enumerate(source_logs):
        log = _mapping(raw_log, f"source_logs[{index}]")
        log_id = log.get("log_id")
        local_date = log.get("local_date")
        if not isinstance(log_id, str) or not log_id or log_id in seen_ids:
            raise ValueError("invalid or duplicate source identity")
        if not isinstance(local_date, str) or local_date not in day_metadata:
            raise ValueError("source is not bound to a projected date")
        seen_ids.add(log_id)
        source_counts_by_date[local_date] += 1
        nutrition = _mapping(log.get("nutrition_snapshot"), "nutrition_snapshot")
        for key, _label, _unit in _NUTRIENTS:
            if key not in nutrition or nutrition[key] is None:
                continue
            value = _number(nutrition[key], f"nutrition_snapshot.{key}")
            totals_by_key[key][local_date] = totals_by_key[key].get(local_date, 0.0) + value
            record_counts[key] += 1
            value_counts_by_key[key][local_date] = (
                value_counts_by_key[key].get(local_date, 0) + 1
            )

    nutrition_summary: dict[str, dict[str, object]] = {}
    labels: dict[str, tuple[str, str]] = {}
    for key, label, unit in _NUTRIENTS:
        daily = totals_by_key[key]
        if not daily:
            continue
        complete_qualified_dates = [
            local_date for local_date in qualified_dates
            if source_counts_by_date[local_date] > 0
            and value_counts_by_key[key].get(local_date, 0) == source_counts_by_date[local_date]
        ]
        partial_dates = [
            local_date for local_date in all_dates
            if local_date in daily and local_date not in complete_qualified_dates
        ]
        qualified_sum = sum(
            (daily[local_date] for local_date in complete_qualified_dates), 0.0
        )
        partial_sum = sum((daily[local_date] for local_date in partial_dates), 0.0)
        nutrition_summary[key] = {
            "recorded_sum": _display_number(sum(daily.values())),
            "qualified_recorded_sum": _display_number(qualified_sum),
            "qualified_day_average": (
                _display_number(qualified_sum / len(complete_qualified_dates))
                if complete_qualified_dates else None
            ),
            "qualified_days_with_complete_value": len(complete_qualified_dates),
            "partial_days_with_recorded_value": len(partial_dates),
            "partial_recorded_sum": _display_number(partial_sum),
            "source_records_with_value": record_counts[key],
            "unit": unit,
        }
        labels[key] = (label, unit)

    day_summaries: list[dict[str, object]] = []
    for local_date in all_dates:
        daily_nutrition = {
            key: _display_number(totals_by_key[key][local_date])
            for key, _label, _unit in _NUTRIENTS
            if local_date in totals_by_key[key]
            and day_metadata[local_date]["completeness_status"] == "qualified"
            and source_counts_by_date[local_date] > 0
            and value_counts_by_key[key].get(local_date, 0) == source_counts_by_date[local_date]
        }
        partial_nutrition = {
            key: _display_number(totals_by_key[key][local_date])
            for key, _label, _unit in _NUTRIENTS
            if local_date in totals_by_key[key] and key not in daily_nutrition
        }
        day_summaries.append({
            **day_metadata[local_date],
            "source_count": source_counts_by_date[local_date],
            "nutrition_totals": daily_nutrition,
            "partial_nutrition_recorded": partial_nutrition,
        })

    profile = _mapping(detail.get("profile"), "profile")
    calorie_target = profile.get("tdee")
    protein_target = profile.get("protein")
    targets = {
        "calories_kcal": None if calorie_target is None else _display_number(
            _number(calorie_target, "profile.tdee")
        ),
        "protein_g": None if protein_target is None else _display_number(
            _number(protein_target, "profile.protein")
        ),
    }

    limitations: list[str] = []
    if targets["calories_kcal"] is None:
        limitations.append("熱量目標未提供，不進行達標率或缺口判讀。")
    if targets["protein_g"] is None:
        limitations.append("蛋白質目標未提供，不進行達標率或缺口判讀。")
    if profile.get("goal") in (None, ""):
        limitations.append("顧客目標未提供，個人化方向需由營養師確認。")
    if profile.get("restrictions") in (None, ""):
        limitations.append("飲食限制未提供，建議內容需由營養師確認。")

    if len(all_dates) == len(qualified_dates):
        coverage_observation = (
            f"{len(qualified_dates)} 個符合完整度規則的日期，共有 {referenced} 筆可驗證營養快照。"
        )
    else:
        coverage_observation = (
            f"共記錄 {len(all_dates)} 個日期，其中 {len(qualified_dates)} 個符合完整度規則；"
            f"共有 {referenced} 筆可驗證營養快照。"
        )
    observations = [coverage_observation]
    for key in ("calories_kcal", "protein_g"):
        if key not in nutrition_summary:
            continue
        summary = nutrition_summary[key]
        label, unit = labels[key]
        complete_days = int(summary["qualified_days_with_complete_value"])
        incomplete_qualified_days = len(qualified_dates) - complete_days
        average = summary["qualified_day_average"]
        if complete_days:
            text = (
                f"{label}在 {complete_days} 個符合記錄門檻且該欄位完整的日期，"
                f"已記錄量日平均為 {average} {unit}"
            )
        else:
            text = f"{label}沒有可計算已記錄量日平均的完整門檻日"
        partial_days = int(summary["partial_days_with_recorded_value"])
        if partial_days:
            text += (
                f"；另有 {partial_days} 個部分或非完整日期僅呈現已記錄量共 "
                f"{summary['partial_recorded_sum']} {unit}，不納入日平均"
            )
        if incomplete_qualified_days:
            text += f"；{incomplete_qualified_days} 個門檻日的{label}欄位不完整"
        observations.append(text + "。")

    return {
        "kind": _KIND,
        "display_label": "系統初稿（未儲存，待營養師審核）",
        "persistence": "not_saved",
        "customer_visible": False,
        "delivery_eligible": False,
        "requires_dietitian_review": True,
        "may_generate_preview": True,
        "source_binding": {
            "source_token": source_token,
            "review_version": 0,
        },
        "coverage": {
            "observed_day_count": len(all_dates),
            "qualified_day_count": len(qualified_dates),
            "qualifying_meal_count": qualifying_meals,
            "source_count": referenced,
            "all_snapshots_available": True,
        },
        "days": day_summaries,
        "nutrition": nutrition_summary,
        "targets": targets,
        "observations": observations,
        "limitations": limitations,
        "editable_seed": {
            "good": " ".join(observations),
            "priority": (
                "資料限制與優先事項仍需由營養師確認。"
                if not limitations
                else "資料限制：" + " ".join(limitations) + " 優先事項仍需由營養師確認。"
            ),
            "next_7_days": "由營養師依已驗證資料、顧客目標與專業判讀完成接下來 7 天內容。",
            "comment": "",
        },
        "dietitian_tasks": [
            "判讀飲食型態與優先改善事項",
            "依顧客目標、限制與臨床專業撰寫個人化建議",
            "確認內容後另行儲存版本化草稿；本摘要不會自動送出",
        ],
    }
