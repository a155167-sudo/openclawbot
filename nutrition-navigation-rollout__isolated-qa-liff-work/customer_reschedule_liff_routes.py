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
        Must be ``"staging"`` for the router to be created.
    pair_reschedule_enabled : bool
        Gate for the submit endpoint; preview is always available.
    token_verifier : callable
        Injectable verifier (same signature as ``verify_line_id_token``).
    now_factory : callable, optional
        Injectable clock; defaults to ``datetime.now``.
    """
    if app_env != "staging":
        raise RuntimeError(
            "customer reschedule LIFF routes are staging-only"
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
        except LineAuthenticationError:
            return JSONResponse(
                {"detail": "LINE 登入無效"},
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
            {"liffId": liff_id, "channelId": channel_id},
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
                if menu_context_loader is not None and not any(
                    order.get("source_dates") for order in orders
                ):
                    fallback = menu_context_loader(user_id) or {}
                    fallback_order_id = fallback.get("order_id")
                    fallback_sources = fallback.get("source_dates") or []
                    fallback_occupied = {
                        str(value) for value in (fallback.get("occupied_dates") or [])
                    }
                    if fallback_order_id not in (None, "") and fallback_sources:
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
        except sqlite3.Error:
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
            with sqlite3.connect(db_path) as conn:
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
                    if not calendar_exists:
                        fallback = menu_context_loader(user_id) or {}
                        fallback_sources = {
                            str(item.get("date")) for item in (fallback.get("source_dates") or [])
                        }
                        fallback_targets = {
                            str(item) for item in (allowed_target_dates(
                                str((conn.execute("SELECT expiry_date FROM usage WHERE user_id=?", (user_id,)).fetchone()[0]) or "")
                            ) or [])
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
        if offload_blocking_submit:
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
                    if not calendar_exists and menu_context_loader is not None:
                        fallback = menu_context_loader(user_id) or {}
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
                    expiry_date = str(usage_row["expiry_date"] or "")
                    if target_occupied_dates_loader is not None:
                        occupied = {
                            str(item) for item in target_occupied_dates_loader(conn, oid, str(user_id))
                        }
                    elif calendar_exists:
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

        if offload_blocking_submit:
            return await anyio.to_thread.run_sync(_submit_worker)
        return _submit_worker()

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
) -> bool:
    """Mount the customer reschedule LIFF routes, staging only.

    Returns True if the routes were mounted, False if skipped.
    """
    app_env = str(environ.get("APP_ENV") or "legacy").strip().lower()
    if app_env != "staging" or not enabled:
        return False
    liff_id = str(environ.get("CUSTOMER_RESCHEDULE_LIFF_ID") or "").strip()
    channel_id = str(
        environ.get("CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID") or ""
    ).strip()
    if not liff_id:
        raise ValueError("CUSTOMER_RESCHEDULE_LIFF_ID must be set in staging")
    if not channel_id:
        raise ValueError("CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID must be set in staging")
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
        )
    )
    return True
