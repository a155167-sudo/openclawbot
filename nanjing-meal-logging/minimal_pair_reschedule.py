"""Offline-only minimal planner for moving a *pair* of scheduled meals.

This module deliberately has no LINE, Google credentials, Garmin, printer, or server
imports.  It produces one Google Sheets batch body and only returns a verified
receipt after exact post-write readback.  Wiring it to production is separately
blocked by the legacy printer consumer described in MINIMAL-RESCHEDULE-CHECK.md.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from meal_mutation_ledger import (
    MealMutationBinding,
    complete_meal_mutation,
    get_claimed_meal_mutation_snapshot,
    lookup_meal_mutation_for_request,
    mark_meal_mutation_rejected,
    mark_meal_mutation_unknown,
    reserve_meal_mutation,
)


MASTER_HEADERS = [
    "Date", "User_ID", "TDEE", "Lunch_Item", "Dinner_Item", "Tomorrow_Training",
    "Is_Coaching_Enabled", "Plan_Type", "Sport_Type", "Plan_Week", "Intervals_ID",
    "Intervals_API_Key", "Training_Freq", "Normal_Train_Time", "Long_Train_Day",
    "Run_Pace", "Bike_FTP", "Swim_Pace", "User_Level", "Race_Date",
    "Is_Carb_Cycling_Enabled",
]
PERSONAL_HEADERS = [
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
    "Dispatch_Row_ID", "Order_ID", "Menu_Version",
]
_EMPTY_MEALS = {"", "無", "尚未安排"}
_PRINTED = {"已列印", "已送印"}
_WRITER_PRICE_SUFFIX = re.compile(r" \(\$[0-9]+\)$")


class PairMoveConflict(RuntimeError):
    """A deterministic precondition failed; no write is allowed."""


class PairMoveUnknown(RuntimeError):
    """The external batch may have applied; callers must not retry or claim success."""


@dataclass(frozen=True)
class PendingPairRequest:
    request_id: int
    owner_user_id: str
    source_date: str
    target_date: str


@dataclass(frozen=True)
class PairMovePlan:
    request: PendingPairRequest
    workbook_id: str
    worksheet_id: int
    master_worksheet_id: int
    before_schedule: list[list[Any]]
    before_master: list[list[Any]]
    after_schedule: list[list[Any]]
    after_master: list[list[Any]]
    batch_body: dict[str, Any]
    meal_count_before: int
    meal_count_after: int


@dataclass(frozen=True)
class VerifiedPairMove:
    request_id: int
    verified: bool


@dataclass(frozen=True)
class FuturePairMoveResult:
    kind: str
    message: str
    meal_count_before: int = 0
    meal_count_after: int = 0


def _full_date(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise PairMoveConflict("full calendar date (YYYY-MM-DD) is required") from exc
    if raw != parsed.isoformat():
        raise PairMoveConflict("full calendar date (YYYY-MM-DD) is required")
    return raw


def _sheet_date(value: object) -> str:
    """Canonicalize the two persisted date spellings used by the real writers."""
    raw = str(value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%Y/%m/%d").date() if "/" in raw else date.fromisoformat(raw)
    except ValueError as exc:
        raise PairMoveConflict("sheet calendar date is invalid") from exc
    if raw not in {parsed.isoformat(), parsed.strftime("%Y/%m/%d")}:
        raise PairMoveConflict("sheet calendar date is invalid")
    return parsed.isoformat()


# Public date authority for readers that must accept exactly the same persisted
# personal-sheet date spellings as the approval planner.
parse_personal_sheet_date = _sheet_date


def locate_unique_header_index(
    rows: Sequence[Sequence[Any]], expected_headers: Sequence[Any]
) -> int:
    """Locate one exact formal header in the printer-compatible first 15 rows."""
    expected = list(expected_headers)
    matches = [
        index for index, row in enumerate(rows[:15]) if list(row) == expected
    ]
    if len(matches) != 1:
        raise PairMoveConflict("formal header is missing or ambiguous in first 15 rows")
    return matches[0]


def load_pending_pair_request(
    db_path: str | Path, *, request_id: int, owner_user_id: str,
    require_pending: bool = True,
) -> PendingPairRequest:
    """Read and bind the existing deferred request; never creates or updates it."""
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
        raise PairMoveConflict("request identity is invalid")
    owner = str(owner_user_id or "").strip()
    with sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT user_id,original_date,original_meal_type,target_date,target_meal_type,status "
            "FROM deferred_meals WHERE id=?", (request_id,),
        ).fetchone()
    if not row:
        raise PairMoveConflict("request does not exist")
    if str(row[0]).strip() != owner or not owner:
        raise PairMoveConflict("request owner differs")
    if require_pending and row[5] != "pending":
        raise PairMoveConflict("request is not pending")
    if (row[2], row[4]) != ("午餐+晚餐", "午餐+晚餐"):
        raise PairMoveConflict("request is not an atomic lunch+dinner pair move")
    return PendingPairRequest(request_id, owner, _full_date(row[1]), _full_date(row[3]))


def _positions(rows: Sequence[Sequence[Any]], *, width: int, dates: set[str],
               header_index: int, owner: str | None = None) -> dict[str, int]:
    positions: dict[str, int] = {}
    start = header_index + 1
    for index, raw in enumerate(rows[start:], start=start):
        row = list(raw)
        if len(row) != width:
            if owner is not None and len(row) > 1 and str(row[1]).strip() == owner:
                raise PairMoveConflict("owner row width differs")
            continue
        if owner is not None and str(row[1]).strip() != owner:
            continue
        try:
            day = _sheet_date(row[0])
        except PairMoveConflict:
            if owner is not None:
                raise
            continue
        if day not in dates:
            continue
        if day in positions:
            raise PairMoveConflict("duplicate date identity")
        positions[day] = index
    return positions


def _occupied(row: Sequence[Any], lunch_index: int, dinner_index: int) -> bool:
    return any(str(row[index]).strip() not in _EMPTY_MEALS for index in (lunch_index, dinner_index))


def _canonical_personal_meal_name_for_cross_view(value: Any) -> str:
    """Remove only the formal personal writer's exact `` ($<integer>)`` suffix."""
    text = str(value)
    canonical = _WRITER_PRICE_SUFFIX.sub("", text)
    if not canonical.strip():
        raise PairMoveConflict("personal/Master source meals differ")
    return canonical


def _plain_master_meal_name_for_cross_view(value: Any) -> str:
    """Master's writer contract is a non-empty plain meal name, without a price."""
    text = str(value)
    if not text.strip() or _WRITER_PRICE_SUFFIX.search(text):
        raise PairMoveConflict("personal/Master source meals differ")
    return text


def _meal_count(rows: Sequence[Sequence[Any]], *, header_index: int) -> int:
    return sum(
        str(row[index]).strip() not in _EMPTY_MEALS
        for row in rows[header_index + 1:] for index in (2, 5)
        if len(row) == 17
    )


def _cell(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        entered = {"boolValue": value}
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        entered = {"numberValue": value}
    else:
        entered = {"stringValue": "" if value is None else str(value)}
    return {"userEnteredValue": entered}


def _write_row(sheet_id: int, row_index: int | None, row: Sequence[Any]) -> dict[str, Any]:
    encoded = [{"values": [_cell(value) for value in row]}]
    if row_index is None:
        return {"appendCells": {"sheetId": sheet_id, "rows": encoded, "fields": "userEnteredValue"}}
    return {"updateCells": {
        "range": {"sheetId": sheet_id, "startRowIndex": row_index,
                  "endRowIndex": row_index + 1, "startColumnIndex": 0,
                  "endColumnIndex": len(row)},
        "rows": encoded, "fields": "userEnteredValue",
    }}


def _check_cutoffs(request: PendingPairRequest, now: datetime) -> None:
    today = now.date()
    source, target = date.fromisoformat(request.source_date), date.fromisoformat(request.target_date)
    if source <= today or target <= today:
        raise PairMoveConflict("past or today cutoff: source and target must both be strictly future Taipei dates")
    # Conservative close window, not a compare-and-swap claim.
    if (now.hour, now.minute) >= (23, 45):
        raise PairMoveConflict("future pair move is closed near the Taipei date boundary")


def plan_pair_move(
    *, request: PendingPairRequest, now: datetime,
    workbook_id: str, expected_workbook_id: str,
    worksheet_id: int, expected_worksheet_id: int,
    schedule_rows: Sequence[Sequence[Any]], master_rows: Sequence[Sequence[Any]],
    target_personal_template: Sequence[Any], target_master_template: Sequence[Any],
    master_worksheet_id: int = 20,
) -> PairMovePlan:
    """Create an exact-before/exact-after plan; this function performs no writes."""
    if not isinstance(request, PendingPairRequest):
        raise PairMoveConflict("pending request binding is required")
    if not workbook_id or workbook_id != expected_workbook_id:
        raise PairMoveConflict("workbook identity differs")
    if isinstance(worksheet_id, bool) or worksheet_id != expected_worksheet_id:
        raise PairMoveConflict("personal worksheet identity differs")
    _check_cutoffs(request, now)
    if request.source_date == request.target_date:
        raise PairMoveConflict("source and target dates are identical")
    schedule_header_index = locate_unique_header_index(schedule_rows, PERSONAL_HEADERS)
    master_header_index = locate_unique_header_index(master_rows, MASTER_HEADERS)

    days = {request.source_date, request.target_date}
    spos = _positions(
        schedule_rows, width=17, dates=days, header_index=schedule_header_index
    )
    mpos = _positions(
        master_rows, width=21, dates=days, header_index=master_header_index,
        owner=request.owner_user_id,
    )
    if request.source_date not in spos or request.source_date not in mpos:
        raise PairMoveConflict("source before-image is missing")

    source_p = list(schedule_rows[spos[request.source_date]])
    source_m = list(master_rows[mpos[request.source_date]])
    if not _occupied(source_p, 2, 5) or any(str(source_p[i]).strip() in _EMPTY_MEALS for i in (2, 5)):
        raise PairMoveConflict("source must contain both meals")
    if (
        _canonical_personal_meal_name_for_cross_view(source_p[2])
        != _plain_master_meal_name_for_cross_view(source_m[3])
        or _canonical_personal_meal_name_for_cross_view(source_p[5])
        != _plain_master_meal_name_for_cross_view(source_m[4])
    ):
        raise PairMoveConflict("personal/Master source meals differ")
    source_status = str(source_p[13]).strip()
    if source_status in _PRINTED:
        raise PairMoveConflict("source has already been physically printed")
    if source_status != "待列印":
        raise PairMoveConflict("source print status is not a known movable pending state")

    target_p = list(schedule_rows[spos[request.target_date]]) if request.target_date in spos else list(target_personal_template)
    target_m = list(master_rows[mpos[request.target_date]]) if request.target_date in mpos else list(target_master_template)
    if len(target_p) != 17 or len(target_m) != 21:
        raise PairMoveConflict("target template width differs")
    if _sheet_date(target_p[0]) != request.target_date or _sheet_date(target_m[0]) != request.target_date:
        raise PairMoveConflict("target template date differs")
    if str(target_m[1]).strip() != request.owner_user_id:
        raise PairMoveConflict("target template owner differs")
    if _occupied(target_p, 2, 5) or _occupied(target_m, 3, 4):
        raise PairMoveConflict("target is occupied")
    target_status = str(target_p[13]).strip()
    if target_status in _PRINTED:
        raise PairMoveConflict("target has already been physically printed")
    if target_status not in {"", "待列印"}:
        raise PairMoveConflict("target print status is unknown")
    if not all(str(target_p[i]).strip() for i in (14, 15, 16)):
        raise PairMoveConflict("target dispatch identity is missing")
    if (str(target_p[15]), str(target_p[16])) != (str(source_p[15]), str(source_p[16])):
        raise PairMoveConflict("target order/menu identity differs")

    source_after_p = list(source_p)
    source_after_p[2:10] = ["無", 0, 0, "無", 0, 0, 0, 0]
    source_after_p[10:12] = ["", ""]
    # The real 10:30 reader selects date + status even when both meals are 無.
    source_after_p[13] = ""
    target_after_p = list(target_p)
    target_after_p[2:10] = source_p[2:10]
    target_after_p[10:12] = source_p[10:12]
    target_after_p[13] = "待列印"
    # A newly-created date carries the existing dispatch identity; it must not
    # leave the same identity on two live sheet rows and must never mint a fake
    # publication receipt for the moved date.
    if str(target_after_p[14]).strip() == str(source_after_p[14]).strip():
        source_after_p[14] = ""
    source_after_m = list(source_m)
    source_after_m[3:5] = ["無", "無"]
    target_after_m = list(target_m)
    target_after_m[3:5] = source_m[3:5]

    before_schedule = copy.deepcopy([list(row) for row in schedule_rows])
    before_master = copy.deepcopy([list(row) for row in master_rows])
    after_schedule = copy.deepcopy(before_schedule)
    after_master = copy.deepcopy(before_master)
    after_schedule[spos[request.source_date]] = source_after_p
    after_master[mpos[request.source_date]] = source_after_m
    target_s_index = spos.get(request.target_date)
    target_m_index = mpos.get(request.target_date)
    if target_s_index is None:
        after_schedule.append(target_after_p)
    else:
        after_schedule[target_s_index] = target_after_p
    if target_m_index is None:
        after_master.append(target_after_m)
    else:
        after_master[target_m_index] = target_after_m

    count_before = _meal_count(before_schedule, header_index=schedule_header_index)
    count_after = _meal_count(after_schedule, header_index=schedule_header_index)
    if count_before != count_after:
        raise PairMoveConflict("meal count would change")
    requests = [
        _write_row(worksheet_id, spos[request.source_date], source_after_p),
        _write_row(worksheet_id, target_s_index, target_after_p),
        _write_row(master_worksheet_id, mpos[request.source_date], source_after_m),
        _write_row(master_worksheet_id, target_m_index, target_after_m),
    ]
    return PairMovePlan(
        request, workbook_id, worksheet_id, master_worksheet_id,
        before_schedule, before_master, after_schedule, after_master,
        {"requests": requests}, count_before, count_after,
    )


def apply_and_verify_pair_move(book: Any, schedule_sheet: Any, master_sheet: Any,
                               plan: PairMovePlan, *, now: datetime | None = None) -> VerifiedPairMove:
    """Submit exactly once and report success only after exact two-view readback."""
    if str(getattr(book, "id", "")) != plan.workbook_id:
        raise PairMoveConflict("runtime workbook identity differs")
    if getattr(schedule_sheet, "id", None) != plan.worksheet_id:
        raise PairMoveConflict("runtime personal worksheet identity differs")
    if getattr(master_sheet, "id", None) != plan.master_worksheet_id or getattr(master_sheet, "title", "") != "Master_API_View":
        raise PairMoveConflict("runtime Master_API_View identity differs")
    if now is not None:
        _check_cutoffs(plan.request, now)
    if (schedule_sheet.get_all_values() != plan.before_schedule
            or master_sheet.get_all_values() != plan.before_master):
        raise PairMoveConflict("Sheet changed after planning")
    try:
        book.batch_update(copy.deepcopy(plan.batch_body))
    except Exception as exc:
        raise PairMoveUnknown("external batch outcome is unknown; do not retry") from exc
    if (schedule_sheet.get_all_values() != plan.after_schedule
            or master_sheet.get_all_values() != plan.after_master):
        raise PairMoveUnknown("exact post-write readback differs; do not report success")
    return VerifiedPairMove(plan.request.request_id, True)


def derive_target_rows_from_existing_views(
    request: PendingPairRequest, source_personal: Sequence[Any],
    source_master: Sequence[Any], schedule_rows: Sequence[Sequence[Any]],
    master_rows: Sequence[Sequence[Any]], *, dispatch_row_id: str,
) -> tuple[list[Any], list[Any]]:
    """Derive only a missing counterpart from an existing authoritative target view.

    If both target rows are absent there is no persisted target-date profile metadata;
    guessing week/training/dispatch identity is forbidden.
    """
    schedule_header_index = locate_unique_header_index(schedule_rows, PERSONAL_HEADERS)
    master_header_index = locate_unique_header_index(master_rows, MASTER_HEADERS)
    spos = _positions(
        schedule_rows, width=17, dates={request.target_date},
        header_index=schedule_header_index,
    )
    mpos = _positions(
        master_rows, width=21, dates={request.target_date},
        header_index=master_header_index, owner=request.owner_user_id,
    )
    target_p = list(schedule_rows[spos[request.target_date]]) if request.target_date in spos else None
    target_m = list(master_rows[mpos[request.target_date]]) if request.target_date in mpos else None
    if target_p is None and target_m is None:
        raise PairMoveConflict("target profile/date metadata is absent; blind derivation refused")
    if target_p is None:
        assert target_m is not None
        target_p = list(source_personal)
        target_p[0] = request.target_date
        target_p[1] = target_m[9]
        target_p[2:10] = ["無", 0, 0, "無", 0, 0, 0, 0]
        target_p[10:12] = ["", ""]
        target_p[12] = target_m[5]
        target_p[13] = "待列印"
        target_p[14] = dispatch_row_id
    if target_m is None:
        target_m = list(source_master)
        target_m[0] = request.target_date
        target_m[3:5] = ["無", "無"]
        target_m[5] = target_p[12]
        target_m[9] = target_p[1]
    return target_p, target_m


def derive_target_rows_from_persisted_plan(
    db_path: str | Path, request: PendingPairRequest,
    source_personal: Sequence[Any], source_master: Sequence[Any],
    schedule_rows: Sequence[Sequence[Any]], master_rows: Sequence[Sequence[Any]],
) -> tuple[list[Any], list[Any]]:
    """Build an absent target date from the activated order's service calendar.

    Existing target metadata remains authoritative when either view already has
    the date.  When both are absent, the activated/formalized order snapshot and
    active entitlement establish the plan interval; stable profile fields come
    from the two persisted source rows.  No workout, carb-cycle classification,
    dispatch id, order id, or menu version is invented.
    """
    schedule_header_index = locate_unique_header_index(schedule_rows, PERSONAL_HEADERS)
    master_header_index = locate_unique_header_index(master_rows, MASTER_HEADERS)
    spos = _positions(
        schedule_rows, width=17, dates={request.target_date},
        header_index=schedule_header_index,
    )
    mpos = _positions(
        master_rows, width=21, dates={request.target_date},
        header_index=master_header_index, owner=request.owner_user_id,
    )
    source_p, source_m = list(source_personal), list(source_master)
    if len(source_p) != 17 or len(source_m) != 21:
        raise PairMoveConflict("source profile width differs")
    if str(source_m[1]).strip() != request.owner_user_id:
        raise PairMoveConflict("source profile owner differs")
    if not all(str(source_p[index]).strip() for index in (14, 15, 16)):
        raise PairMoveConflict("source dispatch/order/menu identity is missing")

    try:
        with sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True) as conn:
            records = conn.execute(
                """SELECT o.id,o.customer_name,o.form_payload_json,
                          e.starts_on,e.expires_on
                     FROM subscription_menu_entitlements e
                     JOIN subscription_orders o
                       ON o.id=e.order_id AND o.user_id=e.user_id
                    WHERE e.user_id=? AND e.status='active'
                      AND o.status='activated'
                      AND COALESCE(o.formalized_at,'')<>''""",
                (request.owner_user_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise PairMoveConflict(f"active service calendar unavailable: {exc}") from exc
    if len(records) != 1:
        raise PairMoveConflict("active service calendar is missing or ambiguous")
    order_id, customer_name, payload_json, starts_on, expires_on = records[0]
    if str(source_p[15]).strip() != str(order_id):
        raise PairMoveConflict("source order identity differs from active plan")
    if str(source_p[16]).strip() != f"order-{order_id}-v1":
        raise PairMoveConflict("source menu identity differs from active plan")
    try:
        snapshot = json.loads(str(payload_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PairMoveConflict("activated order snapshot is invalid") from exc
    if not isinstance(snapshot, dict):
        raise PairMoveConflict("activated order snapshot is invalid")
    if str(snapshot.get("user_id") or "").strip() != request.owner_user_id:
        raise PairMoveConflict("activated order snapshot owner differs")
    snapshot_name = str(snapshot.get("name") or "").strip()
    if snapshot_name and str(customer_name or "").strip() != snapshot_name:
        raise PairMoveConflict("activated order customer identity differs")
    try:
        plan_start = date.fromisoformat(str(snapshot.get("start_date") or ""))
        service_start = date.fromisoformat(str(starts_on or ""))
        service_end = date.fromisoformat(str(expires_on or ""))
        source = date.fromisoformat(request.source_date)
        target = date.fromisoformat(request.target_date)
    except ValueError as exc:
        raise PairMoveConflict("active service calendar dates are invalid") from exc
    plan_end = plan_start + timedelta(days=27)
    if not (service_start <= source <= service_end):
        raise PairMoveConflict("source date is outside the active entitlement service calendar")
    if not (plan_start <= source <= plan_end):
        raise PairMoveConflict("source date is outside the activated four-week plan calendar")
    if not (service_start <= target <= service_end):
        raise PairMoveConflict("target date is outside the active entitlement service calendar")
    if not (plan_start <= target <= plan_end):
        raise PairMoveConflict("target date is outside the activated four-week plan calendar")

    expected_tdee = snapshot.get("tdee")
    if expected_tdee is None or str(source_m[2]).strip() != str(expected_tdee).strip():
        raise PairMoveConflict("source profile TDEE differs from activated order snapshot")
    if request.target_date in spos or request.target_date in mpos:
        return derive_target_rows_from_existing_views(
            request, source_p, source_m, schedule_rows, master_rows,
            dispatch_row_id=str(source_p[14]).strip(),
        )
    # The target label is deterministic inside the persisted four-week calendar.
    week_number = ((target - plan_start).days // 7) + 1
    weekday = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")[target.weekday()]

    target_sheet_date = target.strftime("%Y/%m/%d")
    target_p = list(source_p)
    target_p[0] = target_sheet_date
    target_p[1] = f"第{week_number}週-{weekday}"
    target_p[2:10] = ["無", 0, 0, "無", 0, 0, 0, 0]
    target_p[10:14] = ["", "", "", ""]
    # Move the persisted source identity as-is.  The planner clears it from the
    # source row in the same batch, so no duplicate or fabricated receipt exists.
    target_p[14:17] = source_p[14:17]

    target_m = list(source_m)
    target_m[0] = target_sheet_date
    target_m[3:6] = ["無", "無", ""]
    # Monthly meal plans do not imply a workout/carb-cycle assignment.  Preserve
    # stable profile columns, but use the existing unset semantics for this date.
    target_m[9] = ""
    return target_p, target_m


def execute_future_pair_move(
    *, db_path: str | Path, request_id: int, owner_user_id: str, admin_uid: str,
    event_id: str, now_provider: Any, book: Any, schedule_sheet: Any,
    master_sheet: Any, expected_workbook_id: str, expected_worksheet_id: int,
    target_row_deriver: Any,
) -> FuturePairMoveResult:
    """Run the future-only pair move through the existing durable journal."""
    request = load_pending_pair_request(
        db_path, request_id=request_id, owner_user_id=owner_user_id,
        require_pending=False,
    )
    payload = {"d1": request.source_date, "m1": "午餐+晚餐",
               "d2": request.target_date, "m2": "午餐+晚餐"}
    replay = lookup_meal_mutation_for_request(
        db_path, event_id=event_id, owner_user_id=owner_user_id,
        purpose="defer", request_id=str(request_id), payload=payload,
    )
    if replay.kind == "stored_result":
        result = replay.result if isinstance(replay.result, dict) else {}
        return FuturePairMoveResult("replay_completed", str(result.get("message") or ""))
    if replay.kind == "blocked_unresolved":
        return FuturePairMoveResult("outcome_unknown", "延餐結果未確認，請勿自行重試")
    if replay.kind == "identity_conflict":
        raise PairMoveConflict("durable request identity differs")

    # A terminal request is accepted only through the exact durable replay above.
    load_pending_pair_request(
        db_path, request_id=request_id, owner_user_id=owner_user_id
    )

    first_now = now_provider()
    _check_cutoffs(request, first_now)
    schedule_rows = schedule_sheet.get_all_values()
    master_rows = master_sheet.get_all_values()
    schedule_header_index = locate_unique_header_index(schedule_rows, PERSONAL_HEADERS)
    master_header_index = locate_unique_header_index(master_rows, MASTER_HEADERS)
    source_positions = _positions(
        schedule_rows, width=17, dates={request.source_date},
        header_index=schedule_header_index,
    )
    master_positions = _positions(
        master_rows, width=21, dates={request.source_date},
        header_index=master_header_index, owner=owner_user_id,
    )
    if request.source_date not in source_positions or request.source_date not in master_positions:
        raise PairMoveConflict("source before-image is missing")
    derived_personal, derived_master = target_row_deriver(
        request, list(schedule_rows[source_positions[request.source_date]]),
        list(master_rows[master_positions[request.source_date]]),
        schedule_rows, master_rows,
    )
    plan = plan_pair_move(
        request=request, now=first_now,
        workbook_id=str(getattr(book, "id", "")), expected_workbook_id=expected_workbook_id,
        worksheet_id=getattr(schedule_sheet, "id", None), expected_worksheet_id=expected_worksheet_id,
        schedule_rows=schedule_rows, master_rows=master_rows,
        target_personal_template=derived_personal, target_master_template=derived_master,
        master_worksheet_id=getattr(master_sheet, "id", None),
    )
    source_index = source_positions[request.source_date]
    binding = MealMutationBinding(
        operation_id=f"deferred-meal:{request_id}", event_id=event_id,
        owner_user_id=owner_user_id, purpose="defer", request_id=str(request_id),
        payload=payload, spreadsheet_id=expected_workbook_id,
        worksheet_id=expected_worksheet_id, worksheet_name=schedule_sheet.title,
        cells=({"row_idx": source_index + 1, "col_idx": 3},
               {"row_idx": source_index + 1, "col_idx": 6}),
        before=(str(plan.before_schedule[source_index][2]),
                str(plan.before_schedule[source_index][5])),
        after=("無", "無"),
    )
    reservation = reserve_meal_mutation(db_path, binding)
    if reservation.kind == "replay_completed":
        result = reservation.result if isinstance(reservation.result, dict) else {}
        return FuturePairMoveResult("replay_completed", str(result.get("message") or ""))
    if reservation.kind in {"blocked_unresolved", "resource_blocked"}:
        return FuturePairMoveResult("outcome_unknown", "延餐結果未確認，請勿自行重試")
    if reservation.kind != "claimed":
        raise PairMoveConflict(f"durable reservation rejected: {reservation.kind}")
    get_claimed_meal_mutation_snapshot(
        db_path, reservation.operation_id, reservation.claim_token, owner_user_id
    )
    try:
        apply_and_verify_pair_move(
            book, schedule_sheet, master_sheet, plan, now=now_provider()
        )
    except PairMoveConflict as exc:
        mark_meal_mutation_rejected(
            db_path, reservation.operation_id, reservation.claim_token, owner_user_id,
            {"message": str(exc), "outcome": "rejected"},
        )
        raise
    except PairMoveUnknown as exc:
        mark_meal_mutation_unknown(
            db_path, reservation.operation_id, reservation.claim_token,
            owner_user_id, str(exc),
        )
        return FuturePairMoveResult("outcome_unknown", "延餐結果未確認，請勿自行重試",
                                    plan.meal_count_before, plan.meal_count_after)

    message = (f"✅ 已將 {request.source_date}午餐+晚餐 延至 "
               f"{request.target_date}午餐+晚餐")
    completed_at = now_provider()
    summary_line = (f"\n⏸ 系統紀錄：{completed_at.strftime('%m/%d %H:%M')} 將 "
                    f"{request.source_date}午餐+晚餐 延至 {request.target_date}午餐+晚餐。")
    try:
        final = complete_meal_mutation(
            db_path, reservation.operation_id, reservation.claim_token, owner_user_id,
            summary_line=summary_line,
            result={"message": message, "outcome": "completed",
                    "summary_line": summary_line, "approved_by": admin_uid},
            approved_by=admin_uid,
        )
    except Exception:
        return FuturePairMoveResult("outcome_unknown", "延餐結果未確認，請勿自行重試",
                                    plan.meal_count_before, plan.meal_count_after)
    return FuturePairMoveResult(final.kind, message, plan.meal_count_before, plan.meal_count_after)
