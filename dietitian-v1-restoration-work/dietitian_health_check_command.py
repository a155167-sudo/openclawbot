"""Fail-closed LINE command entry for the dietitian health-check LIFF."""

from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata

from linebot.models import FlexSendMessage


COMMAND_TEXT = "#營養師健檢"
INTENT_PREFIX_SCAN_LIMIT = 128
VISUAL_BLANK_CHARACTERS = frozenset({"\u115f", "\u1160", "\u3164", "\u2800"})
LIFF_ID_PATTERN = re.compile(r"[0-9]{10,}-[A-Za-z0-9_-]{8,}")
LINE_UID_PATTERN = re.compile(r"U[0-9A-Fa-f]{32}")


def load_dietitian_health_check_command_liff_id(
    environ: Mapping[str, str],
) -> str:
    value = str(environ.get("DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID") or "").strip()
    if not value:
        return ""
    if not LIFF_ID_PATTERN.fullmatch(value):
        raise ValueError("DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID 格式無效")
    return value


def load_dietitian_health_check_command_allowed_uids(
    environ: Mapping[str, str],
) -> frozenset[str]:
    raw_value = str(
        environ.get("DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS") or ""
    ).strip()
    if not raw_value:
        return frozenset()
    values = tuple(item.strip() for item in raw_value.split(","))
    if any(not LINE_UID_PATTERN.fullmatch(item) for item in values):
        raise ValueError(
            "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS 格式無效"
        )
    return frozenset(values)


def _bounded_edit_distance(left: str, right: str, *, maximum: int) -> int:
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        if min(current) > maximum:
            return maximum + 1
        previous = current
    return previous[-1]


def _candidate_is_dietitian_health_check_intent(candidate: str) -> bool:
    target = COMMAND_TEXT[1:]
    if candidate.startswith(target):
        return True
    if target.startswith(candidate):
        return len(candidate) >= 4

    minimum_width = max(1, len(target) - 1)
    maximum_width = min(len(candidate), len(target) + 1)
    check_markers = frozenset({"檢", "撿", "检"})
    return any(
        candidate[width - 1] in check_markers
        and _bounded_edit_distance(candidate[:width], target, maximum=2) <= 2
        for width in range(minimum_width, maximum_width + 1)
    )


def _normalize_intent_text(message: str) -> str:
    normalized_units: list[str] = []
    for raw_char in str(message or ""):
        normalized = unicodedata.normalize("NFKC", raw_char)
        normalized_units.append(normalized if len(normalized) <= 1 else "\ufffd")
    return "".join(normalized_units)


def is_dietitian_health_check_command_intent(message: str) -> bool:
    normalized = _normalize_intent_text(message)
    if COMMAND_TEXT[0] not in normalized:
        return False

    segments = normalized.split(COMMAND_TEXT[0])[1:]
    if len(segments) > 64:
        return True
    for segment_index in range(len(segments)):
        # Ignore hashes inside a candidate while also testing each one as a fresh
        # namespace boundary.
        segment = "".join(segments[segment_index:])
        token: list[str] = []
        ignored_count = 0
        for char in segment:
            category = unicodedata.category(char)
            is_standard_whitespace = char.isspace()
            suspicious_blank = not is_standard_whitespace and (
                category[0] in {"C", "M"} or char in VISUAL_BLANK_CHARACTERS
            )
            if is_standard_whitespace or suspicious_blank:
                if suspicious_blank:
                    ignored_count += 1
                    if ignored_count >= INTENT_PREFIX_SCAN_LIMIT:
                        return True
                continue
            token.append(char)
        candidate = "".join(token)
        if candidate and _candidate_is_dietitian_health_check_intent(candidate):
            return True
    return False


def is_authorized_dietitian_health_check_command(
    user_id: str,
    message: str,
    *,
    allowed_uids: frozenset[str],
    liff_id: str,
) -> bool:
    return bool(
        liff_id
        and str(user_id or "") in allowed_uids
        and str(message or "") == COMMAND_TEXT
    )


def build_dietitian_health_check_flex(liff_id: str) -> FlexSendMessage:
    validated_liff_id = load_dietitian_health_check_command_liff_id(
        {"DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID": liff_id}
    )
    if not validated_liff_id:
        raise ValueError("DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID 不可為空")
    return FlexSendMessage(
        alt_text="開啟營養師三日健檢",
        contents={
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "20px",
                "contents": [
                    {
                        "type": "text",
                        "text": "營養師三日健檢",
                        "weight": "bold",
                        "size": "xl",
                        "color": "#1e293b",
                    },
                    {
                        "type": "text",
                        "text": "開啟唯讀工作台，查看待審核的三日飲食健檢資料。",
                        "wrap": True,
                        "margin": "md",
                        "color": "#64748b",
                        "size": "sm",
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "16px",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "color": "#0F766E",
                        "action": {
                            "type": "uri",
                            "label": "開啟三日健檢",
                            "uri": f"https://liff.line.me/{validated_liff_id}",
                        },
                    }
                ],
            },
        },
    )
