"""Fail-closed LINE command entry for the dietitian health-check LIFF."""

from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata

from linebot.models import FlexSendMessage


COMMAND_TEXT = "#營養師健檢"
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


def is_dietitian_health_check_command_intent(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or ""))
    start = 0
    while start < len(normalized):
        category = unicodedata.category(normalized[start])
        if not (normalized[start].isspace() or category[0] in {"C", "M"}):
            break
        start += 1
    if start >= len(normalized) or normalized[start] != COMMAND_TEXT[0]:
        return False

    command_index = 0
    for char in normalized[start:]:
        if char == COMMAND_TEXT[command_index]:
            command_index += 1
            if command_index == len(COMMAND_TEXT):
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
