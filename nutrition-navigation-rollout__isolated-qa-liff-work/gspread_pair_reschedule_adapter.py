"""Strict single-batch Google Sheets adapter for a two-date reschedule.

The module contains no credentials and performs no network setup.  It plans from
read-only worksheet snapshots, rejects ambiguous/missing before-images, and sends
one spreadsheets.batchUpdate request containing both personal and Master rows.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


MASTER_API_HEADERS = [
    "Date", "User_ID", "TDEE", "Lunch_Item", "Dinner_Item", "Tomorrow_Training",
    "Is_Coaching_Enabled", "Plan_Type", "Sport_Type", "Plan_Week", "Intervals_ID",
    "Intervals_API_Key", "Training_Freq", "Normal_Train_Time", "Long_Train_Day",
    "Run_Pace", "Bike_FTP", "Swim_Pace", "User_Level", "Race_Date",
    "Is_Carb_Cycling_Enabled",
]


class PairSheetConflict(RuntimeError):
    """The two-view Sheet update cannot be proven safe."""


@dataclass(frozen=True)
class PersistedMasterProfile:
    """Explicit order/profile + target-calendar authority for one Master row.

    No date-dependent value is inferred from the source date.  In particular TDEE,
    training/weekday fields and carb-cycle state must be supplied by the persisted
    order/profile projection owned by the caller.
    """
    owner_user_id: str
    tdee: object
    tomorrow_training: object
    is_coaching_enabled: object
    plan_type: object
    sport_type: object
    plan_week: object
    intervals_id: object
    intervals_api_key: object
    training_freq: object
    normal_train_time: object
    long_train_day: object
    run_pace: object
    bike_ftp: object
    swim_pace: object
    user_level: object
    race_date: object
    is_carb_cycling_enabled: object


def build_target_master_row(*, target_date: str, owner_user_id: str,
                            lunch_item: object, dinner_item: object,
                            profile: PersistedMasterProfile) -> list[object]:
    """Pure, fixed-width target row generation from explicit authorities."""
    target = _iso_date(target_date)
    if not isinstance(profile, PersistedMasterProfile):
        raise PairSheetConflict("persisted target profile is required")
    if profile.owner_user_id != owner_user_id or not owner_user_id:
        raise PairSheetConflict("persisted target profile owner differs")
    try:
        if float(str(profile.tdee).strip()) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise PairSheetConflict("persisted target TDEE is invalid") from None
    if str(profile.is_coaching_enabled).strip() not in {"0", "1"}:
        raise PairSheetConflict("persisted coaching state is invalid")
    if not str(profile.plan_week).strip():
        raise PairSheetConflict("target calendar training/weekday metadata is missing")
    if str(profile.is_carb_cycling_enabled).strip() not in {"0", "1"}:
        raise PairSheetConflict("persisted carb-cycle state is invalid")
    if any(str(item).strip() in {"", "無", "尚未安排"} for item in (lunch_item, dinner_item)):
        raise PairSheetConflict("Master_API_View source meals are missing")
    return [
        target, owner_user_id, profile.tdee, lunch_item, dinner_item,
        profile.tomorrow_training, profile.is_coaching_enabled, profile.plan_type,
        profile.sport_type, profile.plan_week, profile.intervals_id,
        profile.intervals_api_key, profile.training_freq, profile.normal_train_time,
        profile.long_train_day, profile.run_pace, profile.bike_ftp, profile.swim_pace,
        profile.user_level, profile.race_date, profile.is_carb_cycling_enabled,
    ]


@dataclass(frozen=True)
class PairSheetPlan:
    workbook_id: str
    schedule_sheet_id: int
    master_sheet_id: int
    source_date: str
    target_date: str
    before_schedule_view: list[list[Any]]
    before_master_view: list[list[Any]]
    after_schedule_view: list[list[Any]]
    after_master_view: list[list[Any]]
    batch_body: dict[str, Any]
    expected_readback: dict[str, dict[str, list[Any]]]


def _iso_date(value: object) -> str:
    raw = str(value or "").strip().replace("/", "-")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise PairSheetConflict("Sheet date is not a full calendar date") from exc
    return parsed.isoformat()


def _sheet_id(worksheet: object) -> int:
    value = getattr(worksheet, "id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PairSheetConflict("worksheet numeric identity is missing")
    return value


def _book_identity(value: object) -> str:
    for name in ("id", "spreadsheet_id", "key"):
        candidate = getattr(value, name, None)
        if candidate:
            return str(candidate)
    return ""


def _cell_value(value: object) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"numberValue": value}
    return {"stringValue": "" if value is None else str(value)}


def _update_request(sheet_id: int, row_index: int, row: Sequence[object]) -> dict[str, Any]:
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_index,
                "endRowIndex": row_index + 1,
                "startColumnIndex": 0,
                "endColumnIndex": len(row),
            },
            "rows": [{"values": [{"userEnteredValue": _cell_value(value)} for value in row]}],
            "fields": "userEnteredValue",
        }
    }


def _master_contract_view(rows, owner):
    """Normalize only proven blank padding; never discard another owner's data."""
    result = copy.deepcopy(rows)
    for index, row in enumerate(rows):
        is_owner = len(row) > 1 and str(row[1]).strip() == owner
        if index != 0 and not is_owner:
            continue
        if len(row) < len(MASTER_API_HEADERS) or any(
            not isinstance(cell, str) or cell != "" for cell in row[len(MASTER_API_HEADERS):]
        ):
            raise PairSheetConflict("Master_API_View header/owner row width differs")
        result[index] = list(row[:len(MASTER_API_HEADERS)])
    return result


class GspreadPairRescheduleAdapter:
    """Plan/apply/read a personal-17 + Master-21 update in one workbook batch."""

    def __init__(self, spreadsheet: object, schedule_worksheet: object,
                 master_worksheet: object, *, workbook_id: str):
        self._spreadsheet = spreadsheet
        self._schedule = schedule_worksheet
        self._master = master_worksheet
        self.workbook_id = str(workbook_id or "")
        if not self.workbook_id or _book_identity(spreadsheet) != self.workbook_id:
            raise PairSheetConflict("spreadsheet identity differs from persisted workbook")
        for worksheet in (schedule_worksheet, master_worksheet):
            parent = getattr(worksheet, "spreadsheet", None)
            if parent is not None and parent is not spreadsheet:
                raise PairSheetConflict("worksheets are not in the same workbook")
            declared = str(getattr(worksheet, "workbook_id", self.workbook_id) or "")
            if declared != self.workbook_id:
                raise PairSheetConflict("worksheet workbook identity differs")
        if str(getattr(master_worksheet, "title", "")) != "Master_API_View":
            raise PairSheetConflict("Master_API_View worksheet identity differs")
        self.schedule_sheet_id = _sheet_id(schedule_worksheet)
        self.master_sheet_id = _sheet_id(master_worksheet)
        if self.schedule_sheet_id == self.master_sheet_id:
            raise PairSheetConflict("personal and Master worksheets must differ")

    @staticmethod
    def _schedule_positions(rows: Sequence[Sequence[object]], days: set[str],
                            *, allow_missing: set[str] | None = None) -> dict[str, int]:
        positions: dict[str, int] = {}
        for index, row in enumerate(rows):
            if len(row) != 17:
                continue
            try:
                day = _iso_date(row[0])
            except PairSheetConflict:
                continue
            if day not in days:
                continue
            if day in positions:
                raise PairSheetConflict("personal sheet has duplicate service date")
            positions[day] = index
        if set(positions) | set(allow_missing or ()) != days:
            raise PairSheetConflict("personal sheet before-image is missing")
        return positions

    @staticmethod
    def _master_positions(rows: Sequence[Sequence[object]], owner: str,
                          days: set[str], *, allow_missing: set[str] | None = None) -> dict[str, int]:
        rows = _master_contract_view(rows, owner)
        if not rows or list(rows[0]) != MASTER_API_HEADERS:
            raise PairSheetConflict("Master_API_View header contract differs")
        positions: dict[str, int] = {}
        for index, row in enumerate(rows[1:], start=1):
            if len(row) != len(MASTER_API_HEADERS):
                if len(row) > 1 and str(row[1]).strip() == owner:
                    raise PairSheetConflict("Master_API_View owner row width differs")
                continue
            if str(row[1]).strip() != owner:
                continue
            try:
                day = _iso_date(row[0])
            except PairSheetConflict as exc:
                raise PairSheetConflict("Master_API_View owner date is invalid") from exc
            if day not in days:
                continue
            if day in positions:
                raise PairSheetConflict("Master_API_View duplicate Date/User_ID")
            positions[day] = index
        if set(positions) | set(allow_missing or ()) != days:
            raise PairSheetConflict("Master_API_View pair before-image is missing")
        return positions

    def plan_pair_reschedule(
        self, *, workbook_id: str, worksheet_id: int, owner_user_id: str,
        source_date: str, target_date: str,
        schedule_rows: Mapping[str, Sequence[object]],
        expected_schedule_before: Mapping[str, Sequence[object] | None] | None = None,
        target_master_profile: PersistedMasterProfile | None = None,
    ) -> PairSheetPlan:
        source, target = _iso_date(source_date), _iso_date(target_date)
        days = {source, target}
        if source == target or not owner_user_id:
            raise PairSheetConflict("pair update binding is incomplete")
        if str(workbook_id) != self.workbook_id or worksheet_id != self.schedule_sheet_id:
            raise PairSheetConflict("persisted personal worksheet binding differs")
        if set(schedule_rows) != days or any(len(list(schedule_rows[d])) != 17 for d in days):
            raise PairSheetConflict("personal successor rows must be complete A:Q rows")

        schedule_before = copy.deepcopy(self._schedule.get_all_values())
        master_before = copy.deepcopy(self._master.get_all_values())
        schedule_positions = self._schedule_positions(schedule_before, days, allow_missing={target})
        master_positions = self._master_positions(
            master_before, owner_user_id, days, allow_missing={target},
        )
        if expected_schedule_before is not None:
            if set(expected_schedule_before) != days or any(
                (expected_schedule_before[day] is None) != (day not in schedule_positions)
                or (day in schedule_positions and
                    list(expected_schedule_before[day] or []) != list(schedule_before[schedule_positions[day]]))
                for day in days
            ):
                raise PairSheetConflict("personal sheet differs from expected before-image")

        source_master = list(master_before[master_positions[source]][:21])
        if any(str(value).strip() in {"", "無", "尚未安排"} for value in source_master[3:5]):
            raise PairSheetConflict("Master_API_View source meals are missing")
        if target in master_positions:
            target_master = list(master_before[master_positions[target]][:21])
            if any(str(value).strip() not in ("", "無") for value in target_master[3:5]):
                raise PairSheetConflict("Master_API_View target meals are occupied")
            target_after_master = list(target_master)
            target_after_master[3:5] = source_master[3:5]
        else:
            if target_master_profile is None:
                raise PairSheetConflict("persisted target profile is required for missing Master row")
            target_after_master = build_target_master_row(
                target_date=target, owner_user_id=owner_user_id,
                lunch_item=source_master[3], dinner_item=source_master[4],
                profile=target_master_profile,
            )
        source_after_master = list(source_master)
        source_after_master[3:5] = ["無", "無"]

        schedule_after = copy.deepcopy(schedule_before)
        master_after = copy.deepcopy(master_before)
        schedule_after[schedule_positions[source]] = list(schedule_rows[source])
        if target in schedule_positions:
            schedule_after[schedule_positions[target]] = list(schedule_rows[target])
        else:
            schedule_after.append(list(schedule_rows[target]))
        master_after[master_positions[source]] = source_after_master + list(master_before[master_positions[source]][21:])
        if target in master_positions:
            master_after[master_positions[target]] = target_after_master + list(master_before[master_positions[target]][21:])
        else:
            master_after.append(target_after_master)
        requests = [_update_request(
            self.schedule_sheet_id, schedule_positions[source], schedule_rows[source]
        )]
        if target in schedule_positions:
            requests.append(_update_request(
                self.schedule_sheet_id, schedule_positions[target], schedule_rows[target]
            ))
        else:
            requests.append({"appendCells": {
                "sheetId": self.schedule_sheet_id,
                "rows": [{"values": [
                    {"userEnteredValue": _cell_value(value)} for value in schedule_rows[target]
                ]}],
                "fields": "userEnteredValue",
            }})
        requests.append(_update_request(
            self.master_sheet_id, master_positions[source], source_after_master,
        ))
        if target in master_positions:
            requests.append(_update_request(
                self.master_sheet_id, master_positions[target], target_after_master,
            ))
        else:
            requests.append({"appendCells": {
                "sheetId": self.master_sheet_id,
                "rows": [{"values": [
                    {"userEnteredValue": _cell_value(value)} for value in target_after_master
                ]}],
                "fields": "userEnteredValue",
            }})
        return PairSheetPlan(
            workbook_id=self.workbook_id,
            schedule_sheet_id=self.schedule_sheet_id,
            master_sheet_id=self.master_sheet_id,
            source_date=source,
            target_date=target,
            before_schedule_view=schedule_before,
            before_master_view=master_before,
            after_schedule_view=schedule_after,
            after_master_view=master_after,
            batch_body={"requests": requests},
            expected_readback={
                "schedule": {day: list(schedule_rows[day]) for day in (source, target)},
                "master": {
                    source: source_after_master,
                    target: target_after_master,
                },
            },
        )

    def read_rows(self, service_dates: Sequence[str]) -> dict[str, list[Any] | None]:
        """Read exact personal rows for coordinator preflight; missing dates stay None."""
        days = {_iso_date(value) for value in service_dates}
        if len(days) != len(service_dates):
            raise PairSheetConflict("personal preflight dates are duplicated")
        rows = self._schedule.get_all_values()
        positions = self._schedule_positions(rows, days, allow_missing=days)
        return {
            day: list(rows[positions[day]]) if day in positions else None
            for day in days
        }

    def apply_pair_reschedule(self, plan: PairSheetPlan) -> None:
        if not isinstance(plan, PairSheetPlan) or plan.workbook_id != self.workbook_id:
            raise PairSheetConflict("pair Sheet plan binding differs")
        if (self._schedule.get_all_values() != plan.before_schedule_view
                or self._master.get_all_values() != plan.before_master_view):
            raise PairSheetConflict("Sheet changed after planning")
        self._spreadsheet.batch_update(copy.deepcopy(plan.batch_body))

    def read_pair_reschedule(self, plan: PairSheetPlan) -> dict[str, dict[str, list[Any]]]:
        schedule = self._schedule.get_all_values()
        master = self._master.get_all_values()
        owner = str(plan.expected_readback["master"][plan.source_date][1])
        if schedule != plan.after_schedule_view or _master_contract_view(master, owner) != _master_contract_view(plan.after_master_view, owner):
            raise PairSheetConflict("personal/Master exact readback differs")
        days = {plan.source_date, plan.target_date}
        schedule_positions = self._schedule_positions(schedule, days)
        owner = str(plan.expected_readback["master"][plan.source_date][1])
        master_positions = self._master_positions(master, owner, days)
        return {
            "schedule": {day: list(schedule[schedule_positions[day]]) for day in (plan.source_date, plan.target_date)},
            "master": {day: list(master[master_positions[day]][:21]) for day in (plan.source_date, plan.target_date)},
        }

    def read_pair_rows(self, *, owner_user_id: str, source_date: str,
                       target_date: str) -> dict[str, dict[str, list[Any]]]:
        """Read only the two exact view pairs; used for unknown-outcome reconcile."""
        source, target = _iso_date(source_date), _iso_date(target_date)
        days = {source, target}
        schedule = self._schedule.get_all_values()
        master = self._master.get_all_values()
        schedule_positions = self._schedule_positions(schedule, days)
        master_positions = self._master_positions(master, owner_user_id, days)
        return {
            "schedule": {day: list(schedule[schedule_positions[day]]) for day in (source, target)},
            "master": {day: list(master[master_positions[day]][:21]) for day in (source, target)},
        }
