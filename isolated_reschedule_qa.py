"""Opt-in staging-only isolated QA surface for customer pair reschedules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import anyio
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from customer_health_check_liff import (
    LineAuthenticationError,
    LineAuthenticationUnavailable,
    NO_STORE_HEADERS,
    verify_line_id_token,
)
from customer_reschedule_liff_routes import create_customer_reschedule_router
from pair_reschedule_coordinator import (
    PairRescheduleConflict,
    reconcile_pair_reschedule_readback,
    verify_admin_context,
)
from reschedule_dispatch_versions import exportable_versions
from reschedule_service_integration import (
    RescheduleAuthorizationError,
    RescheduleFeatureUnavailable,
    RescheduleRequestConflict,
    approve_customer_pair_reschedule,
)


ALLOWLISTED_WORKBOOK_ID = "10lktGaFncSi-AEzAs0egh_od7yN1Lt7nKkl_uM7zeTM"
DEFAULT_DB_PATH = "/app/data/isolated-reschedule-qa/customer-uat.sqlite3"
DEFAULT_MANIFEST_PATH = "/app/data/isolated-reschedule-qa/customer-uat.json"
_HTML_PATH = Path(__file__).with_name("customer-reschedule-qa-liff.html")


@dataclass(frozen=True)
class IsolatedRescheduleFixture:
    db_path: str
    manifest_path: str
    workbook_id: str
    personal_worksheet_id: int
    personal_worksheet_title: str
    master_worksheet_id: int
    master_worksheet_title: str
    order_id: int
    owner_user_id: str
    admin_user_id: str
    original_expiry_date: str


def isolated_reschedule_qa_enabled(environ: Mapping[str, str]) -> bool:
    value = str(environ.get("ISOLATED_RESCHEDULE_QA_ENABLED") or "").strip().lower()
    if value == "":
        return False
    if value in {"true", "false"}:
        return value == "true"
    raise ValueError("ISOLATED_RESCHEDULE_QA_ENABLED must be exactly true or false")


def _uid(value: object, key: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"U[0-9A-Za-z]{32}", text):
        raise RuntimeError(f"isolated manifest {key} is invalid")
    return text


def _int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"isolated manifest {key} is invalid")
    return value


def _iso_date(value: object, key: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"isolated manifest {key} is invalid") from exc
    if parsed.isoformat() != text:
        raise RuntimeError(f"isolated manifest {key} is invalid")
    return text


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _fixture_from_manifest(
    *,
    db_path: str,
    manifest_path: str,
    main_db_path: str | None,
) -> IsolatedRescheduleFixture:
    db = Path(db_path).resolve()
    manifest = Path(manifest_path).resolve()
    if main_db_path and _same_path(db, main_db_path):
        raise RuntimeError("isolated QA database must not collide with main DB")
    if not db.exists() or not db.is_file():
        raise RuntimeError("isolated QA database is missing")
    if not manifest.exists() or not manifest.is_file():
        raise RuntimeError("isolated QA manifest is missing")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("isolated QA manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("isolated QA manifest is invalid")
    manifest_db = str(payload.get("db_path") or "").strip()
    if not manifest_db or Path(manifest_db).resolve() != db:
        raise RuntimeError("isolated QA manifest database binding mismatch")
    workbook_id = str(payload.get("workbook_id") or "").strip()
    if workbook_id != ALLOWLISTED_WORKBOOK_ID:
        raise RuntimeError("isolated QA workbook is not allowlisted")
    master_title = str(payload.get("master_worksheet_title") or "").strip()
    if master_title != "Master_API_View":
        raise RuntimeError("isolated QA Master worksheet binding mismatch")
    fixture = IsolatedRescheduleFixture(
        db_path=str(db),
        manifest_path=str(manifest),
        workbook_id=workbook_id,
        personal_worksheet_id=_int(payload.get("personal_worksheet_id"), "personal_worksheet_id"),
        personal_worksheet_title=str(payload.get("personal_worksheet_title") or "").strip(),
        master_worksheet_id=_int(payload.get("master_worksheet_id"), "master_worksheet_id"),
        master_worksheet_title=master_title,
        order_id=_int(payload.get("order_id"), "order_id"),
        owner_user_id=_uid(payload.get("owner_user_id"), "owner_user_id"),
        admin_user_id=_uid(payload.get("admin_user_id"), "admin_user_id"),
        original_expiry_date=_iso_date(
            payload.get("original_expiry_date"),
            "original_expiry_date",
        ),
    )
    if not fixture.personal_worksheet_title:
        raise RuntimeError("isolated manifest personal_worksheet_title is invalid")
    return fixture


def _validate_sqlite_bindings(fixture: IsolatedRescheduleFixture) -> None:
    database_uri = Path(fixture.db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        admin = conn.execute(
            "SELECT value,typeof(value) FROM admin_settings WHERE key='admin_id'"
        ).fetchall()
        if len(admin) != 1 or tuple(admin[0]) != (fixture.admin_user_id, "text"):
            raise RuntimeError("isolated QA admin binding mismatch")
        order = conn.execute(
            "SELECT user_id,status FROM subscription_orders WHERE id=?",
            (fixture.order_id,),
        ).fetchone()
        if not order or tuple(order) != (fixture.owner_user_id, "activated"):
            raise RuntimeError("isolated QA owner/order binding mismatch")
        usage = conn.execute(
            "SELECT expiry_date,status FROM usage WHERE user_id=?",
            (fixture.owner_user_id,),
        ).fetchone()
        if (
            not usage
            or tuple(usage) != (fixture.original_expiry_date, "active")
        ):
            raise RuntimeError("isolated QA original expiry anchor mismatch")
        rows = conn.execute(
            """SELECT DISTINCT workbook_id,worksheet_id,worksheet_title
                 FROM subscription_dispatch_rows
                WHERE order_id=? AND customer_uid=? AND publish_state='published'""",
            (fixture.order_id, fixture.owner_user_id),
        ).fetchall()
        if not rows or any(
            str(row["workbook_id"]) != fixture.workbook_id
            or int(row["worksheet_id"]) != fixture.personal_worksheet_id
            or str(row["worksheet_title"]) != fixture.personal_worksheet_title
            for row in rows
        ):
            raise RuntimeError("isolated QA publication binding mismatch")


def load_isolated_reschedule_fixture(
    *,
    db_path: str = DEFAULT_DB_PATH,
    manifest_path: str = DEFAULT_MANIFEST_PATH,
    main_db_path: str | None = None,
) -> IsolatedRescheduleFixture:
    fixture = _fixture_from_manifest(
        db_path=db_path,
        manifest_path=manifest_path,
        main_db_path=main_db_path,
    )
    _validate_sqlite_bindings(fixture)
    return fixture


def _authenticate(
    authorization: str | None,
    *,
    channel_id: str,
    token_verifier: Callable[..., str],
) -> tuple[JSONResponse | None, str | None]:
    scheme, separator, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return JSONResponse({"detail": "需要 LINE 登入"}, status_code=401, headers=NO_STORE_HEADERS), None
    try:
        return None, token_verifier(token.strip(), channel_id=channel_id)
    except LineAuthenticationError:
        return JSONResponse({"detail": "LINE 登入無效"}, status_code=401, headers=NO_STORE_HEADERS), None
    except (LineAuthenticationUnavailable, Exception):
        return JSONResponse(
            {"detail": "LINE 驗證服務暫時無法使用"},
            status_code=503,
            headers=NO_STORE_HEADERS,
        ), None


async def _read_small_json(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    content_length = request.headers.get("content-length", "")
    if content_length and (not content_length.isdigit() or int(content_length) > 1024):
        return None, JSONResponse({"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
    body = await request.body()
    if len(body) > 1024:
        return None, JSONResponse({"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
    if not body:
        return {}, None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, JSONResponse({"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
    if not isinstance(payload, dict):
        return None, JSONResponse({"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
    if "actor_id" in payload:
        return None, JSONResponse({"detail": "actor_id is not accepted"}, status_code=422, headers=NO_STORE_HEADERS)
    return payload, None


def _runtime_adapter_factory(_conn: sqlite3.Connection, _order_id: int, fixture: IsolatedRescheduleFixture):
    import os

    import gspread
    from google.oauth2.service_account import Credentials
    from gspread_pair_reschedule_adapter import GspreadPairRescheduleAdapter

    raw_credentials = str(os.environ.get("GOOGLE_CREDENTIALS") or "").strip()
    if not raw_credentials:
        raise RuntimeError("Google service account credentials are unavailable")
    try:
        credentials_info = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Google service account credentials are invalid") from exc
    if not isinstance(credentials_info, dict):
        raise RuntimeError("Google service account credentials are invalid")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
    client = gspread.authorize(credentials)
    client.set_timeout(15)
    book = client.open_by_key(fixture.workbook_id)
    schedule = book.get_worksheet_by_id(fixture.personal_worksheet_id)
    master = book.get_worksheet_by_id(fixture.master_worksheet_id)
    if schedule is None or str(getattr(schedule, "title", "")) != fixture.personal_worksheet_title:
        raise RuntimeError("isolated QA personal worksheet binding mismatch")
    if master is None or str(getattr(master, "title", "")) != fixture.master_worksheet_title:
        raise RuntimeError("isolated QA Master worksheet binding mismatch")
    return GspreadPairRescheduleAdapter(
        book,
        schedule,
        master,
        workbook_id=fixture.workbook_id,
    )


def _result_response(result: Any) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "operation_id": result.operation_id,
            "request_id": result.request_id,
            "old_version_id": result.old_version_id,
            "new_version_id": result.new_version_id,
            "status": result.status,
            "expected_payload_hash": result.expected_payload_hash,
        },
        headers=NO_STORE_HEADERS,
    )


def _has_meal(columns: list[Any]) -> bool:
    return any(str(columns[index] or "").strip() not in ("", "無") for index in (2, 5))


def _meal_items(columns: list[Any]) -> list[dict[str, str]]:
    meals = []
    lunch = str(columns[2] if len(columns) > 2 else "").strip()
    dinner = str(columns[5] if len(columns) > 5 else "").strip()
    if lunch and lunch != "無":
        meals.append({"meal": "午餐", "label": lunch})
    if dinner and dinner != "無":
        meals.append({"meal": "晚餐", "label": dinner})
    return meals


def _current_version_rows(
    conn: sqlite3.Connection,
    *,
    fixture: IsolatedRescheduleFixture,
) -> dict[str, list[Any]]:
    rows = exportable_versions(conn, order_id=fixture.order_id)
    if len(rows) != 1:
        return {}
    try:
        payload = json.loads(str(rows[0]["payload_json"]))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("owner_user_id") or "") != fixture.owner_user_id:
        return {}
    if payload.get("workbook_id") is not None and str(payload.get("workbook_id") or "") != fixture.workbook_id:
        return {}
    if payload.get("worksheet_id") is not None and int(payload.get("worksheet_id") or 0) != fixture.personal_worksheet_id:
        return {}
    version_rows = payload.get("rows")
    if not isinstance(version_rows, list):
        return {}
    result: dict[str, list[Any]] = {}
    for item in version_rows:
        if not isinstance(item, dict):
            return {}
        day = str(item.get("service_date") or "").strip()
        columns = item.get("columns")
        if columns is None:
            columns = item.get("source_columns")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not isinstance(columns, list):
            return {}
        if len(columns) < 14:
            return {}
        result[day] = list(columns)
    return result


def _current_occupied_dates(
    conn: sqlite3.Connection,
    *,
    fixture: IsolatedRescheduleFixture,
) -> set[str]:
    return {
        day for day, columns in _current_version_rows(conn, fixture=fixture).items()
        if _has_meal(columns)
    }


def _allowed_calendar_targets(
    conn: sqlite3.Connection,
    *,
    fixture: IsolatedRescheduleFixture,
) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT service_date,schedule_label
             FROM subscription_service_calendar
            WHERE order_id=? AND is_service_day=1
            ORDER BY service_date""",
        (fixture.order_id,),
    ))


def _fixed_deadline_dates(fixture: IsolatedRescheduleFixture) -> set[str]:
    original = date.fromisoformat(fixture.original_expiry_date)
    return {
        (original + timedelta(days=offset)).isoformat()
        for offset in range(31)
    }


def _service_labels(conn: sqlite3.Connection, *, fixture: IsolatedRescheduleFixture) -> dict[str, str]:
    return {
        str(row["service_date"]): str(row["schedule_label"] or "")
        for row in _allowed_calendar_targets(conn, fixture=fixture)
    }


def _active_usage_is_original_anchor(conn: sqlite3.Connection, *, fixture: IsolatedRescheduleFixture) -> bool:
    row = conn.execute(
        """SELECT remaining_meals,expiry_date,status,typeof(remaining_meals)
             FROM usage WHERE user_id=?""",
        (fixture.owner_user_id,),
    ).fetchone()
    return bool(
        row
        and row[3] == "integer"
        and not isinstance(row[0], bool)
        and int(row[0]) > 0
        and str(row[1]) == fixture.original_expiry_date
        and str(row[2]) == "active"
    )


def _fixed_deadline_policy_labels(
    conn: sqlite3.Connection,
    *,
    fixture: IsolatedRescheduleFixture,
    source_date: str,
    target_date: str,
    current_rows: Mapping[str, list[Any]] | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    if not _active_usage_is_original_anchor(conn, fixture=fixture):
        raise PairRescheduleConflict("fixed original expiry anchor is invalid")
    source = date.fromisoformat(source_date).isoformat()
    target = date.fromisoformat(target_date).isoformat()
    if target not in _fixed_deadline_dates(fixture):
        raise PairRescheduleConflict("target date is outside fixed original expiry window")
    rows = dict(current_rows) if current_rows is not None else _current_version_rows(conn, fixture=fixture)
    source_columns = rows.get(source)
    if not source_columns or not _has_meal(source_columns):
        raise PairRescheduleConflict("source date is not the current confirmed meal day")
    if any(day == target and _has_meal(columns) for day, columns in rows.items()):
        raise PairRescheduleConflict("target date already has a confirmed meal plan")
    labels = _service_labels(conn, fixture=fixture)
    if source not in labels or target not in labels:
        raise PairRescheduleConflict("missing service calendar configuration")
    local_now = now
    if local_now is not None:
        today = local_now.date().isoformat()
        for service_date in (source, target):
            if service_date < today:
                raise PairRescheduleConflict("past service date cannot be rescheduled")
            if service_date == today and local_now.timetz().replace(tzinfo=None) >= datetime.strptime("08:00", "%H:%M").time():
                raise PairRescheduleConflict("same-day pair reschedule cutoff has passed")
    return labels[source], labels[target]


def _eligible_target_labels(
    conn: sqlite3.Connection,
    *,
    fixture: IsolatedRescheduleFixture,
    usage: sqlite3.Row,
    current_rows: Mapping[str, list[Any]],
) -> dict[str, str]:
    if not _active_usage_is_original_anchor(conn, fixture=fixture):
        return {}
    fixed_window = _fixed_deadline_dates(fixture)
    occupied = {
        day for day, columns in current_rows.items()
        if _has_meal(columns)
    }
    return {
        str(row["service_date"]): str(row["schedule_label"] or "")
        for row in _allowed_calendar_targets(conn, fixture=fixture)
        if (
            str(row["service_date"]) in fixed_window
            and str(row["service_date"]) not in occupied
        )
    }


def _find_actionable_semantic_request(
    conn: sqlite3.Connection,
    order_id: int,
    owner_user_id: str,
    source_date: str,
    target_date: str,
    now: datetime,
) -> dict[str, object] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_pair_reschedule_requests'"
    ).fetchone()
    if exists is None:
        return None
    rows = conn.execute(
        """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                  operation_id,created_at,expires_at,admin_notification_status,
                  admin_notification_last_error,admin_notification_attempted_at
             FROM customer_pair_reschedule_requests
            WHERE order_id=? AND owner_user_id=? AND source_date=? AND target_date=?
              AND status IN ('pending_admin','sheet_unknown')
            ORDER BY created_at ASC, request_id ASC
            LIMIT 100""",
        (order_id, owner_user_id, source_date, target_date),
    ).fetchall()
    local_now = _aware_datetime(now)
    for row in rows:
        status = str(row["status"])
        if status == "pending_admin" and not _expires_at_is_actionable(
            str(row["expires_at"]),
            local_now,
        ):
            continue
        return {
            "request_id": row["request_id"],
            "order_id": row["order_id"],
            "owner_user_id": row["owner_user_id"],
            "source_date": row["source_date"],
            "target_date": row["target_date"],
            "status": row["status"],
            "operation_id": row["operation_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "admin_notification_status": row["admin_notification_status"],
        }
    return None


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    return value


def _parse_aware_expiry(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _expires_at_is_actionable(expires_at: str, now: datetime) -> bool:
    parsed = _parse_aware_expiry(expires_at)
    return bool(parsed is not None and _aware_datetime(now) <= parsed)


def _admin_request_projection(row: sqlite3.Row, now: datetime) -> dict[str, object]:
    status = str(row["status"])
    is_pending = status == "pending_admin"
    is_sheet_unknown = status == "sheet_unknown"
    expiry_valid = True
    expired = False
    disabled_reason = ""
    if is_pending:
        parsed = _parse_aware_expiry(str(row["expires_at"]))
        expiry_valid = parsed is not None
        expired = bool(parsed is not None and _aware_datetime(now) > parsed)
        if not expiry_valid:
            disabled_reason = "申請期限格式異常，請重新送出申請。"
        elif expired:
            disabled_reason = "申請已逾期，請顧客重新送出申請。"
    can_approve = bool(is_pending and expiry_valid and not expired)
    return {
        "request_id": str(row["request_id"]),
        "order_id": int(row["order_id"]),
        "source_date": str(row["source_date"]),
        "target_date": str(row["target_date"]),
        "status": status,
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]),
        "admin_notification_status": str(row["admin_notification_status"] or ""),
        "admin_notification_last_error": str(row["admin_notification_last_error"] or ""),
        "admin_notification_attempted_at": str(row["admin_notification_attempted_at"] or ""),
        "can_approve": can_approve,
        "can_reconcile": is_sheet_unknown,
        "disabled_reason": disabled_reason,
        "operation_id": str(row["operation_id"] or ""),
    }


def _target_is_eligible_for_submit(
    conn: sqlite3.Connection,
    *,
    fixture: IsolatedRescheduleFixture,
    source_date: str,
    target_date: str,
) -> bool:
    usage = conn.execute(
        "SELECT last_date,expiry_date FROM usage WHERE user_id=? AND status IN ('active','vip')",
        (fixture.owner_user_id,),
    ).fetchone()
    if usage is None:
        return False
    current_rows = _current_version_rows(conn, fixture=fixture)
    source_columns = current_rows.get(source_date)
    if not source_columns or not _has_meal(source_columns):
        return False
    try:
        _fixed_deadline_policy_labels(
            conn,
            fixture=fixture,
            source_date=source_date,
            target_date=target_date,
            current_rows=current_rows,
        )
    except (PairRescheduleConflict, ValueError):
        return False
    return True


def create_isolated_reschedule_qa_router(
    *,
    liff_id: str,
    channel_id: str,
    app_env: str,
    db_path: str = DEFAULT_DB_PATH,
    manifest_path: str = DEFAULT_MANIFEST_PATH,
    main_db_path: str | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
    now_factory: Callable[[], datetime] | None = None,
    adapter_factory: Callable[[sqlite3.Connection, int, IsolatedRescheduleFixture], object] | None = None,
) -> APIRouter:
    if str(app_env or "").strip().lower() != "staging":
        raise RuntimeError("isolated reschedule QA routes are staging-only")
    fixture = load_isolated_reschedule_fixture(
        db_path=db_path,
        manifest_path=manifest_path,
        main_db_path=main_db_path,
    )
    _now = now_factory or (lambda: datetime.now(ZoneInfo("Asia/Taipei")))
    _adapter_factory = adapter_factory or _runtime_adapter_factory

    router = APIRouter()

    def _admin(
        authorization: str | None,
    ) -> tuple[JSONResponse | None, str | None]:
        denied, user_id = _authenticate(
            authorization,
            channel_id=channel_id,
            token_verifier=token_verifier,
        )
        if denied is not None:
            return denied, None
        if user_id != fixture.admin_user_id:
            return JSONResponse({"detail": "forbidden"}, status_code=403, headers=NO_STORE_HEADERS), None
        return None, str(user_id)

    def _sheet(conn: sqlite3.Connection) -> object:
        _validate_sqlite_bindings(fixture)
        return _adapter_factory(conn, fixture.order_id, fixture)

    @router.get("/customer-reschedule/context", response_class=JSONResponse)
    def isolated_context(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        denied, user_id = _authenticate(
            authorization,
            channel_id=channel_id,
            token_verifier=token_verifier,
        )
        if denied is not None:
            return denied
        if user_id != fixture.owner_user_id:
            return JSONResponse({"detail": "forbidden"}, status_code=403, headers=NO_STORE_HEADERS)
        try:
            database_uri = Path(fixture.db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                usage = conn.execute(
                    "SELECT last_date,expiry_date FROM usage WHERE user_id=? AND status IN ('active','vip')",
                    (fixture.owner_user_id,),
                ).fetchone()
                if usage is None:
                    return JSONResponse({"orders": []}, headers=NO_STORE_HEADERS)
                current_rows = _current_version_rows(conn, fixture=fixture)
                if not current_rows:
                    return JSONResponse(
                        {"orders": [], "detail": "目前有待確認或無法讀取的改期狀態，請先到管理 QA 檢查。"},
                        headers=NO_STORE_HEADERS,
                    )
                local_now = _now()
                earliest_source = local_now.date()
                if local_now.time() >= datetime.strptime("08:00", "%H:%M").time():
                    from datetime import timedelta
                    earliest_source += timedelta(days=1)
                calendar_rows = _allowed_calendar_targets(
                    conn,
                    fixture=fixture,
                )
                labels = {str(row["service_date"]): str(row["schedule_label"] or "") for row in calendar_rows}
                target_labels = _eligible_target_labels(
                    conn,
                    fixture=fixture,
                    usage=usage,
                    current_rows=current_rows,
                )
                source_dates = [
                    {
                        "date": day,
                        "label": labels.get(day) or str(columns[1] if len(columns) > 1 else ""),
                        "meals": _meal_items(columns),
                    }
                    for day, columns in sorted(current_rows.items())
                    if day >= earliest_source.isoformat() and day in labels and _has_meal(columns)
                ]
                target_dates = sorted(target_labels)
                return JSONResponse(
                    {
                        "orders": [{
                            "order_id": fixture.order_id,
                            "source_dates": source_dates,
                            "target_dates": target_dates,
                        }]
                    },
                    headers=NO_STORE_HEADERS,
                )
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return JSONResponse({"detail": "改期選項暫時無法載入"}, status_code=503, headers=NO_STORE_HEADERS)

    @router.get("/customer-reschedule/preview", response_class=JSONResponse)
    def isolated_preview(
        authorization: str | None = Header(default=None),
        order_id: str | None = None,
        source_date: str | None = None,
        target_date: str | None = None,
    ) -> JSONResponse:
        denied, user_id = _authenticate(
            authorization,
            channel_id=channel_id,
            token_verifier=token_verifier,
        )
        if denied is not None:
            return denied
        if user_id != fixture.owner_user_id:
            return JSONResponse({"detail": "forbidden"}, status_code=403, headers=NO_STORE_HEADERS)
        try:
            oid = int(order_id) if order_id is not None else 0
        except (TypeError, ValueError):
            return JSONResponse({"detail": "order_id 格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
        src = str(source_date or "").strip()
        tgt = str(target_date or "").strip()
        if oid != fixture.order_id or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", src) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tgt):
            return JSONResponse({"detail": "日期格式必須是 YYYY-MM-DD"}, status_code=422, headers=NO_STORE_HEADERS)
        try:
            database_uri = Path(fixture.db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                usage = conn.execute(
                    "SELECT last_date,expiry_date FROM usage WHERE user_id=?",
                    (fixture.owner_user_id,),
                ).fetchone()
                if usage is None:
                    return JSONResponse({"detail": "找不到使用權益資料"}, status_code=404, headers=NO_STORE_HEADERS)
                current_rows = _current_version_rows(conn, fixture=fixture)
                source_columns = current_rows.get(src)
                if not source_columns or not _has_meal(source_columns):
                    return JSONResponse({"detail": "來源日期不是可改期的排餐日"}, status_code=422, headers=NO_STORE_HEADERS)
                allowed = _eligible_target_labels(
                    conn,
                    fixture=fixture,
                    usage=usage,
                    current_rows=current_rows,
                )
                if tgt not in allowed:
                    return JSONResponse({"detail": "目標日期已有排餐或不在可改期範圍"}, status_code=422, headers=NO_STORE_HEADERS)
                return JSONResponse(
                    {
                        "ok": True,
                        "source_date": src,
                        "target_date": tgt,
                        "source_label": str(source_columns[1] if len(source_columns) > 1 else ""),
                        "target_label": allowed[tgt],
                        "source_meals": _meal_items(source_columns),
                        "status": "preview_only",
                    },
                    headers=NO_STORE_HEADERS,
                )
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return JSONResponse({"detail": "改期預覽暫時無法載入"}, status_code=503, headers=NO_STORE_HEADERS)

    @router.get("/api/admin/customer-pair-reschedule-requests", response_class=JSONResponse)
    def isolated_admin_requests(
        authorization: str | None = Header(default=None),
        status: str = "actionable",
        limit: str = "50",
    ) -> JSONResponse:
        denied, _admin_id = _admin(authorization)
        if denied is not None:
            return denied
        if len(str(limit)) > 3 or not re.fullmatch(r"[0-9]+", str(limit)):
            return JSONResponse({"detail": "limit 格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
        parsed_limit = int(limit)
        if not 1 <= parsed_limit <= 100:
            return JSONResponse({"detail": "limit 格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
        allowed_statuses = {
            "actionable": ("pending_admin", "sheet_unknown"),
            "pending_admin": ("pending_admin",),
            "sheet_unknown": ("sheet_unknown",),
            "confirmed": ("confirmed",),
            "all": ("pending_admin", "sheet_unknown", "confirmed"),
        }
        if status not in allowed_statuses:
            return JSONResponse({"detail": "status 格式無效"}, status_code=422, headers=NO_STORE_HEADERS)
        placeholders = ",".join("?" for _ in allowed_statuses[status])
        try:
            database_uri = Path(fixture.db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""SELECT request_id,order_id,source_date,target_date,status,created_at,
                              expires_at,admin_notification_status,
                              admin_notification_last_error,admin_notification_attempted_at,
                              operation_id
                         FROM customer_pair_reschedule_requests
                        WHERE order_id=? AND status IN ({placeholders})
                        ORDER BY created_at DESC, request_id DESC
                        LIMIT ?""",
                    (fixture.order_id, *allowed_statuses[status], parsed_limit),
                ).fetchall()
                local_now = _aware_datetime(_now())
                projected = [
                    _admin_request_projection(row, local_now)
                    for row in rows
                ]
                if status == "actionable":
                    projected = [
                        item for item in projected
                        if item["can_approve"] or item["can_reconcile"]
                    ]
                return JSONResponse(
                    {
                        "requests": projected
                    },
                    headers=NO_STORE_HEADERS,
                )
        except sqlite3.Error:
            return JSONResponse({"detail": "改期申請讀取暫時無法使用"}, status_code=503, headers=NO_STORE_HEADERS)

    @router.post(
        "/api/admin/customer-pair-reschedule-requests/{request_id}/approve",
        response_class=JSONResponse,
    )
    async def approve(
        request_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        denied, admin_id = await anyio.to_thread.run_sync(lambda: _admin(authorization))
        if denied is not None:
            return denied
        _payload, bad = await _read_small_json(request)
        if bad is not None:
            return bad

        def _approve_worker() -> JSONResponse:
            try:
                with sqlite3.connect(fixture.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys=ON")
                    admin_context = verify_admin_context(conn, str(admin_id))
                    result = approve_customer_pair_reschedule(
                        conn,
                        None,
                        request_id=request_id,
                        admin_context=admin_context,
                        now=_now(),
                        feature_enabled=True,
                        sheet_factory=lambda inner_conn, _order_id: _sheet(inner_conn),
                        policy_validator=lambda inner_conn, order_id, owner, source, target, local_now: (
                            _fixed_deadline_policy_labels(
                                inner_conn,
                                fixture=fixture,
                                source_date=source,
                                target_date=target,
                                now=local_now,
                            )
                        ),
                    )
                    return _result_response(result)
            except RescheduleAuthorizationError:
                return JSONResponse({"detail": "forbidden"}, status_code=403, headers=NO_STORE_HEADERS)
            except (RescheduleFeatureUnavailable, RescheduleRequestConflict, PairRescheduleConflict) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=409, headers=NO_STORE_HEADERS)
            except Exception as exc:
                return JSONResponse(
                    {"detail": f"改期核准暫時無法處理: {type(exc).__name__}"},
                    status_code=503,
                    headers=NO_STORE_HEADERS,
                )

        return await anyio.to_thread.run_sync(_approve_worker)

    @router.post(
        "/api/admin/customer-pair-reschedule-requests/{request_id}/reconcile",
        response_class=JSONResponse,
    )
    async def reconcile(
        request_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        denied, admin_id = await anyio.to_thread.run_sync(lambda: _admin(authorization))
        if denied is not None:
            return denied
        _payload, bad = await _read_small_json(request)
        if bad is not None:
            return bad

        def _reconcile_worker() -> JSONResponse:
            try:
                with sqlite3.connect(fixture.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys=ON")
                    admin_context = verify_admin_context(conn, str(admin_id))
                    result = reconcile_pair_reschedule_readback(
                        conn,
                        _sheet(conn),
                        order_id=fixture.order_id,
                        request_id=request_id,
                        admin_context=admin_context,
                        now=_now(),
                    )
                    conn.execute(
                        """UPDATE customer_pair_reschedule_requests
                              SET status=?,operation_id=?
                            WHERE request_id=? AND order_id=?""",
                        (result.status, result.operation_id, request_id, fixture.order_id),
                    )
                    conn.commit()
                    return _result_response(result)
            except RescheduleAuthorizationError:
                return JSONResponse({"detail": "forbidden"}, status_code=403, headers=NO_STORE_HEADERS)
            except (RescheduleFeatureUnavailable, RescheduleRequestConflict, PairRescheduleConflict) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=409, headers=NO_STORE_HEADERS)
            except Exception as exc:
                return JSONResponse(
                    {"detail": f"改期讀回暫時無法處理: {type(exc).__name__}"},
                    status_code=503,
                    headers=NO_STORE_HEADERS,
                )

        return await anyio.to_thread.run_sync(_reconcile_worker)

    router.include_router(
        create_customer_reschedule_router(
            liff_id=liff_id,
            channel_id=channel_id,
            db_path=fixture.db_path,
            app_env="staging",
            pair_reschedule_enabled=True,
            token_verifier=token_verifier,
            now_factory=_now,
            admin_notification_sender=None,
            html_path=_HTML_PATH,
            customer_authorizer=lambda user_id: user_id == fixture.owner_user_id,
            target_occupied_dates_loader=lambda conn, _order_id, _user_id: _current_occupied_dates(
                conn,
                fixture=fixture,
            ),
            target_policy_validator=lambda conn, _order_id, _user_id, source_date, target_date: (
                None
                if _target_is_eligible_for_submit(
                    conn,
                    fixture=fixture,
                    source_date=source_date,
                    target_date=target_date,
                )
                else "目標日期不在目前有效權益內，無法送出改期。"
            ),
            semantic_pending_request_loader=_find_actionable_semantic_request,
            offload_blocking_submit=True,
        )
    )

    return router


def attach_isolated_reschedule_qa_routes(
    app: Any,
    *,
    environ: Mapping[str, str],
    main_db_path: str,
    token_verifier: Callable[..., str] = verify_line_id_token,
    now_factory: Callable[[], datetime] | None = None,
    adapter_factory: Callable[[sqlite3.Connection, int, IsolatedRescheduleFixture], object] | None = None,
) -> bool:
    enabled = isolated_reschedule_qa_enabled(environ)
    app_env = str(environ.get("APP_ENV") or "legacy").strip().lower()
    if not enabled:
        return False
    if app_env != "staging":
        raise RuntimeError("isolated reschedule QA can only be enabled in staging")
    liff_id = str(environ.get("CUSTOMER_RESCHEDULE_LIFF_ID") or "").strip()
    channel_id = str(environ.get("CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID") or "").strip()
    if not liff_id:
        raise ValueError("CUSTOMER_RESCHEDULE_LIFF_ID must be set in staging")
    if not channel_id:
        raise ValueError("CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID must be set in staging")
    db_path = str(environ.get("ISOLATED_RESCHEDULE_QA_DB_PATH") or DEFAULT_DB_PATH).strip()
    manifest_path = str(
        environ.get("ISOLATED_RESCHEDULE_QA_MANIFEST_PATH") or DEFAULT_MANIFEST_PATH
    ).strip()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=liff_id,
            channel_id=channel_id,
            app_env=app_env,
            db_path=db_path,
            manifest_path=manifest_path,
            main_db_path=main_db_path,
            token_verifier=token_verifier,
            now_factory=now_factory,
            adapter_factory=adapter_factory,
        )
    )
    return True
