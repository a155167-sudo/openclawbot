"""Staging-only HTTP routes for the customer meal-reschedule LIFF.

The router authenticates the LINE ID token from the Authorization header using
the same ``verify_line_id_token`` verifier as the health-check LIFF.  The
verified LINE user ID becomes the server-side ``actor_id`` — the client body
never supplies or overrides the caller identity.

Three endpoints are exposed:
* ``GET /customer-reschedule/context`` — owner-bound orders and service dates.
* ``GET /customer-reschedule/preview`` — read-only date validation.
* ``POST /customer-reschedule/pending-request`` — writes a ``pending_admin``
  row via the existing ``reschedule_service_integration`` boundary.

The router is only mounted when ``APP_ENV == "staging"``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import anyio
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from customer_health_check_liff import (
    LINE_AUTH_DIAGNOSTIC_CODES,
    LineAuthenticationError,
    LineAuthenticationUnavailable,
    NO_STORE_HEADERS,
    verify_line_id_token,
)
from customer_reschedule_liff import (
    MAX_FORWARD_DAYS,
    allowed_target_dates,
    validate_target_date,
    submit_pending_admin_request,
)
from reschedule_service_integration import (
    RescheduleAuthorizationError,
    RescheduleFeatureUnavailable,
    RescheduleRequestConflict,
    list_pending_admin_customer_pair_reschedules,
    verify_customer_reschedule_context,
    submit_customer_pair_reschedule_pending,
)
from pair_reschedule_coordinator import PairRescheduleConflict, verify_admin_context
from pair_reschedule_coordinator import reconcile_pair_reschedule_readback
from reschedule_service_integration import approve_customer_pair_reschedule


_HTML_PATH = Path(__file__).with_name("customer-reschedule-liff.html")


def _parse_iso_date(value: str) -> str:
    """Return a validated YYYY-MM-DD string or raise ValueError."""
    value = str(value or "").strip()
    from datetime import date as _date
    parsed = _date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD format")
    return value


def create_customer_reschedule_router(
    *,
    liff_id: str,
    channel_id: str,
    db_path: str,
    app_env: str,
    pair_reschedule_enabled: bool = False,
    token_verifier: Callable[..., str] = verify_line_id_token,
    now_factory: Callable[[], datetime] | None = None,
    menu_context_loader: Callable[[str], Mapping[str, object] | None] | None = None,
    admin_notification_sender: Callable[[str, str, str], None] | None = None,
    html_path: str | Path | None = None,
    customer_authorizer: Callable[[str], bool] | None = None,
    target_occupied_dates_loader: Callable[[Any, int, str], set[str] | frozenset[str]] | None = None,
    target_policy_validator: Callable[[Any, int, str, str, str], str | None] | None = None,
    semantic_pending_request_loader: Callable[[Any, int, str, str, str, datetime], Mapping[str, object] | None] | None = None,
    offload_blocking_submit: bool = False,
    normal_flow: bool = False,
    preview_only: bool = False,
    sheet_factory: Callable[[Any, int], object] | None = None,
) -> APIRouter:
    """Build a router for the customer reschedule LIFF.

    Parameters
    ----------
    liff_id : str
        LIFF identifier used to render the HTML page.
    channel_id : str
        LINE Login channel ID used for ID-token verification.
    db_path : str
        SQLite database path for reading order/usage state.
    app_env : str
        Staging, or production with an explicit normal_flow boundary.
    pair_reschedule_enabled : bool
        Gate for the submit endpoint; preview is always available.
    token_verifier : callable
        Injectable verifier (same signature as ``verify_line_id_token``).
    now_factory : callable, optional
        Injectable clock; defaults to ``datetime.now``.
    """
    if app_env != "staging" and not (app_env == "production" and normal_flow):
        raise RuntimeError(
            "customer reschedule LIFF routes are staging-only unless production normal_flow is explicit"
        )
    liff_id = str(liff_id or "").strip()
    channel_id = str(channel_id or "").strip()
    if not re.fullmatch(r"[0-9]{5,}-[A-Za-z0-9_-]+", liff_id):
        raise ValueError("reschedule LIFF ID format invalid")
    if not re.fullmatch(r"[0-9]{5,}", channel_id):
        raise ValueError("reschedule LINE Login Channel ID format invalid")
    if not liff_id.startswith(channel_id + "-"):
        raise ValueError("reschedule LIFF does not belong to the configured LINE Login Channel")

    router = APIRouter()
    _now = now_factory or (lambda: datetime.now(ZoneInfo("Asia/Taipei")))
    page_path = Path(html_path) if html_path is not None else _HTML_PATH

    def _preview_write_denied() -> JSONResponse:
        return JSONResponse(
            {'detail': '目前為唯讀預覽，尚未開放送出或核准。', 'code': 'RESCHEDULE_PREVIEW_ONLY'},
            status_code=403, headers=NO_STORE_HEADERS,
        )

    # --- authentication helper -------------------------------------------
    def _authenticate(
        authorization: str | None,
    ) -> tuple[JSONResponse | None, str | None]:
        """Return ``(error_response, user_id)``; exactly one is non-None."""
        scheme, separator, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            return JSONResponse(
                {"detail": "需要 LINE 登入"},
                status_code=401,
                headers=NO_STORE_HEADERS,
            ), None
        try:
            return None, token_verifier(token.strip(), channel_id=channel_id)
        except LineAuthenticationError as exc:
            body = {"detail": "LINE 登入無效"}
            if normal_flow:
                import logging
                code = getattr(exc, "reason_code", "A00")
                if not isinstance(code, str) or code not in LINE_AUTH_DIAGNOSTIC_CODES:
                    code = "A00"
                body["auth_code"] = code
                logging.getLogger(__name__).warning("[LIFF-AUTH] rejected code=%s", code)
            return JSONResponse(
                body,
                status_code=401,
                headers=NO_STORE_HEADERS,
            ), None
        except LineAuthenticationUnavailable:
            return JSONResponse(
                {"detail": "LINE 驗證服務暫時無法使用"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            ), None
        except Exception:
            return JSONResponse(
                {"detail": "LINE 驗證服務暫時無法使用"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            ), None

    # --- page ------------------------------------------------------------
    @router.get("/customer-reschedule", response_class=HTMLResponse)
    def reschedule_page() -> HTMLResponse:
        html = page_path.read_text(encoding="utf-8")
        runtime = json.dumps(
            {"liffId": liff_id, "channelId": channel_id, **({'previewOnly': True} if preview_only else {})},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        html = html.replace(
            "null; /* __CUSTOMER_RESCHEDULE_RUNTIME__ */",
            f"{runtime};",
        )
        return HTMLResponse(
            html,
            headers=NO_STORE_HEADERS,
        )

    # --- preview (read-only) ---------------------------------------------
    @router.get("/customer-reschedule/context", response_class=JSONResponse)
    def reschedule_context(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Return only reschedule choices derived from the verified LINE subject."""
        denied, user_id = _authenticate(authorization)
        if denied is not None:
            return denied
        if customer_authorizer is not None and not customer_authorizer(str(user_id or "")):
            return JSONResponse(
                {"detail": "forbidden"},
                status_code=403,
                headers=NO_STORE_HEADERS,
            )

        import sqlite3

        try:
            database_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                usage = conn.execute(
                    """SELECT expiry_date FROM usage
                       WHERE user_id=? AND status IN ('active','vip')
                         AND typeof(remaining_meals)='integer' AND remaining_meals>0""",
                    (user_id,),
                ).fetchone()
                if usage is None:
                    return JSONResponse({"orders": []}, headers=NO_STORE_HEADERS)
                if normal_flow:
                    from normal_reschedule_policy import normal_order_expiry
                    local_now = _now()
                    fallback = menu_context_loader(str(user_id)) if menu_context_loader else None
                    if not fallback or not fallback.get('authoritative'):
                        return JSONResponse({'orders': []}, headers=NO_STORE_HEADERS)
                    oid = int(fallback.get('order_id') or 0)
                    try:
                        expiry = normal_order_expiry(conn, oid, str(user_id))
                    except PairRescheduleConflict:
                        return JSONResponse({'orders': []}, headers=NO_STORE_HEADERS)
                    sources = [item for item in fallback.get('source_dates', [])
                               if str(item['date']) > local_now.date().isoformat()
                               or (str(item['date']) == local_now.date().isoformat()
                                   and local_now.time() < time(8))]
                    occupied = set(fallback.get('occupied_dates') or [])
                    targets = [day for day in allowed_target_dates(expiry)
                               if day >= local_now.date().isoformat() and day not in occupied]
                    return JSONResponse({'orders': [{'order_id': oid,
                        'source_dates': sources, 'target_dates': targets}] if sources else []},
                        headers=NO_STORE_HEADERS)
                target_window = allowed_target_dates(str(usage["expiry_date"] or ""))
                local_now = _now()
                earliest_source = local_now.date()
                if local_now.time() >= time(8, 0):
                    earliest_source += timedelta(days=1)
                calendar_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscription_service_calendar'"
                ).fetchone() is not None
                order_rows = conn.execute(
                    """SELECT id FROM subscription_orders
                       WHERE user_id=? AND status='activated'
                       ORDER BY id""",
                    (user_id,),
                ).fetchall()
                orders = []
                for order_row in order_rows:
                    order_id = int(order_row["id"])
                    calendar_rows = []
                    if calendar_exists:
                        calendar_rows = conn.execute(
                            """SELECT service_date,schedule_label
                               FROM subscription_service_calendar
                               WHERE order_id=? AND is_service_day=1
                                 AND service_date>=?
                               ORDER BY service_date""",
                            (order_id, earliest_source.isoformat()),
                        ).fetchall()
                    occupied = {str(row["service_date"]) for row in calendar_rows}
                    orders.append(
                        {
                            "order_id": order_id,
                            "source_dates": [
                                {
                                    "date": _parse_iso_date(str(row["service_date"])),
                                    "label": str(row["schedule_label"] or ""),
                                }
                                for row in calendar_rows
                            ],
                            "target_dates": [date for date in target_window if date not in occupied],
                        }
                    )
                fallback = None
                if menu_context_loader is not None:
                    fallback = menu_context_loader(user_id) or {}
                    fallback_order_id = fallback.get("order_id")
                    fallback_sources = fallback.get("source_dates") or []
                    fallback_occupied = {
                        str(value) for value in (fallback.get("occupied_dates") or [])
                    }
                    if fallback.get("authoritative") and fallback_order_id in {
                        order["order_id"] for order in orders
                    }:
                        orders = [{
                            "order_id": int(fallback_order_id),
                            "source_dates": fallback_sources,
                            "target_dates": [date for date in target_window if date not in fallback_occupied],
                        }]
                    elif not any(order.get("source_dates") for order in orders) and fallback_order_id not in (None, "") and fallback_sources:
                        fallback_order = {
                            "order_id": int(fallback_order_id),
                            "source_dates": fallback_sources,
                            "target_dates": [date for date in target_window if date not in fallback_occupied],
                        }
                        orders = [fallback_order]
                    elif not orders:
                        reason = "既有菜單資料尚未綁定可改期訂單"
                        if fallback is None:
                            reason = "找不到此帳號的既有菜單來源"
                        elif not fallback.get("order_id"):
                            reason = "找到菜單來源，但沒有已啟用的包月訂單"
                        elif not fallback.get("source_dates"):
                            reason = "找到包月訂單，但菜單沒有可用日期"
                        return JSONResponse({"orders": [], "detail": reason}, headers=NO_STORE_HEADERS)
                if not any(order.get("source_dates") for order in orders):
                    reason = "既有菜單資料尚未綁定可改期訂單"
                    if fallback is None:
                        reason = "找不到此帳號的既有菜單來源"
                    elif not fallback.get("order_id"):
                        reason = "找到菜單來源，但沒有已啟用的包月訂單"
                    elif not fallback.get("source_dates"):
                        reason = "找到包月訂單，但菜單沒有可用日期"
                    return JSONResponse({"orders": [], "detail": reason}, headers=NO_STORE_HEADERS)
                return JSONResponse({"orders": orders}, headers=NO_STORE_HEADERS)
        except (sqlite3.Error, TypeError, ValueError, OSError):
            return JSONResponse(
                {"detail": "改期選項暫時無法載入"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            )

    # --- admin readback (staging-only, read-only) -------------------------
    @router.get("/api/admin/customer-pair-reschedule-requests", response_class=JSONResponse)
    def admin_reschedule_requests(
        authorization: str | None = Header(default=None),
        status: str = "pending_admin",
        limit: str = "50",
    ) -> JSONResponse:
        denied, user_id = _authenticate(authorization)
        if denied is not None:
            return denied
        if status != "pending_admin":
            return JSONResponse(
                {"detail": "status must be pending_admin"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )
        if len(str(limit)) > 3 or not re.fullmatch(r"[0-9]+", str(limit)):
            return JSONResponse(
                {"detail": "limit 格式無效"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )
        parsed_limit = int(limit)
        if not 1 <= parsed_limit <= 100:
            return JSONResponse(
                {"detail": "limit 格式無效"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )
        if not pair_reschedule_enabled:
            return JSONResponse(
                {"detail": "雙餐改期功能目前未開放"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            )

        import sqlite3

        try:
            database_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                if normal_flow:
                    try:
                        verify_admin_context(conn, str(user_id or ''))
                    except PairRescheduleConflict:
                        return JSONResponse({'detail': 'forbidden'}, status_code=403, headers=NO_STORE_HEADERS)
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='customer_pair_reschedule_requests'").fetchone() is None:
                        return JSONResponse({'requests': []}, headers=NO_STORE_HEADERS)
                    rows = conn.execute('''SELECT request_id,order_id,source_date,target_date,
                        status,created_at,expires_at,typeof(order_id) FROM customer_pair_reschedule_requests
                        WHERE status IN ('pending_admin','sheet_unknown')
                        ORDER BY created_at ASC,request_id ASC LIMIT ?''', (parsed_limit,)).fetchall()
                    if any(
                        row[7] != 'integer' or isinstance(row[1], bool)
                        or not isinstance(row[1], int) or row[1] <= 0
                        for row in rows
                    ):
                        raise ValueError('invalid persisted order_id')
                    return JSONResponse({'requests': [{
                        'request_id': str(row[0]), 'order_id': int(row[1]),
                        'source_date': str(row[2]), 'target_date': str(row[3]),
                        'status': str(row[4]), 'created_at': str(row[5]),
                        'expires_at': str(row[6]),
                        'can_approve': not preview_only and str(row[4]) == 'pending_admin',
                        'can_reconcile': not preview_only and str(row[4]) == 'sheet_unknown',
                        **({'disabled_reason': '目前為唯讀預覽，尚未開放核准。'} if preview_only else {}),
                    } for row in rows]}, headers=NO_STORE_HEADERS)
                try:
                    admin_context = verify_admin_context(conn, str(user_id or ""))
                except PairRescheduleConflict:
                    return JSONResponse(
                        {"detail": "forbidden"},
                        status_code=403,
                        headers=NO_STORE_HEADERS,
                    )
                try:
                    requests = list_pending_admin_customer_pair_reschedules(
                        conn,
                        admin_context=admin_context,
                        feature_enabled=True,
                        limit=parsed_limit,
                    )
                except RescheduleAuthorizationError:
                    return JSONResponse(
                        {"detail": "forbidden"},
                        status_code=403,
                        headers=NO_STORE_HEADERS,
                    )
                except RescheduleRequestConflict as exc:
                    return JSONResponse(
                        {"detail": str(exc)},
                        status_code=409,
                        headers=NO_STORE_HEADERS,
                    )
                return JSONResponse(
                    {
                        "requests": [
                            {
                                "request_id": item.request_id,
                                "order_id": item.order_id,
                                "source_date": item.source_date,
                                "target_date": item.target_date,
                                "status": item.status,
                                "created_at": item.created_at,
                                "expires_at": item.expires_at,
                                "admin_notification_status": item.admin_notification_status,
                                "admin_notification_last_error": item.admin_notification_last_error,
                                "admin_notification_attempted_at": item.admin_notification_attempted_at,
                            }
                            for item in requests
                        ],
                    },
                    headers=NO_STORE_HEADERS,
                )
        except (sqlite3.Error, TypeError, ValueError):
            return JSONResponse(
                {"detail": "改期申請讀取暫時無法使用"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            )

    # --- preview (read-only) ---------------------------------------------
    @router.get("/customer-reschedule/preview", response_class=JSONResponse)
    def reschedule_preview(
        authorization: str | None = Header(default=None),
        order_id: str | None = None,
        source_date: str | None = None,
        target_date: str | None = None,
    ) -> JSONResponse:
        denied, user_id = _authenticate(authorization)
        if denied is not None:
            return denied
        if customer_authorizer is not None and not customer_authorizer(str(user_id or "")):
            return JSONResponse(
                {"detail": "forbidden"},
                status_code=403,
                headers=NO_STORE_HEADERS,
            )

        # Validate query parameters
        try:
            oid = int(order_id) if order_id is not None else None
        except (TypeError, ValueError):
            return JSONResponse(
                {"detail": "order_id 格式無效"}, status_code=422, headers=NO_STORE_HEADERS
            )
        if oid is None:
            return JSONResponse(
                {"detail": "order_id 為必填"}, status_code=422, headers=NO_STORE_HEADERS
            )
        try:
            src = _parse_iso_date(source_date)
            tgt = _parse_iso_date(target_date)
        except (TypeError, ValueError):
            return JSONResponse(
                {"detail": "日期格式必須是 YYYY-MM-DD"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )

        import sqlite3

        try:
            database_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                # Verify the token user owns this activated order
                try:
                    ctx = verify_customer_reschedule_context(
                        conn, actor_id=user_id, order_id=oid
                    )
                except RescheduleAuthorizationError:
                    return JSONResponse(
                        {"detail": "無法驗證顧客訂單權限"},
                        status_code=403,
                        headers=NO_STORE_HEADERS,
                    )

                # Preview must use the same legacy summary fallback as context when the
                # staging service-calendar table is not present.
                if menu_context_loader is not None:
                    calendar_exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscription_service_calendar'"
                    ).fetchone() is not None
                    fallback = menu_context_loader(user_id) or {}
                    if not calendar_exists or fallback.get("authoritative"):
                        fallback_sources = {
                            str(item.get("date")) for item in (fallback.get("source_dates") or [])
                        }
                        if normal_flow:
                            from normal_reschedule_policy import normal_order_expiry, normal_pair_policy
                            try:
                                expiry_for_preview = normal_order_expiry(conn, oid, str(user_id))
                                normal_pair_policy(conn, oid, str(user_id), src, tgt, _now())
                            except PairRescheduleConflict as exc:
                                return JSONResponse({'detail': str(exc)}, status_code=422, headers=NO_STORE_HEADERS)
                        else:
                            expiry_for_preview = str((conn.execute("SELECT expiry_date FROM usage WHERE user_id=?", (user_id,)).fetchone()[0]) or "")
                        fallback_targets = {
                            str(item) for item in (allowed_target_dates(expiry_for_preview) or [])
                        } - {
                            str(item) for item in (fallback.get("occupied_dates") or [])
                        }
                        if int(fallback.get("order_id") or 0) != oid or src not in fallback_sources:
                            return JSONResponse(
                                {"detail": "來源日期不是可改期的排餐日"},
                                status_code=422,
                                headers=NO_STORE_HEADERS,
                            )
                        if tgt not in fallback_targets:
                            return JSONResponse(
                                {"detail": "目標日期已有排餐或不在可改期範圍"},
                                status_code=422,
                                headers=NO_STORE_HEADERS,
                            )
                        source_item = next(
                            (item for item in (fallback.get("source_dates") or []) if str(item.get("date")) == src),
                            None,
                        )
                        source_meals = list((source_item or {}).get("meals") or [])
                        if not source_meals:
                            return JSONResponse(
                                {"detail": "來源日期沒有可辨識的午餐／晚餐內容，暫時無法預覽"},
                                status_code=422,
                                headers=NO_STORE_HEADERS,
                            )
                        return JSONResponse(
                            {
                                "ok": True,
                                "source_date": src,
                                "target_date": tgt,
                                "source_meals": source_meals,
                                "window": {"from": src, "to": tgt},
                            },
                            headers=NO_STORE_HEADERS,
                        )

                source_row = conn.execute(
                    """SELECT schedule_label,typeof(is_service_day)
                       FROM subscription_service_calendar
                       WHERE order_id=? AND service_date=? AND is_service_day=1""",
                    (oid, src),
                ).fetchone()
                if source_row is None or source_row[1] != "integer":
                    return JSONResponse(
                        {"detail": "來源日期不是可改期的排餐日"},
                        status_code=422,
                        headers=NO_STORE_HEADERS,
                    )

                # Read usage to get expiry_date for target window validation
                usage_row = conn.execute(
                    "SELECT expiry_date FROM usage WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if not usage_row:
                    return JSONResponse(
                        {"detail": "找不到使用權益資料"},
                        status_code=404,
                        headers=NO_STORE_HEADERS,
                    )
                expiry_date = str(usage_row["expiry_date"] or "")

                if target_occupied_dates_loader is not None:
                    occupied = {
                        str(item) for item in target_occupied_dates_loader(conn, oid, str(user_id))
                    }
                else:
                    occupied_rows = conn.execute(
                        """SELECT service_date FROM subscription_service_calendar
                           WHERE order_id=? AND is_service_day=1""",
                        (oid,),
                    ).fetchall()
                    occupied = {str(r["service_date"]) for r in occupied_rows}

                # Validate target date
                try:
                    validate_target_date(expiry_date, tgt, frozenset(occupied))
                except ValueError as exc:
                    return JSONResponse(
                        {"detail": str(exc)},
                        status_code=422,
                        headers=NO_STORE_HEADERS,
                    )

                # Build preview of meals on source date
                meal_rows = conn.execute(
                    """SELECT service_date, schedule_label
                       FROM subscription_service_calendar
                       WHERE order_id=? AND service_date=?""",
                    (oid, src),
                ).fetchall()
                if any(not str(r["schedule_label"] or "").strip() for r in meal_rows):
                    return JSONResponse(
                        {"detail": "來源日期缺少實際餐點內容，暫時無法預覽"},
                        status_code=422,
                        headers=NO_STORE_HEADERS,
                    )

                window = allowed_target_dates(expiry_date)
                return JSONResponse(
                    {
                        "ok": True,
                        "source_date": src,
                        "target_date": tgt,
                        "source_meals": [
                            {"date": str(r["service_date"]), "label": str(r["schedule_label"] or "餐點內容未提供")}
                            for r in meal_rows
                        ],
                        "window": {"from": window[0], "to": window[-1]},
                    },
                    headers=NO_STORE_HEADERS,
                )
        except Exception:
            return JSONResponse(
                {"detail": "改期預覽暫時無法載入"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            )

    # --- submit pending-admin request -------------------------------------
    @router.post("/customer-reschedule/pending-request", response_class=JSONResponse)
    async def reschedule_submit(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        if offload_blocking_submit or normal_flow:
            denied, user_id = await anyio.to_thread.run_sync(
                lambda: _authenticate(authorization)
            )
        else:
            denied, user_id = _authenticate(authorization)
        if denied is not None:
            return denied
        if customer_authorizer is not None and not customer_authorizer(str(user_id or "")):
            return JSONResponse(
                {"detail": "forbidden"},
                status_code=403,
                headers=NO_STORE_HEADERS,
            )
        if preview_only:
            return _preview_write_denied()

        # Parse body with size limit
        content_length = request.headers.get("content-length", "")
        if content_length and (not content_length.isdigit() or int(content_length) > 2048):
            return JSONResponse(
                {"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS
            )
        body = await request.body()
        if len(body) > 2048:
            return JSONResponse(
                {"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS
            )
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                {"detail": "請求格式無效"}, status_code=422, headers=NO_STORE_HEADERS
            )
        required_keys = {"order_id", "source_date", "target_date", "request_id"}
        if set(payload) != required_keys:
            return JSONResponse(
                {"detail": f"請求必須包含 {', '.join(sorted(required_keys))}"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )

        # Validate request_id format
        rid = str(payload["request_id"] or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", rid):
            return JSONResponse(
                {"detail": "request_id 格式無效"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )

        try:
            oid = int(payload["order_id"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"detail": "order_id 格式無效"}, status_code=422, headers=NO_STORE_HEADERS
            )

        try:
            src = _parse_iso_date(payload["source_date"])
            tgt = _parse_iso_date(payload["target_date"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"detail": "日期格式必須是 YYYY-MM-DD"},
                status_code=422,
                headers=NO_STORE_HEADERS,
            )

        def _submit_worker() -> JSONResponse:
            if not pair_reschedule_enabled:
                return JSONResponse(
                    {"detail": "雙餐改期功能目前未開放"},
                    status_code=503,
                    headers=NO_STORE_HEADERS,
                )

            import sqlite3

            try:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys=ON")
                    if semantic_pending_request_loader is not None:
                        conn.execute("BEGIN IMMEDIATE")

                    # Verify ownership — token user_id is the server-side actor
                    try:
                        context = verify_customer_reschedule_context(
                            conn, actor_id=user_id, order_id=oid
                        )
                    except RescheduleAuthorizationError:
                        return JSONResponse(
                            {"detail": "無法驗證顧客訂單權限"},
                            status_code=403,
                            headers=NO_STORE_HEADERS,
                        )

                    if semantic_pending_request_loader is not None:
                        prior_semantic = semantic_pending_request_loader(
                            conn, oid, str(user_id), src, tgt, _now()
                        )
                        if prior_semantic is not None:
                            return JSONResponse(
                                {
                                    "ok": True,
                                    "request_id": str(prior_semantic["request_id"]),
                                    "order_id": int(prior_semantic["order_id"]),
                                    "owner_user_id": str(prior_semantic["owner_user_id"]),
                                    "source_date": str(prior_semantic["source_date"]),
                                    "target_date": str(prior_semantic["target_date"]),
                                    "status": str(prior_semantic["status"]),
                                    "created_at": str(prior_semantic["created_at"]),
                                    "expires_at": str(prior_semantic["expires_at"]),
                                    "admin_notification_status": str(
                                        prior_semantic.get("admin_notification_status") or ""
                                    ),
                                },
                                headers=NO_STORE_HEADERS,
                            )

                    calendar_exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscription_service_calendar'"
                    ).fetchone() is not None
                    fallback = None
                    if menu_context_loader is not None:
                        fallback = menu_context_loader(user_id) or {}
                    use_fallback = fallback is not None and (not calendar_exists or fallback.get("authoritative"))
                    if use_fallback:
                        source_dates = {str(item.get("date")) for item in (fallback.get("source_dates") or [])}
                        if int(fallback.get("order_id") or 0) != oid or src not in source_dates:
                            return JSONResponse({"detail": "來源日期不是可改期的排餐日"}, status_code=422, headers=NO_STORE_HEADERS)
                    elif calendar_exists:
                        source_row = conn.execute(
                            """SELECT typeof(is_service_day)
                               FROM subscription_service_calendar
                               WHERE order_id=? AND service_date=? AND is_service_day=1""",
                            (oid, src),
                        ).fetchone()
                        if source_row is None or source_row[0] != "integer":
                            return JSONResponse(
                                {"detail": "來源日期不是可改期的排餐日"},
                                status_code=422,
                                headers=NO_STORE_HEADERS,
                            )

                    # Validate target date against usage window and occupied dates
                    usage_row = conn.execute(
                        "SELECT expiry_date FROM usage WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    if not usage_row:
                        return JSONResponse(
                            {"detail": "找不到使用權益資料"},
                            status_code=404,
                            headers=NO_STORE_HEADERS,
                        )
                    if normal_flow:
                        from normal_reschedule_policy import normal_order_expiry
                        try:
                            expiry_date = normal_order_expiry(conn, oid, str(user_id))
                        except PairRescheduleConflict as exc:
                            return JSONResponse({'detail': str(exc)}, status_code=422, headers=NO_STORE_HEADERS)
                    else:
                        expiry_date = str(usage_row["expiry_date"] or "")
                    if target_occupied_dates_loader is not None:
                        occupied = {
                            str(item) for item in target_occupied_dates_loader(conn, oid, str(user_id))
                        }
                    elif calendar_exists and not use_fallback:
                        occupied_rows = conn.execute(
                            """SELECT service_date FROM subscription_service_calendar
                               WHERE order_id=? AND is_service_day=1""",
                            (oid,),
                        ).fetchall()
                        occupied = {str(r["service_date"]) for r in occupied_rows}
                    else:
                        occupied = {str(item) for item in (fallback.get("occupied_dates") or [])}
                    try:
                        validate_target_date(expiry_date, tgt, frozenset(occupied))
                    except ValueError as exc:
                        return JSONResponse(
                            {"detail": str(exc)},
                            status_code=422,
                            headers=NO_STORE_HEADERS,
                        )
                    if target_policy_validator is not None:
                        detail = target_policy_validator(conn, oid, str(user_id), src, tgt)
                        if detail:
                            return JSONResponse(
                                {"detail": detail},
                                status_code=422,
                                headers=NO_STORE_HEADERS,
                            )
                    if normal_flow:
                        from normal_reschedule_policy import normal_pair_policy
                        try:
                            normal_pair_policy(conn, oid, str(user_id), src, tgt, _now())
                        except PairRescheduleConflict as exc:
                            return JSONResponse({'detail': str(exc)}, status_code=422, headers=NO_STORE_HEADERS)

                    # Submit through the service integration boundary
                    try:
                        result = submit_customer_pair_reschedule_pending(
                            conn,
                            context=context,
                            source_date=src,
                            target_date=tgt,
                            request_id=rid,
                            now=_now(),
                            feature_enabled=True,
                            notification_enabled=pair_reschedule_enabled,
                            notification_sender=admin_notification_sender,
                        )
                    except RescheduleFeatureUnavailable:
                        return JSONResponse(
                            {"detail": "雙餐改期功能目前未開放"},
                            status_code=503,
                            headers=NO_STORE_HEADERS,
                        )
                    except RescheduleAuthorizationError:
                        return JSONResponse(
                            {"detail": "無法驗證顧客訂單權限"},
                            status_code=403,
                            headers=NO_STORE_HEADERS,
                        )
                    except RescheduleRequestConflict as exc:
                        return JSONResponse(
                            {"detail": str(exc)},
                            status_code=409,
                            headers=NO_STORE_HEADERS,
                        )

                    return JSONResponse(
                        {
                            "ok": True,
                            "request_id": result.request_id,
                            "order_id": result.order_id,
                            "owner_user_id": result.owner_user_id,
                            "source_date": result.source_date,
                            "target_date": result.target_date,
                            "status": result.status,
                            "created_at": result.created_at,
                            "expires_at": result.expires_at,
                            "admin_notification_status": result.admin_notification_status,
                        },
                        headers=NO_STORE_HEADERS,
                    )
            except RescheduleRequestConflict as exc:
                return JSONResponse(
                    {"detail": str(exc)}, status_code=409, headers=NO_STORE_HEADERS
                )
            except Exception as exc:
                print(f"⚠️ customer reschedule pending request failed: {type(exc).__name__}: {exc}")
                return JSONResponse(
                    {"detail": "改期申請暫時無法處理"},
                    status_code=503,
                    headers=NO_STORE_HEADERS,
                )

        if offload_blocking_submit or normal_flow:
            return await anyio.to_thread.run_sync(_submit_worker)
        return _submit_worker()

    if normal_flow:
        @router.post('/api/admin/customer-pair-reschedule-requests/{request_id}/approve')
        def normal_admin_approve(request_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
            denied, actor = _authenticate(authorization)
            if denied is not None:
                return denied
            if preview_only:
                return _preview_write_denied()
            if not pair_reschedule_enabled:
                return JSONResponse({'detail': '雙餐改期功能目前未開放'}, status_code=503, headers=NO_STORE_HEADERS)
            import sqlite3
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    admin = verify_admin_context(conn, str(actor))
                    from normal_reschedule_policy import normal_pair_policy
                    result = approve_customer_pair_reschedule(
                        conn, None, request_id=request_id, admin_context=admin,
                        now=_now(), feature_enabled=True, sheet_factory=sheet_factory,
                        policy_validator=normal_pair_policy)
                    return JSONResponse({'ok': True, 'request_id': result.request_id,
                        'operation_id': result.operation_id, 'status': result.status},
                        headers=NO_STORE_HEADERS)
            except PairRescheduleConflict:
                return JSONResponse({'detail': 'forbidden'}, status_code=403, headers=NO_STORE_HEADERS)
            except (RescheduleRequestConflict, RescheduleAuthorizationError) as exc:
                return JSONResponse({'detail': str(exc)}, status_code=409, headers=NO_STORE_HEADERS)
            except Exception as exc:
                return JSONResponse({'detail': f'核准暫時無法處理: {type(exc).__name__}'}, status_code=503, headers=NO_STORE_HEADERS)

        @router.post('/api/admin/customer-pair-reschedule-requests/{request_id}/reconcile')
        def normal_admin_reconcile(request_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
            denied, actor = _authenticate(authorization)
            if denied is not None:
                return denied
            if preview_only:
                return _preview_write_denied()
            if not pair_reschedule_enabled:
                return JSONResponse({'detail': '雙餐改期功能目前未開放'}, status_code=503, headers=NO_STORE_HEADERS)
            import sqlite3
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    admin = verify_admin_context(conn, str(actor))
                    row = conn.execute('''SELECT order_id,status FROM customer_pair_reschedule_requests
                        WHERE request_id=?''', (request_id,)).fetchone()
                    if not row or str(row[1]) not in ('sheet_unknown','confirmed'):
                        raise RescheduleRequestConflict('request is not awaiting readback')
                    if not callable(sheet_factory):
                        raise RescheduleRequestConflict('reschedule sheet adapter is unavailable')
                    result = reconcile_pair_reschedule_readback(
                        conn, sheet_factory(conn, int(row[0])), order_id=int(row[0]),
                        request_id=request_id, admin_context=admin, now=_now(),
                        require_customer_request=True)
                    return JSONResponse({'ok': True, 'request_id': result.request_id,
                        'operation_id': result.operation_id, 'status': result.status},
                        headers=NO_STORE_HEADERS)
            except PairRescheduleConflict:
                return JSONResponse({'detail': 'forbidden or readback conflict'}, status_code=403, headers=NO_STORE_HEADERS)
            except RescheduleRequestConflict as exc:
                return JSONResponse({'detail': str(exc)}, status_code=409, headers=NO_STORE_HEADERS)
            except Exception as exc:
                return JSONResponse({'detail': f'讀回暫時無法處理: {type(exc).__name__}'}, status_code=503, headers=NO_STORE_HEADERS)

    return router


def attach_customer_reschedule_liff_routes(
    app: Any,
    *,
    enabled: bool,
    environ: Mapping[str, str],
    db_path: str,
    pair_reschedule_enabled: bool = False,
    token_verifier: Callable[..., str] = verify_line_id_token,
    now_factory: Callable[[], datetime] | None = None,
    menu_context_loader: Callable[[str], Mapping[str, object] | None] | None = None,
    admin_notification_sender: Callable[[str, str, str], None] | None = None,
    normal_flow: bool = False,
    sheet_factory: Callable[[Any, int], object] | None = None,
    html_path: str | Path | None = None,
    preview_only: bool = False,
    semantic_pending_request_loader: Callable[[Any, int, str, str, str, datetime], Mapping[str, object] | None] | None = None,
) -> bool:
    """Mount opt-in staging or production normal-flow routes; never production QA.

    Returns True if the routes were mounted, False if skipped.
    """
    app_env = str(environ.get("APP_ENV") or "legacy").strip().lower()
    if not enabled or (app_env != "staging" and not (app_env == "production" and normal_flow)):
        return False
    liff_id = str(environ.get("CUSTOMER_RESCHEDULE_LIFF_ID") or "").strip()
    channel_id = str(
        environ.get("CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID") or ""
    ).strip()
    if not liff_id:
        raise ValueError(f"CUSTOMER_RESCHEDULE_LIFF_ID must be set in {app_env}")
    if not channel_id:
        raise ValueError(f"CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID must be set in {app_env}")
    app.include_router(
        create_customer_reschedule_router(
            liff_id=liff_id,
            channel_id=channel_id,
            db_path=db_path,
            app_env=app_env,
            pair_reschedule_enabled=pair_reschedule_enabled,
            token_verifier=token_verifier,
            now_factory=now_factory,
            menu_context_loader=menu_context_loader,
            admin_notification_sender=admin_notification_sender,
            normal_flow=normal_flow,
            sheet_factory=sheet_factory,
            html_path=html_path,
            preview_only=preview_only,
            semantic_pending_request_loader=semantic_pending_request_loader,
        )
    )
    return True
