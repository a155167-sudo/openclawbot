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
from typing import Any, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse, Response
import requests

from vip_health_check import canonical_food_log_source_hash


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
    {"good", "priority", "strengths", "improvements", "recommendations", "summary"}
)
SUGGESTED_VALUE_FIELDS = frozenset(
    {
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g", "fiber_g",
        "sodium_mg", "vegetable_servings", "water_ml",
    }
)
AI_TEXT_FIELDS = frozenset({"pattern", "observation"})
AI_TEXT_OR_LIST_FIELDS = AI_OBSERVATION_FIELDS - AI_TEXT_FIELDS
REVIEW_TEXT_FIELDS = frozenset({"good", "priority", "summary"})
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


def _local_date_text(value: object, field: str) -> str:
    text = _required_text(value, field, maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"invalid {field}")
    return text


def _source_hash_matches(log: sqlite3.Row) -> bool:
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
        return False
    raw_nutrition = log["nutrition_snapshot_json"]
    if not isinstance(raw_nutrition, str):
        return False
    actual = canonical_food_log_source_hash(log_id, version, raw_nutrition)
    return hmac.compare_digest(actual, expected)


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
    normalized_start = (
        window_started_at[:-1] + "+00:00"
        if window_started_at.endswith(("Z", "z"))
        else window_started_at
    )
    normalized_end = (
        window_ends_at[:-1] + "+00:00"
        if window_ends_at.endswith(("Z", "z"))
        else window_ends_at
    )
    try:
        invalid_window = datetime.fromisoformat(normalized_start) >= datetime.fromisoformat(
            normalized_end
        )
    except TypeError as exc:
        raise ValueError("invalid case window") from exc
    if invalid_window:
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
        qualified_day_rows = row["qualified_day_rows"]
        if (
            isinstance(qualified_day_rows, bool)
            or not isinstance(qualified_day_rows, int)
            or qualified_day_rows != item["valid_day_count"]
            or (
                item["status"] in THREE_DAY_REQUIRED_STATUSES
                and qualified_day_rows < 3
            )
        ):
            raise ValueError("invalid case list semantics")
        items.append(item)
    return {"items": items, "total": count, "limit": limit, "offset": offset}


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
    valid_days = conn.execute(
        """SELECT local_date,rule_version,qualifying_meal_count,completeness_status,evaluated_at
           FROM vip_health_check_valid_days WHERE case_id=? ORDER BY local_date""",
        (case_id,),
    ).fetchall()
    validated_days = []
    for day in valid_days:
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
        if completeness not in {"qualified", "incomplete"}:
            raise ValueError("invalid valid_day.completeness_status")
        validated_days.append(
            {
                "local_date": _local_date_text(
                    day["local_date"], "valid_day.local_date"
                ),
                "rule_version": _required_text(
                    day["rule_version"], "valid_day.rule_version", maximum=100
                ),
                "qualifying_meal_count": meal_count,
                "completeness_status": completeness,
                "evaluated_at": _iso_datetime_text(
                    day["evaluated_at"], "valid_day.evaluated_at"
                ),
            }
        )
    result["valid_days"] = validated_days

    qualified_count = sum(
        day["completeness_status"] == "qualified" for day in validated_days
    )
    if result["valid_day_count"] != qualified_count:
        raise ValueError("invalid case valid-day count")
    if result["status"] in THREE_DAY_REQUIRED_STATUSES and qualified_count < 3:
        raise ValueError("invalid case review threshold")
    window_start = datetime.fromisoformat(
        str(result["window_started_at"]).replace("Z", "+00:00").replace("z", "+00:00")
    )
    window_end = datetime.fromisoformat(
        str(result["window_ends_at"]).replace("Z", "+00:00").replace("z", "+00:00")
    )
    day_dates = [date.fromisoformat(str(day["local_date"])) for day in validated_days]
    if len(day_dates) != len(set(day_dates)) or any(
        not window_start.date() <= day_value < window_end.date() for day_value in day_dates
    ):
        raise ValueError("invalid valid-day window")

    source_refs = conn.execute(
        """SELECT food_log_id,food_log_version,local_date,source_hash
           FROM vip_health_check_source_refs WHERE case_id=? ORDER BY food_log_id""",
        (case_id,),
    ).fetchall()
    manifest_lines: list[str] = []
    valid_day_names = {str(day["local_date"]) for day in validated_days}
    for source_ref in source_refs:
        food_log_id = _required_text(
            source_ref["food_log_id"], "source_ref.food_log_id", maximum=128
        )
        version = source_ref["food_log_version"]
        source_hash = source_ref["source_hash"]
        local_date = _local_date_text(source_ref["local_date"], "source_ref.local_date")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(source_hash, str)
            or CANONICAL_SHA256_PATTERN.fullmatch(source_hash) is None
            or local_date not in valid_day_names
        ):
            raise ValueError("invalid source reference")
        manifest_lines.append(f"{food_log_id}:{version}:{source_hash}")
    manifest_lines.extend(
        f"day:{day['local_date']}:{day['qualifying_meal_count']}:{day['rule_version']}"
        for day in validated_days
    )
    expected_manifest = hashlib.sha256(
        "\n".join(manifest_lines).encode("utf-8")
    ).hexdigest()
    stored_manifest = row["source_manifest_hash"]
    empty_collecting_case = (
        result["status"] == "collecting"
        and not source_refs
        and not validated_days
        and stored_manifest == ""
    )
    if not empty_collecting_case and (
        not isinstance(stored_manifest, str)
        or CANONICAL_SHA256_PATTERN.fullmatch(stored_manifest) is None
        or not hmac.compare_digest(stored_manifest, expected_manifest)
    ):
        raise ValueError("invalid case source manifest")

    referenced_count = len(source_refs)
    logs = conn.execute(
        """SELECT fl.log_id,sr.food_log_version,sr.source_hash,
                  fl.nutrition_snapshot_json
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
        if not _source_hash_matches(log):
            continue
        version = log["food_log_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("invalid source_log.food_log_version")
        nutrition_snapshot = _project_typed_json_object(
            log["nutrition_snapshot_json"], numeric_fields=NUTRITION_FIELDS
        )
        if nutrition_snapshot is None:
            continue
        source_logs.append(
            {
                "log_id": _required_text(log["log_id"], "source_log.log_id", maximum=128),
                "food_log_version": version,
                "nutrition_snapshot": nutrition_snapshot,
            }
        )
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
    review_is_fresh = (
        review is not None
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


def _response(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=NO_STORE_HEADERS)


def create_dietitian_health_check_router(
    *,
    channel_id: str,
    allowed_uids: frozenset[str],
    list_loader: Callable[..., Mapping[str, object]],
    detail_loader: Callable[[str], Mapping[str, object] | None],
    image_loader: Callable[[str, str], tuple[bytes, str] | None] | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
) -> APIRouter:
    if not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise ValueError("invalid LINE Login Channel ID")
    if not allowed_uids or any(not LINE_UID_PATTERN.fullmatch(uid) for uid in allowed_uids):
        raise ValueError("invalid dietitian allowlist")
    router = APIRouter()

    def authorize(authorization: str | None) -> JSONResponse | None:
        scheme, separator, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            return _response("authentication required", 401)
        try:
            subject = token_verifier(token.strip(), channel_id=channel_id)
        except LineAuthenticationError:
            return _response("authentication failed", 401)
        except LineAuthenticationUnavailable:
            return _response("authentication unavailable", 503)
        except Exception:
            return _response("authentication unavailable", 503)
        if subject not in allowed_uids:
            return _response("forbidden", 403)
        return None

    @router.get("/api/dietitian/health-checks", response_class=JSONResponse)
    def list_cases(
        authorization: str | None = Header(default=None),
        status: list[str] | None = Query(default=None),
        limit: str = Query(default="25"),
        offset: str = Query(default="0"),
    ) -> JSONResponse:
        denied = authorize(authorization)
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
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        denied = authorize(authorization)
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

    @router.get("/api/dietitian/health-checks/{case_id}/photos/{log_id}")
    def source_photo(
        case_id: str,
        log_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> Response:
        denied = authorize(authorization)
        if denied is not None:
            return denied
        if (
            not CASE_ID_PATTERN.fullmatch(case_id)
            or not CASE_ID_PATTERN.fullmatch(log_id)
        ):
            return _response("invalid source photo id", 422)
        if image_loader is None:
            return _response("source photo not found", 404)
        try:
            loaded = image_loader(case_id, log_id)
            if loaded is None:
                return _response("source photo not found", 404)
            content, media_type = loaded
            if (
                not isinstance(content, bytes)
                or not 100 <= len(content) <= 1024 * 1024
                or media_type != "image/jpeg"
            ):
                raise ValueError("invalid source photo payload")
            return Response(
                content=content,
                media_type=media_type,
                headers=NO_STORE_HEADERS,
            )
        except Exception:
            return _response("source photo unavailable", 503)

    def method_not_allowed() -> JSONResponse:
        return JSONResponse(
            {"detail": "method not allowed"},
            status_code=405,
            headers={**NO_STORE_HEADERS, "Allow": "GET"},
        )

    route_handlers = (
        ("/api/dietitian/health-checks", list_cases),
        ("/api/dietitian/health-checks/{case_id}", case_detail),
        ("/api/dietitian/health-checks/{case_id}/photos/{log_id}", source_photo),
    )
    for path, handler in route_handlers:
        router.add_api_route(
            path + "/",
            handler,
            methods=["GET"],
            include_in_schema=False,
        )

    unsupported_methods = ["HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]
    for path, _handler in route_handlers:
        for route_path in (path, path + "/"):
            router.add_api_route(
                route_path,
                method_not_allowed,
                methods=unsupported_methods,
                include_in_schema=False,
            )

    return router


def attach_dietitian_health_check_routes(
    app: Any,
    *,
    config: DietitianHealthCheckConfig,
    list_loader: Callable[..., Mapping[str, object]],
    detail_loader: Callable[[str], Mapping[str, object] | None],
    image_loader: Callable[[str, str], tuple[bytes, str] | None] | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
) -> bool:
    if not config.enabled:
        return False
    app.include_router(
        create_dietitian_health_check_router(
            channel_id=config.channel_id,
            allowed_uids=config.allowed_uids,
            list_loader=list_loader,
            detail_loader=detail_loader,
            image_loader=image_loader,
            token_verifier=token_verifier,
        )
    )
    return True
