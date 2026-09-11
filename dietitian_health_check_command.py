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


def is_dietitian_health_check_command_intent(message: str) -> bool:
    raw_message = str(message or "")
    command_index = 0
    for raw_index, raw_char in enumerate(raw_message):
        if raw_index >= INTENT_PREFIX_SCAN_LIMIT:
            return True
        for char in unicodedata.normalize("NFKC", raw_char):
            category = unicodedata.category(char)
            if (
                char.isspace()
                or category[0] in {"C", "M"}
                or char in VISUAL_BLANK_CHARACTERS
                or (char == COMMAND_TEXT[0] and command_index > 0)
            ):
                continue
            if char != COMMAND_TEXT[command_index]:
                return False
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


def build_dietitian_health_check_ready_flex(
    liff_id: str, *, valid_day_count: int = 3
) -> FlexSendMessage:
    """建立三日資料完成後才推送的營養師工作台通知。"""
    validated_liff_id = load_dietitian_health_check_command_liff_id(
        {"DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID": liff_id}
    )
    if not validated_liff_id:
        raise ValueError("DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID 不可為空")
    if isinstance(valid_day_count, bool) or not isinstance(valid_day_count, int) or valid_day_count < 3:
        raise ValueError("valid_day_count 必須至少為3")
    return FlexSendMessage(
        alt_text="三日健檢資料已完成，請進入營養師工作台",
        contents={
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px",
                "contents": [
                    {
                        "type": "text", "text": "✅ 三日健檢資料已完成",
                        "weight": "bold", "size": "xl", "color": "#176B3A",
                    },
                    {
                        "type": "text",
                        "text": f"案件已累積{valid_day_count}個有效日，請進入工作台進行整體審查。",
                        "wrap": True, "margin": "md", "color": "#475569", "size": "sm",
                    },
                    {
                        "type": "text",
                        "text": "餐點不需逐筆核准；如有明顯辨識錯誤，再選擇性修正。",
                        "wrap": True, "margin": "md", "color": "#64748b", "size": "xs",
                    },
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "16px",
                "contents": [{
                    "type": "button", "style": "primary", "color": "#0F766E",
                    "action": {
                        "type": "uri", "label": "開啟三日健檢",
                        "uri": f"https://liff.line.me/{validated_liff_id}",
                    },
                }],
            },
        },
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
