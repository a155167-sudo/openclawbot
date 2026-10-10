from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
import requests

from health_check_rules import HEALTH_CHECK_DAY_RULE_MINIMUM_MEALS
from nutrition_system import (
    meal_photo_estimate_snapshot_is_valid,
    user_confirmed_meal_photo_estimate_is_valid,
    user_confirmed_meal_photo_trust_projection,
    verified_exchange_approval_projection,
)
from protected_health_check_image import ImagePreview, ImageUnavailable, read_bounded_preview
from dietitian_health_check_draft import DraftConflict, DraftNotFound
from vip_health_check import (
    health_check_source_hash_v2,
    health_check_source_token,
    normalized_health_check_meal_slot,
)


LINE_ID_TOKEN_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
LINE_ID_TOKEN_ISSUER = "https://access.line.me"
LINE_UID_PATTERN = re.compile(r"U[0-9a-f]{32}", re.IGNORECASE)
CHANNEL_ID_PATTERN = re.compile(r"[0-9]{10,}")
LIFF_ID_PATTERN = re.compile(r"([0-9]{10,})-[A-Za-z0-9_-]{8,}")
CASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
HEX_DIGEST_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
CANONICAL_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
INTERNAL_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9:/])/(?!/)[^\s?#]+")
WINDOWS_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+)"
)
FILE_URI_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9])file\s*:")
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
SENSITIVE_MARKER_PATTERN = re.compile(
    r"(?i)(?:#?vip(?:order|\d+)-[A-Za-z0-9_-]+|"
    r"(?:^|[^A-Za-z0-9])(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}|"
    r"\bbearer\s+[A-Za-z0-9._~-]+|"
    r"\bsecret[-_=][A-Za-z0-9_-]+|"
    r"\b(?:activation|event|source|review|case|food[_ -]?log|user)[_ -]?id\s*[:=]\s*\S+|"
    r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|credential|"
    r"activation[_ -]?secret)\s*[:=_-]\s*\S+)"
)
TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?(?:[Zz]|[+-]\d{2}:\d{2})?"
)
MAX_LINE_ID_TOKEN_LIFETIME_SECONDS = 31 * 24 * 60 * 60
_TAIPEI = ZoneInfo("Asia/Taipei")
CASE_STATUSES = frozenset(
    {
        "collecting",
        "ready_for_review",
        "needs_more_info",
        "approved_pending_delivery",
        "delivery_failed",
        "delivered",
        "expired",
        "cancelled",
    }
)
THREE_DAY_REQUIRED_STATUSES = frozenset(
    {"ready_for_review", "needs_more_info", "approved_pending_delivery", "delivery_failed", "delivered"}
)
REFRESHABLE_CASE_STATUSES = frozenset(
    {"collecting", "ready_for_review", "needs_more_info"}
)
IMAGE_VIEWABLE_CASE_STATUSES = frozenset(
    {"ready_for_review", "needs_more_info", "approved_pending_delivery", "delivery_failed"}
)

NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
NUTRITION_FIELDS = frozenset(
    {
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g", "fiber_g",
        "sugar_g", "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg",
    }
)
AI_OBSERVATION_FIELDS = frozenset(
    {"pattern", "patterns", "observation", "observations", "strengths", "gaps", "risks"}
)
REVIEW_FIELDS = frozenset(
    {
        "good", "priority", "next_7_days", "comment", "strengths",
        "improvements", "recommendations", "summary",
    }
)
SUGGESTED_VALUE_FIELDS = frozenset(
    {
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g", "fiber_g",
        "sodium_mg", "vegetable_servings", "water_ml",
    }
)
AI_TEXT_FIELDS = frozenset({"pattern", "observation"})
AI_TEXT_OR_LIST_FIELDS = AI_OBSERVATION_FIELDS - AI_TEXT_FIELDS
REVIEW_TEXT_FIELDS = frozenset({"good", "priority", "next_7_days", "comment", "summary"})
REVIEW_TEXT_OR_LIST_FIELDS = REVIEW_FIELDS - REVIEW_TEXT_FIELDS
_DROP = object()
_INVALID = object()


def _contains_sensitive_text(value: str) -> bool:
    decoded = value
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return any(
        LINE_UID_PATTERN.search(candidate)
        or HEX_DIGEST_PATTERN.search(candidate)
        or INTERNAL_PATH_PATTERN.search(candidate)
        or WINDOWS_PATH_PATTERN.search(candidate)
        or FILE_URI_PATTERN.search(candidate)
        or JWT_PATTERN.search(candidate)
        or SENSITIVE_MARKER_PATTERN.search(candidate)
        for candidate in (value, decoded)
    )


class LineAuthenticationError(ValueError):
    """The presented LINE credential is conclusively invalid."""


class LineAuthenticationUnavailable(RuntimeError):
    """LINE verification did not produce a trustworthy decision."""


@dataclass(frozen=True)
class DietitianHealthCheckConfig:
    enabled: bool
    liff_id: str = ""
    channel_id: str = ""
    allowed_uids: frozenset[str] = frozenset()


def load_dietitian_health_check_config(
    environ: Mapping[str, str],
) -> DietitianHealthCheckConfig:
    raw_enabled = str(environ.get("DIETITIAN_HEALTH_CHECK_READ_ENABLED") or "")
    if raw_enabled == "":
        return DietitianHealthCheckConfig(enabled=False)
    if raw_enabled not in {"true", "false"}:
        raise ValueError("DIETITIAN_HEALTH_CHECK_READ_ENABLED must be true or false")
    if raw_enabled == "false":
        return DietitianHealthCheckConfig(enabled=False)

    liff_id = str(environ.get("DIETITIAN_HEALTH_CHECK_LIFF_ID") or "")
    channel_id = str(
        environ.get("DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID") or ""
    )
    allowed_text = str(environ.get("DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS") or "")
    liff_match = LIFF_ID_PATTERN.fullmatch(liff_id)
    if not liff_match:
        raise ValueError("invalid DIETITIAN_HEALTH_CHECK_LIFF_ID")
    if not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise ValueError("invalid DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID")
    if liff_match.group(1) != channel_id:
        raise ValueError("dietitian LIFF and LINE Login Channel ID do not match")
    parts = allowed_text.split(",")
    if not allowed_text or any(not LINE_UID_PATTERN.fullmatch(uid) for uid in parts):
        raise ValueError("invalid DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS")
    if len(set(parts)) != len(parts):
        raise ValueError("duplicate DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS")
    return DietitianHealthCheckConfig(
        enabled=True,
        liff_id=liff_id,
        channel_id=channel_id,
        allowed_uids=frozenset(parts),
    )


def verify_line_id_token(
    id_token: str,
    *,
    channel_id: str,
    http_post: Callable[..., Any] = requests.post,
    now: Callable[[], float] = time.time,
) -> str:
    token = str(id_token or "").strip()
    if (
        not token
        or len(token) > 8192
        or not CHANNEL_ID_PATTERN.fullmatch(str(channel_id or ""))
    ):
        raise LineAuthenticationError("invalid LINE login")
    try:
        response = http_post(
            LINE_ID_TOKEN_VERIFY_URL,
            data={"id_token": token, "client_id": channel_id},
            timeout=5,
        )
    except Exception as exc:
        raise LineAuthenticationUnavailable("LINE verification unavailable") from exc
    try:
        status_code = int(response.status_code)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LineAuthenticationUnavailable("LINE verification unavailable") from exc
    if status_code == 429 or status_code >= 500:
        raise LineAuthenticationUnavailable("LINE verification unavailable")
    if status_code != 200:
        raise LineAuthenticationError("invalid LINE login")
    try:
        payload = response.json()
    except Exception as exc:
        raise LineAuthenticationUnavailable("LINE verification unavailable") from exc
    if not isinstance(payload, Mapping):
        raise LineAuthenticationUnavailable("LINE verification unavailable")

    required = ("iss", "aud", "sub", "exp", "iat")
    if any(key not in payload for key in required):
        raise LineAuthenticationUnavailable("LINE verification unavailable")
    exp = payload.get("exp")
    iat = payload.get("iat")
    if (
        isinstance(exp, bool)
        or not isinstance(exp, int)
        or isinstance(iat, bool)
        or not isinstance(iat, int)
    ):
        raise LineAuthenticationUnavailable("LINE verification unavailable")

    subject = payload.get("sub")
    current_time = int(now())
    if (
        payload.get("iss") != LINE_ID_TOKEN_ISSUER
        or payload.get("aud") != channel_id
        or not isinstance(subject, str)
        or not LINE_UID_PATTERN.fullmatch(subject)
        or exp <= current_time
        or iat <= 0
        or iat > current_time + 60
        or iat > exp
        or exp - iat > MAX_LINE_ID_TOKEN_LIFETIME_SECONDS
        or exp > current_time + MAX_LINE_ID_TOKEN_LIFETIME_SECONDS + 60
    ):
        raise LineAuthenticationError("invalid LINE login")
    return subject


def _safe_json(value: object) -> object | None:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(
            value,
            parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _clean_public_text(value: object) -> object:
    if not isinstance(value, str) or len(value) > 1000:
        return _INVALID
    return _DROP if _contains_sensitive_text(value) else value


def _clean_public_text_or_list(value: object) -> object:
    if isinstance(value, str):
        return _clean_public_text(value)
    if not isinstance(value, list) or len(value) > 100:
        return _INVALID
    cleaned: list[str] = []
    for item in value:
        projected = _clean_public_text(item)
        if projected is _INVALID:
            return _INVALID
        if projected is not _DROP:
            assert isinstance(projected, str)
            cleaned.append(projected)
    return cleaned


def _clean_public_number(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _INVALID
    if not math.isfinite(float(value)) or not 0 <= float(value) <= 1_000_000:
        return _INVALID
    return value


def _project_typed_json_object(
    value: object,
    *,
    numeric_fields: frozenset[str] = frozenset(),
    text_fields: frozenset[str] = frozenset(),
    text_or_list_fields: frozenset[str] = frozenset(),
) -> dict[str, object] | None:
    parsed = _safe_json(value)
    if not isinstance(parsed, Mapping):
        return None
    projected: dict[str, object] = {}
    for key in sorted(numeric_fields | text_fields | text_or_list_fields):
        if key not in parsed:
            continue
        if key in numeric_fields:
            cleaned = _clean_public_number(parsed[key])
        elif key in text_fields:
            cleaned = _clean_public_text(parsed[key])
        else:
            cleaned = _clean_public_text_or_list(parsed[key])
        if cleaned is _INVALID:
            return None
        if cleaned is not _DROP:
            projected[key] = cleaned
    return projected


def _required_text(value: object, field: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _contains_sensitive_text(value)
    ):
        raise ValueError(f"invalid {field}")
    return value


def _optional_text(value: object, field: str, *, maximum: int = 1000) -> str | None:
    if value in (None, ""):
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or _contains_sensitive_text(value)
    ):
        raise ValueError(f"invalid {field}")
    return value


def _bounded_number(
    value: object, field: str, *, minimum: float = 0, maximum: float = 1_000_000
) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field}")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"invalid {field}")
    return value


def _iso_datetime_text(value: object, field: str, *, optional: bool = False) -> str | None:
    text = _optional_text(value, field, maximum=64) if optional else _required_text(
        value, field, maximum=64
    )
    if text is None:
        return None
    if not TIMESTAMP_PATTERN.fullmatch(text):
        raise ValueError(f"invalid {field}")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    return text


def _taipei_datetime(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_TAIPEI)
    return parsed.astimezone(_TAIPEI)


def _local_date_text(value: object, field: str) -> str:
    text = _required_text(value, field, maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"invalid {field}")
    return text


def _source_hash_version(conn: sqlite3.Connection, log: sqlite3.Row) -> str | None:
    expected = log["source_hash"]
    version = log["food_log_version"]
    log_id = log["log_id"]
    if (
        not isinstance(expected, str)
        or CANONICAL_SHA256_PATTERN.fullmatch(expected) is None
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(log_id, str)
    ):
        return None
    raw_nutrition = log["nutrition_snapshot_json"]
    if not isinstance(raw_nutrition, str):
        return None
    try:
        canonical = json.dumps(
            json.loads(raw_nutrition),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        canonical = raw_nutrition
    trust = user_confirmed_meal_photo_trust_projection(
        conn, log_id, str(log["trust_type"] or "") if "trust_type" in log.keys() else ""
    )
    if trust["integrity_status"] == "integrity_verification_failed":
        return None
    trust_binding = (
        str(trust["effective_revision_hash"])
        if trust["integrity_status"] == "verified"
        else ""
    )
    legacy = hashlib.sha256(
        (
            f"{log_id}:{version}:{canonical}"
            + (
                f":user_confirmed_ai_estimate:{trust['effective_revision_hash']}"
                if trust["integrity_status"] == "verified"
                else ""
            )
        ).encode("utf-8")
    ).hexdigest()
    if hmac.compare_digest(legacy, expected):
        return "v1"
    keys = set(log.keys())
    if {"local_date", "consumed_at", "meal_slot"}.issubset(keys):
        try:
            consumed_text = _iso_datetime_text(
                log["consumed_at"], "source_log.consumed_at"
            )
            assert consumed_text is not None
            canonical_date = _taipei_datetime(consumed_text).date().isoformat()
            ref_date = _local_date_text(log["local_date"], "source_ref.local_date")
            normalized_slot = normalized_health_check_meal_slot(log["meal_slot"])
        except (AssertionError, TypeError, ValueError):
            return None
        if canonical_date != ref_date:
            return None
        current = health_check_source_hash_v2(
            food_log_id=log_id,
            food_log_version=version,
            nutrition_snapshot_json=raw_nutrition,
            local_date=ref_date,
            normalized_meal_slot=normalized_slot,
            effective_revision_hash=trust_binding,
        )
        if hmac.compare_digest(current, expected):
            return "v2"
    return None


def _source_hash_matches(conn: sqlite3.Connection, log: sqlite3.Row) -> bool:
    return _source_hash_version(conn, log) is not None


def _profile_from_row(row: sqlite3.Row | None) -> dict[str, object | None]:
    fields = ("name", "tdee", "protein", "goal", "restrictions", "active_days")
    if row is None:
        return {field: None for field in fields}
    return {
        "name": _optional_text(row["name"], "profile.name", maximum=200),
        "tdee": _bounded_number(row["tdee"], "profile.tdee", maximum=20_000),
        "protein": _bounded_number(row["protein"], "profile.protein", maximum=2_000),
        "goal": _optional_text(row["goal"], "profile.goal"),
        "restrictions": _optional_text(row["restrictions"], "profile.restrictions"),
        "active_days": _optional_text(row["active_days"], "profile.active_days", maximum=200),
    }


def _case_summary(row: sqlite3.Row) -> dict[str, object]:
    case_id = _required_text(row["case_id"], "case.case_id", maximum=128)
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError("invalid case.case_id")
    status = _required_text(row["status"], "case.status", maximum=40)
    if status not in CASE_STATUSES:
        raise ValueError("invalid case.status")
    valid_day_count = row["valid_day_count"]
    if (
        isinstance(valid_day_count, bool)
        or not isinstance(valid_day_count, int)
        or not 0 <= valid_day_count <= 7
    ):
        raise ValueError("invalid case.valid_day_count")
    window_started_at = _iso_datetime_text(
        row["window_started_at"], "case.window_started_at"
    )
    window_ends_at = _iso_datetime_text(row["window_ends_at"], "case.window_ends_at")
    assert window_started_at is not None and window_ends_at is not None
    if _taipei_datetime(window_started_at) >= _taipei_datetime(window_ends_at):
        raise ValueError("invalid case window")
    return {
        "case_id": case_id,
        "status": status,
        "window_started_at": window_started_at,
        "window_ends_at": window_ends_at,
        "valid_day_count": valid_day_count,
        "submitted_at": _iso_datetime_text(
            row["submitted_at"], "case.submitted_at", optional=True
        ),
        "report_published_at": _iso_datetime_text(
            row["report_published_at"], "case.report_published_at", optional=True
        ),
        "created_at": _iso_datetime_text(row["created_at"], "case.created_at"),
        "updated_at": _iso_datetime_text(row["updated_at"], "case.updated_at"),
        "profile": _profile_from_row(row),
    }


def _validated_days_for_case(
    conn: sqlite3.Connection, *, case_id: str
) -> list[dict[str, object]]:
    rows = conn.execute(
        """SELECT local_date,rule_version,qualifying_meal_count,
                  completeness_status,evaluated_at
           FROM vip_health_check_valid_days
           WHERE case_id=? ORDER BY local_date""",
        (case_id,),
    ).fetchall()
    validated_days: list[dict[str, object]] = []
    for day in rows:
        meal_count = day["qualifying_meal_count"]
        if (
            isinstance(meal_count, bool)
            or not isinstance(meal_count, int)
            or not 0 <= meal_count <= 100
        ):
            raise ValueError("invalid valid_day.qualifying_meal_count")
        completeness = _required_text(
            day["completeness_status"], "valid_day.completeness_status", maximum=40
        )
        rule_version = _required_text(
            day["rule_version"], "valid_day.rule_version", maximum=100
        )
        minimum_meals = HEALTH_CHECK_DAY_RULE_MINIMUM_MEALS.get(rule_version)
        expected_completeness = (
            "qualified"
            if minimum_meals is not None and meal_count >= minimum_meals
            else "incomplete"
        )
        if minimum_meals is None or completeness != expected_completeness:
            raise ValueError("invalid valid_day.completeness_status")
        validated_days.append(
            {
                "local_date": _local_date_text(
                    day["local_date"], "valid_day.local_date"
                ),
                "rule_version": rule_version,
                "qualifying_meal_count": meal_count,
                "completeness_status": completeness,
                "evaluated_at": _iso_datetime_text(
                    day["evaluated_at"], "valid_day.evaluated_at"
                ),
            }
        )
    return validated_days


def _validate_case_day_semantics(
    case: Mapping[str, object], validated_days: Sequence[Mapping[str, object]]
) -> None:
    qualified_count = sum(
        day["completeness_status"] == "qualified" for day in validated_days
    )
    if case["valid_day_count"] != qualified_count:
        raise ValueError("invalid case valid-day count")
    if case["status"] in THREE_DAY_REQUIRED_STATUSES and qualified_count < 3:
        raise ValueError("invalid case review threshold")
    window_start = _taipei_datetime(str(case["window_started_at"]))
    window_end = _taipei_datetime(str(case["window_ends_at"]))
    day_dates = [date.fromisoformat(str(day["local_date"])) for day in validated_days]
    end_date_is_included = window_end.time() != datetime.min.time()
    if len(day_dates) != len(set(day_dates)) or any(
        not (
            window_start.date() <= day_value
            and (
                day_value < window_end.date()
                or (day_value == window_end.date() and end_date_is_included)
            )
        )
        for day_value in day_dates
    ):
        raise ValueError("invalid valid-day window")


def _validate_case_source_semantics(
    conn: sqlite3.Connection,
    *,
    case: Mapping[str, object],
    validated_days: Sequence[Mapping[str, object]],
) -> None:
    food_log_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(food_logs)")}
    trust_columns = (
        ",fl.trust_type,fl.trust_hash,fl.exchange_snapshot_json"
        if {"trust_type", "trust_hash", "exchange_snapshot_json"}.issubset(food_log_columns)
        else ",'' AS trust_type,'' AS trust_hash,'{}' AS exchange_snapshot_json"
    )
    rows = conn.execute(
        f"""SELECT sr.food_log_id,sr.food_log_id AS log_id,
                  sr.food_log_version,sr.local_date,sr.source_hash,
                  fl.log_id AS canonical_log_id,fl.consumed_at,fl.meal_slot,
                  fl.nutrition_snapshot_json{trust_columns}
           FROM vip_health_check_source_refs sr
           JOIN vip_health_check_cases c ON c.case_id=sr.case_id
           LEFT JOIN food_logs fl
             ON fl.log_id=sr.food_log_id AND fl.user_id=c.user_id
            AND fl.version=sr.food_log_version
            AND fl.confirmation_status='confirmed' AND COALESCE(fl.deleted_at,'')=''
           WHERE sr.case_id=? ORDER BY sr.food_log_id""",
        (str(case["case_id"]),),
    ).fetchall()
    day_by_date = {str(day["local_date"]): day for day in validated_days}
    slots_by_date: dict[str, set[str]] = {day_name: set() for day_name in day_by_date}
    seen_log_ids: set[str] = set()
    window_start = _taipei_datetime(str(case["window_started_at"]))
    window_end = _taipei_datetime(str(case["window_ends_at"]))
    for row in rows:
        food_log_id = _required_text(
            row["food_log_id"], "source_ref.food_log_id", maximum=128
        )
        version = row["food_log_version"]
        source_hash = row["source_hash"]
        ref_date = _local_date_text(row["local_date"], "source_ref.local_date")
        canonical_log_id = row["canonical_log_id"]
        if (
            food_log_id in seen_log_ids
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(source_hash, str)
            or CANONICAL_SHA256_PATTERN.fullmatch(source_hash) is None
            or canonical_log_id != food_log_id
            or ref_date not in day_by_date
            or not _source_hash_matches(conn, row)
            or _project_typed_json_object(
                row["nutrition_snapshot_json"], numeric_fields=NUTRITION_FIELDS
            ) is None
        ):
            raise ValueError("invalid source reference semantics")
        consumed_text = _iso_datetime_text(
            row["consumed_at"], "source_log.consumed_at"
        )
        assert consumed_text is not None
        consumed_at = _taipei_datetime(consumed_text)
        if not window_start <= consumed_at < window_end:
            raise ValueError("invalid source reference window")
        if consumed_at.date().isoformat() != ref_date:
            raise ValueError("invalid source reference local date")
        meal_slot = str(row["meal_slot"] or "unspecified").strip() or "unspecified"
        slots_by_date[ref_date].add(meal_slot)
        seen_log_ids.add(food_log_id)
    for day_name, day in day_by_date.items():
        if len(slots_by_date[day_name]) != day["qualifying_meal_count"]:
            raise ValueError("invalid source reference meal count")


def _validated_case_source_refs(
    conn: sqlite3.Connection,
    *,
    case: Mapping[str, object],
    validated_days: Sequence[Mapping[str, object]],
    stored_manifest: object,
) -> list[sqlite3.Row]:
    source_refs = conn.execute(
        """SELECT food_log_id,food_log_version,local_date,source_hash
           FROM vip_health_check_source_refs WHERE case_id=? ORDER BY food_log_id""",
        (str(case["case_id"]),),
    ).fetchall()
    manifest_lines: list[str] = []
    valid_day_names = {str(day["local_date"]) for day in validated_days}
    seen_log_ids: set[str] = set()
    for source_ref in source_refs:
        food_log_id = _required_text(
            source_ref["food_log_id"], "source_ref.food_log_id", maximum=128
        )
        version = source_ref["food_log_version"]
        source_hash = source_ref["source_hash"]
        local_date = _local_date_text(source_ref["local_date"], "source_ref.local_date")
        if (
            food_log_id in seen_log_ids
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(source_hash, str)
            or CANONICAL_SHA256_PATTERN.fullmatch(source_hash) is None
            or local_date not in valid_day_names
        ):
            raise ValueError("invalid source reference")
        seen_log_ids.add(food_log_id)
        manifest_lines.append(f"{food_log_id}:{version}:{source_hash}")
    manifest_lines.extend(
        f"day:{day['local_date']}:{day['qualifying_meal_count']}:{day['rule_version']}"
        for day in validated_days
    )
    expected_manifest = hashlib.sha256(
        "\n".join(manifest_lines).encode("utf-8")
    ).hexdigest()
    empty_case = not source_refs and not validated_days and stored_manifest == ""
    if not empty_case and (
        not isinstance(stored_manifest, str)
        or CANONICAL_SHA256_PATTERN.fullmatch(stored_manifest) is None
        or not hmac.compare_digest(stored_manifest, expected_manifest)
    ):
        raise ValueError("invalid case source manifest")
    return source_refs


def load_health_check_list(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str],
    limit: int,
    offset: int,
) -> dict[str, object]:
    conn.row_factory = sqlite3.Row
    selected = tuple(statuses)
    if not selected or any(status not in CASE_STATUSES for status in selected):
        raise ValueError("invalid case status")
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise ValueError("invalid pagination")
    placeholders = ",".join("?" for _ in selected)
    count = conn.execute(
        f"SELECT COUNT(*) FROM vip_health_check_cases WHERE status IN ({placeholders})",
        selected,
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT c.case_id,c.status,c.window_started_at,c.window_ends_at,
                   c.valid_day_count,c.submitted_at,c.report_published_at,c.created_at,c.updated_at,
                   c.source_manifest_hash,
                   (SELECT COUNT(*) FROM vip_health_check_valid_days vd
                    WHERE vd.case_id=c.case_id AND vd.completeness_status='qualified')
                       AS qualified_day_rows,
                   hp.name,hp.tdee,hp.protein,hp.goal,hp.restrictions,hp.active_days
            FROM vip_health_check_cases c
            LEFT JOIN health_profile hp ON hp.user_id=c.user_id
            WHERE c.status IN ({placeholders})
            ORDER BY c.updated_at DESC,c.case_id ASC LIMIT ? OFFSET ?""",
        (*selected, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        item = _case_summary(row)
        validated_days = _validated_days_for_case(
            conn, case_id=str(item["case_id"])
        )
        _validate_case_day_semantics(item, validated_days)
        _validated_case_source_refs(
            conn,
            case=item,
            validated_days=validated_days,
            stored_manifest=row["source_manifest_hash"],
        )
        if item["status"] in REFRESHABLE_CASE_STATUSES:
            _validate_case_source_semantics(
                conn, case=item, validated_days=validated_days
            )
        if row["qualified_day_rows"] != item["valid_day_count"]:
            raise ValueError("invalid case list semantics")
        items.append(item)
    return {"items": items, "total": count, "limit": limit, "offset": offset}


def _verified_source_approval_status(
    conn: sqlite3.Connection, *, log_id: str
) -> str | None:
    """Return approved only when the canonical approval relationship and hash validate."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "food_exchange_approvals" not in tables:
        return None
    log_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(food_logs)")}
    catalog_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(food_catalog)")}
    approval_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(food_exchange_approvals)")
    }
    required_log = {"exchange_approval_id", "food_id", "user_id", "consumed_servings", "approved_exchange_json"}
    required_catalog = {"source_type", "owner_user_id", "fingerprint"}
    required_approval = {
        "approval_id", "food_id", "food_fingerprint", "suggestion_rule_version",
        "approved_exchange_json", "approved_exchange_hash",
    }
    if (
        not required_log.issubset(log_columns)
        or not required_catalog.issubset(catalog_columns)
        or not required_approval.issubset(approval_columns)
    ):
        return None
    row = conn.execute(
        """SELECT fl.user_id AS log_user_id,fl.food_id AS log_food_id,
                  fc.source_type AS catalog_source_type,fc.owner_user_id AS catalog_owner_user_id,
                  fc.fingerprint AS catalog_fingerprint,fl.consumed_servings,
                  fl.approved_exchange_json AS applied_json,fl.exchange_approval_id AS approval_id,
                  a.food_id AS approval_food_id,a.food_fingerprint AS approval_fingerprint,
                  a.suggestion_rule_version AS rule_version,
                  a.approved_exchange_json AS approved_json,
                  a.approved_exchange_hash AS approval_hash
           FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
           JOIN food_exchange_approvals a ON a.approval_id=fl.exchange_approval_id
           WHERE fl.log_id=?""",
        (log_id,),
    ).fetchone()
    if row is None:
        return None
    projection = verified_exchange_approval_projection(**dict(row))
    return "approved" if projection.get("is_valid") is True else None


def load_health_check_detail(
    conn: sqlite3.Connection,
    *,
    case_id: str,
) -> dict[str, object] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT c.case_id,c.status,c.window_started_at,c.window_ends_at,
                  c.valid_day_count,c.submitted_at,c.report_published_at,c.created_at,c.updated_at,
                  c.source_manifest_hash,
                  hp.name,hp.tdee,hp.protein,hp.goal,hp.restrictions,hp.active_days
           FROM vip_health_check_cases c
           LEFT JOIN health_profile hp ON hp.user_id=c.user_id
           WHERE c.case_id=?""",
        (case_id,),
    ).fetchone()
    if row is None:
        return None
    result = _case_summary(row)
    validated_days = _validated_days_for_case(conn, case_id=case_id)
    result["valid_days"] = validated_days
    _validate_case_day_semantics(result, validated_days)
    source_refs = _validated_case_source_refs(
        conn,
        case=result,
        validated_days=validated_days,
        stored_manifest=row["source_manifest_hash"],
    )
    if result["status"] in REFRESHABLE_CASE_STATUSES:
        _validate_case_source_semantics(
            conn, case=result, validated_days=validated_days
        )

    referenced_count = len(source_refs)
    food_log_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(food_logs)")}
    trust_columns = (
        ",fl.trust_type,fl.trust_hash,fl.exchange_snapshot_json"
        if {"trust_type", "trust_hash", "exchange_snapshot_json"}.issubset(food_log_columns)
        else ",'' AS trust_type,'' AS trust_hash,'{}' AS exchange_snapshot_json"
    )
    logs = conn.execute(
        f"""SELECT fl.log_id,sr.food_log_version,sr.source_hash,sr.local_date,
                  fl.consumed_at,fl.meal_slot,fl.nutrition_snapshot_json{trust_columns}
           FROM vip_health_check_source_refs sr
           JOIN vip_health_check_cases c ON c.case_id=sr.case_id
           JOIN food_logs fl ON fl.log_id=sr.food_log_id AND fl.user_id=c.user_id
              AND fl.version=sr.food_log_version
              AND fl.confirmation_status='confirmed' AND COALESCE(fl.deleted_at,'')=''
           WHERE sr.case_id=?
           ORDER BY fl.log_id""",
        (case_id,),
    ).fetchall()
    source_logs = []
    for log in logs:
        source_hash_version = _source_hash_version(conn, log)
        if source_hash_version is None:
            continue
        version = log["food_log_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("invalid source_log.food_log_version")
        nutrition_snapshot = _project_typed_json_object(
            log["nutrition_snapshot_json"], numeric_fields=NUTRITION_FIELDS
        )
        if nutrition_snapshot is None:
            continue
        source = {
            "log_id": _required_text(log["log_id"], "source_log.log_id", maximum=128),
            "food_log_version": version,
            "nutrition_snapshot": nutrition_snapshot,
        }
        if source_hash_version == "v2":
            source["local_date"] = _local_date_text(
                log["local_date"], "source_ref.local_date"
            )
            source["normalized_meal_slot"] = normalized_health_check_meal_slot(
                log["meal_slot"]
            )
        trust = user_confirmed_meal_photo_trust_projection(
            conn, log["log_id"], str(log["trust_type"] or "")
        )
        if trust["trust_type"] and trust["integrity_status"] != "verified":
            continue
        if trust["integrity_status"] == "verified":
            estimate = trust.get("estimate")
            if not meal_photo_estimate_snapshot_is_valid(estimate):
                continue
            nutrition_snapshot = trust.get("nutrition")
            if not isinstance(nutrition_snapshot, dict):
                continue
            source.update({
                "nutrition_snapshot": nutrition_snapshot,
                "trust_type": "user_confirmed_ai_estimate",
                "trust_label": "顧客確認・AI估算",
                "estimate_schema_version": trust["schema_version"],
                "estimate": estimate,
            })
        else:
            approval_status = _verified_source_approval_status(
                conn, log_id=str(log["log_id"])
            )
            if approval_status is not None:
                source["approval_status"] = approval_status
        source_logs.append(source)
    result["source_logs"] = source_logs
    result["source_integrity"] = {
        "referenced_count": referenced_count,
        "available_snapshot_count": len(source_logs),
        "all_snapshots_available": referenced_count == len(source_logs),
    }

    review = conn.execute(
        """SELECT review_id,review_version,status,ai_observations_json,review_json,
                  suggested_values_json,limitations,approved_at,created_at,updated_at,
                  source_manifest_hash
           FROM vip_health_check_reviews WHERE case_id=?
           ORDER BY review_version DESC LIMIT 1""",
        (case_id,),
    ).fetchone()
    case_manifest = row["source_manifest_hash"]
    review_manifest = None if review is None else review["source_manifest_hash"]
    current_review_version = 0 if review is None else review["review_version"]
    if (
        isinstance(current_review_version, bool)
        or not isinstance(current_review_version, int)
        or current_review_version < 0
        or (review is not None and current_review_version < 1)
    ):
        raise ValueError("invalid review version")
    result["current_review_version"] = current_review_version
    revision_table_exists = conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='dietitian_health_check_source_revisions'"""
    ).fetchone() is not None
    if revision_table_exists:
        result["source_token"] = health_check_source_token(
            conn, case_id=case_id, manifest_hash=str(case_manifest)
        )
    review_is_fresh = (
        review is not None
        and referenced_count == len(source_logs)
        and isinstance(case_manifest, str)
        and isinstance(review_manifest, str)
        and CANONICAL_SHA256_PATTERN.fullmatch(case_manifest) is not None
        and CANONICAL_SHA256_PATTERN.fullmatch(review_manifest) is not None
        and hmac.compare_digest(review_manifest, case_manifest)
    )
    result["latest_review_fresh"] = None if review is None else review_is_fresh
    result["latest_review_available"] = False
    if not review_is_fresh:
        result["latest_review"] = None
    else:
        assert review is not None
        review_version = review["review_version"]
        review_status = _required_text(review["status"], "review.status", maximum=20)
        if (
            isinstance(review_version, bool)
            or not isinstance(review_version, int)
            or review_version < 1
            or review_status not in {"draft", "approved", "superseded"}
        ):
            raise ValueError("invalid review")
        ai_observations = _project_typed_json_object(
            review["ai_observations_json"],
            text_fields=AI_TEXT_FIELDS,
            text_or_list_fields=AI_TEXT_OR_LIST_FIELDS,
        )
        review_payload = _project_typed_json_object(
            review["review_json"],
            text_fields=REVIEW_TEXT_FIELDS,
            text_or_list_fields=REVIEW_TEXT_OR_LIST_FIELDS,
        )
        suggested_values = _project_typed_json_object(
            review["suggested_values_json"], numeric_fields=SUGGESTED_VALUE_FIELDS
        )
        if ai_observations is None or review_payload is None or suggested_values is None:
            result["latest_review"] = None
            return result
        result["latest_review_available"] = True
        result["latest_review"] = {
            "review_id": _required_text(review["review_id"], "review.review_id", maximum=128),
            "review_version": review_version,
            "status": review_status,
            "ai_observations": ai_observations,
            "review": review_payload,
            "suggested_values": suggested_values,
            "limitations": _optional_text(
                review["limitations"], "review.limitations", maximum=5000
            ),
            "approved_at": _iso_datetime_text(
                review["approved_at"], "review.approved_at", optional=True
            ),
            "created_at": _iso_datetime_text(
                review["created_at"], "review.created_at"
            ),
            "updated_at": _iso_datetime_text(
                review["updated_at"], "review.updated_at"
            ),
        }
    return result


def load_health_check_image(
    conn: sqlite3.Connection, *, case_id: str, log_id: str, image_root: str,
) -> ImagePreview | None:
    """Resolve one private image through its canonical case/log/draft chain."""
    try:
        conn.row_factory = sqlite3.Row
        source_ref_columns = {
            str(item[1]) for item in conn.execute("PRAGMA table_info(vip_health_check_source_refs)")
        }
        food_log_columns = {
            str(item[1]) for item in conn.execute("PRAGMA table_info(food_logs)")
        }
        timeline_columns = (
            ",sr.local_date,fl.consumed_at,fl.meal_slot"
            if "local_date" in source_ref_columns
            and {"consumed_at", "meal_slot"}.issubset(food_log_columns)
            else ",'' AS local_date,'' AS consumed_at,'' AS meal_slot"
        )
        rows = conn.execute(
            f"""SELECT c.user_id AS case_user_id,c.status AS case_status,
                      sr.food_log_id,sr.food_log_version,sr.source_hash,
                      fl.log_id,fl.user_id AS log_user_id,fl.version,
                      fl.nutrition_snapshot_json,fl.source_image_ref,
                      fl.confirmation_status,fl.deleted_at,fl.trust_type,fl.trust_hash,
                      fl.exchange_snapshot_json,
                      fc.owner_user_id AS catalog_owner_user_id,
                      fc.source_type AS catalog_source_type,
                      fc.original_image_ref AS catalog_image_ref,
                      d.token AS draft_token,d.user_id AS draft_user_id,
                      d.source_image_ref AS draft_image_ref,d.status AS draft_status,
                      d.version AS draft_version,d.workflow_version,
                      d.confirmed_log_id,d.approved_log_id,d.confirmed_by
                      {timeline_columns}
               FROM vip_health_check_cases c
               JOIN vip_health_check_source_refs sr ON sr.case_id=c.case_id
               JOIN food_logs fl ON fl.log_id=sr.food_log_id
               JOIN food_catalog fc ON fc.food_id=fl.food_id
               JOIN pending_meal_photo_drafts d
                 ON d.user_id=c.user_id AND d.source_image_ref=fl.source_image_ref
                AND (d.confirmed_log_id=fl.log_id OR d.approved_log_id=fl.log_id)
               WHERE c.case_id=? AND sr.food_log_id=?""",
            (case_id, log_id),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        if (
            row["case_status"] not in IMAGE_VIEWABLE_CASE_STATUSES
            or row["case_user_id"] != row["log_user_id"]
            or row["case_user_id"] != row["draft_user_id"]
            or row["case_user_id"] != row["catalog_owner_user_id"]
            or row["catalog_source_type"] != "user_meal_photo"
            or row["food_log_id"] != row["log_id"]
            or row["food_log_version"] != row["version"]
            or row["confirmation_status"] != "confirmed"
            or str(row["deleted_at"] or "")
            or not str(row["source_image_ref"] or "")
            or row["source_image_ref"] != row["draft_image_ref"]
            or row["source_image_ref"] != row["catalog_image_ref"]
            or not _source_hash_matches(conn, row)
        ):
            return None
        workflow = row["workflow_version"]
        if workflow in {
            "user_confirmed_ai_estimate_v1", "user_confirmed_ai_nutrition_v2"
        }:
            if (
                row["draft_status"] != "user_confirmed"
                or row["confirmed_log_id"] != log_id
                or row["confirmed_by"] != row["case_user_id"]
                or not user_confirmed_meal_photo_estimate_is_valid(
                    conn, log_id,
                    expected_user_id=row["case_user_id"],
                    expected_draft_token=row["draft_token"],
                    expected_draft_version=row["draft_version"],
                )
            ):
                return None
        elif workflow == "expert_review_v1":
            if row["draft_status"] != "approved" or row["approved_log_id"] != log_id:
                return None
        else:
            return None
        return read_bounded_preview(image_root, row["source_image_ref"])
    except (sqlite3.Error, TypeError, ValueError, ImageUnavailable):
        return None


def _response(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=NO_STORE_HEADERS)


def create_dietitian_health_check_router(
    *,
    channel_id: str,
    allowed_uids: frozenset[str],
    list_loader: Callable[..., Mapping[str, object]],
    detail_loader: Callable[[str], Mapping[str, object] | None],
    image_loader: Callable[[str, str], ImagePreview | None] | None = None,
    draft_saver: Callable[..., Mapping[str, object]] | None = None,
    allowed_uid_loader: Callable[[], frozenset[str]] | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
) -> APIRouter:
    if not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise ValueError("invalid LINE Login Channel ID")
    if not allowed_uids or any(not LINE_UID_PATTERN.fullmatch(uid) for uid in allowed_uids):
        raise ValueError("invalid dietitian allowlist")
    router = APIRouter()

    def authorize(
        authorization: str | None,
    ) -> tuple[JSONResponse | None, str | None]:
        scheme, separator, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            return _response("authentication required", 401), None
        try:
            subject = token_verifier(token.strip(), channel_id=channel_id)
        except LineAuthenticationError:
            return _response("authentication failed", 401), None
        except LineAuthenticationUnavailable:
            return _response("authentication unavailable", 503), None
        except Exception:
            return _response("authentication unavailable", 503), None
        try:
            current_allowed = allowed_uids if allowed_uid_loader is None else allowed_uid_loader()
        except Exception:
            return _response("authorization unavailable", 503), None
        if subject not in current_allowed:
            return _response("forbidden", 403), None
        return None, subject

    @router.get("/api/dietitian/health-checks", response_class=JSONResponse)
    def list_cases(
        authorization: str | None = Header(default=None),
        status: list[str] | None = Query(default=None),
        limit: str = Query(default="25"),
        offset: str = Query(default="0"),
    ) -> JSONResponse:
        denied, _actor_id = authorize(authorization)
        if denied is not None:
            return denied
        statuses = tuple(status or sorted(CASE_STATUSES))
        if (
            not statuses
            or any(item not in CASE_STATUSES for item in statuses)
            or len(statuses) > len(CASE_STATUSES)
            or len(set(statuses)) != len(statuses)
            or len(limit) > 3
            or len(offset) > 5
            or not re.fullmatch(r"[0-9]+", limit)
            or not re.fullmatch(r"[0-9]+", offset)
            or not 1 <= int(limit) <= 100
            or not 0 <= int(offset) <= 10_000
        ):
            return _response("invalid query", 422)
        try:
            payload = list_loader(statuses=statuses, limit=int(limit), offset=int(offset))
            return JSONResponse(payload, headers=NO_STORE_HEADERS)
        except Exception:
            return _response("health-check data unavailable", 503)

    @router.get("/api/dietitian/health-checks/{case_id}", response_class=JSONResponse)
    def case_detail(
        case_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        denied, _actor_id = authorize(authorization)
        if denied is not None:
            return denied
        if not CASE_ID_PATTERN.fullmatch(case_id):
            return _response("invalid case id", 422)
        try:
            payload = detail_loader(case_id)
            if payload is None:
                return _response("health-check case not found", 404)
            return JSONResponse(payload, headers=NO_STORE_HEADERS)
        except Exception:
            return _response("health-check data unavailable", 503)

    if draft_saver is not None:
        @router.post(
            "/api/dietitian/health-checks/{case_id}/reviews",
            response_class=JSONResponse,
        )
        async def save_draft(
            case_id: str,
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            denied, actor_id = authorize(authorization)
            if denied is not None:
                return denied
            assert actor_id is not None
            if not CASE_ID_PATTERN.fullmatch(case_id):
                return _response("invalid case id", 422)
            content_length = request.headers.get("content-length", "")
            if content_length and (not content_length.isdigit() or int(content_length) > 20_000):
                return _response("invalid draft", 422)
            body = await request.body()
            if len(body) > 20_000:
                return _response("invalid draft", 422)
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _response("invalid draft", 422)
            allowed = {
                "good", "priority", "next_7_days", "comment",
                "expected_source_token", "expected_review_version", "request_id",
            }
            if not isinstance(payload, dict) or set(payload) != allowed:
                return _response("invalid draft", 422)
            fields = {
                key: payload[key]
                for key in ("good", "priority", "next_7_days", "comment")
            }
            if any(
                not isinstance(value, str) or not value.strip() or len(value) > 4000
                for value in fields.values()
            ):
                return _response("invalid draft", 422)
            expected_source_token = payload["expected_source_token"]
            expected_review = payload["expected_review_version"]
            request_id = payload["request_id"]
            if (
                not isinstance(expected_source_token, str)
                or CANONICAL_SHA256_PATTERN.fullmatch(expected_source_token) is None
                or isinstance(expected_review, bool)
                or not isinstance(expected_review, int)
                or expected_review < 0
                or not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id)
            ):
                return _response("invalid draft", 422)
            try:
                result = draft_saver(
                    case_id, fields, expected_source_token, expected_review, request_id,
                    actor_id,
                )
            except DraftNotFound:
                return _response("health-check case not found", 404)
            except DraftConflict:
                return _response("draft is stale or frozen", 409)
            except Exception:
                return _response("health-check data unavailable", 503)
            public_result = {
                key: result[key]
                for key in ("review_id", "review_version", "status", "created")
                if key in result
            }
            return JSONResponse(public_result, headers=NO_STORE_HEADERS)

    if image_loader is not None:
        @router.get(
            "/api/dietitian/health-checks/{case_id}/sources/{log_id}/image",
            response_class=Response,
        )
        def source_image(
            case_id: str,
            log_id: str,
            authorization: Any = Header(default=None),
        ) -> Response:
            denied, _actor_id = authorize(authorization)
            if denied is not None:
                return denied
            if not CASE_ID_PATTERN.fullmatch(case_id) or not CASE_ID_PATTERN.fullmatch(log_id):
                return _response("image not found", 404)
            try:
                preview = image_loader(case_id, log_id)
            except Exception:
                preview = None
            if preview is None:
                return _response("image not found", 404)
            return Response(
                content=preview.data,
                media_type=preview.media_type,
                headers=NO_STORE_HEADERS,
            )

    def method_not_allowed() -> JSONResponse:
        return JSONResponse(
            {"detail": "method not allowed"},
            status_code=405,
            headers={**NO_STORE_HEADERS, "Allow": "GET"},
        )

    unsupported_methods = [
        "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"
    ]
    router.add_api_route(
        "/api/dietitian/health-checks",
        method_not_allowed,
        methods=unsupported_methods,
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/dietitian/health-checks/{case_id}",
        method_not_allowed,
        methods=unsupported_methods,
        include_in_schema=False,
    )
    if draft_saver is not None:
        def draft_method_not_allowed() -> JSONResponse:
            return JSONResponse(
                {"detail": "method not allowed"}, status_code=405,
                headers={**NO_STORE_HEADERS, "Allow": "POST"},
            )

        router.add_api_route(
            "/api/dietitian/health-checks/{case_id}/reviews",
            draft_method_not_allowed,
            methods=["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"],
            include_in_schema=False,
        )

    return router


def attach_dietitian_health_check_routes(
    app: Any,
    *,
    config: DietitianHealthCheckConfig,
    list_loader: Callable[..., Mapping[str, object]],
    detail_loader: Callable[[str], Mapping[str, object] | None],
    image_loader: Callable[[str, str], ImagePreview | None] | None = None,
    draft_saver: Callable[..., Mapping[str, object]] | None = None,
    allowed_uid_loader: Callable[[], frozenset[str]] | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
) -> bool:
    if not config.enabled:
        return False

    @app.middleware("http")
    async def enforce_dietitian_health_check_read_only(request: Any, call_next: Any):
        path = str(request.url.path)
        protected_prefix = "/api/dietitian/health-checks"
        is_draft_post = (
            draft_saver is not None
            and request.method == "POST"
            and re.fullmatch(
                r"/api/dietitian/health-checks/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/reviews",
                path,
            ) is not None
        )
        if request.method != "GET" and not is_draft_post and (
            path == protected_prefix or path.startswith(protected_prefix + "/")
        ):
            return JSONResponse(
                {"detail": "method not allowed"},
                status_code=405,
                headers={**NO_STORE_HEADERS, "Allow": "GET"},
            )
        return await call_next(request)

    app.include_router(
        create_dietitian_health_check_router(
            channel_id=config.channel_id,
            allowed_uids=config.allowed_uids,
            list_loader=list_loader,
            detail_loader=detail_loader,
            image_loader=image_loader,
            draft_saver=draft_saver,
            allowed_uid_loader=allowed_uid_loader,
            token_verifier=token_verifier,
        )
    )
    return True
