import json

import pytest

from dietitian_health_check_command import (
    COMMAND_TEXT,
    build_dietitian_health_check_flex,
    is_authorized_dietitian_health_check_command,
    is_dietitian_health_check_command_intent,
    load_dietitian_health_check_command_allowed_uids,
    load_dietitian_health_check_command_liff_id,
)


LIFF_ID = "2011528194-EsxeCZ2a"
DIETITIAN_UID = "U1234567890abcdef1234567890abcdef"
OTHER_UID = "Uabcdef1234567890abcdef1234567890"


def test_command_configuration_is_optional_but_rejects_malformed_values():
    assert load_dietitian_health_check_command_liff_id({}) == ""
    assert load_dietitian_health_check_command_allowed_uids({}) == frozenset()
    assert load_dietitian_health_check_command_liff_id(
        {"DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID": LIFF_ID}
    ) == LIFF_ID
    assert load_dietitian_health_check_command_allowed_uids(
        {
            "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS": (
                f"{DIETITIAN_UID},{OTHER_UID}"
            )
        }
    ) == frozenset({DIETITIAN_UID, OTHER_UID})

    for malformed in (
        "https://liff.line.me/2011528194-EsxeCZ2a",
        "2011528194",
        "2011528194-x",
        "../../secret",
    ):
        with pytest.raises(ValueError, match="DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID"):
            load_dietitian_health_check_command_liff_id(
                {"DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID": malformed}
            )

    for malformed in ("not-a-line-uid", "U123", "U" + "g" * 32):
        with pytest.raises(
            ValueError, match="DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS"
        ):
            load_dietitian_health_check_command_allowed_uids(
                {"DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS": malformed}
            )


def test_only_exact_allowlisted_command_is_authorized_when_liff_is_configured():
    assert COMMAND_TEXT == "#營養師健檢"
    allowed_uids = frozenset({DIETITIAN_UID})
    assert is_authorized_dietitian_health_check_command(
        DIETITIAN_UID,
        COMMAND_TEXT,
        allowed_uids=allowed_uids,
        liff_id=LIFF_ID,
    ) is True

    for user_id, message, liff_id in (
        (OTHER_UID, COMMAND_TEXT, LIFF_ID),
        (DIETITIAN_UID, "＃營養師健檢", LIFF_ID),
        (DIETITIAN_UID, "# 營養師健檢", LIFF_ID),
        (DIETITIAN_UID, "#營養師 健檢", LIFF_ID),
        (DIETITIAN_UID, "#營養師健檢現在", LIFF_ID),
        (DIETITIAN_UID, COMMAND_TEXT, ""),
    ):
        assert is_authorized_dietitian_health_check_command(
            user_id,
            message,
            allowed_uids=allowed_uids,
            liff_id=liff_id,
        ) is False


def test_dietitian_command_intent_catches_variants_without_matching_normal_text():
    for variant in (
        "#營養師健檢",
        " #營養師健檢",
        "#營養師健檢 ",
        "\t#營養師健檢\n",
        "＃營養師健檢",
        "# 營養師健檢",
        "#營養師 健檢",
        "#營養師\u200b健檢",
        "#營養師\u034f健檢",
        "#營養師\ufe0f健檢",
        "#營養師\U000e0100健檢",
        "#營養師健檢現在",
        "#營養師健撿",
        "##營養師健檢",
        "#營養師\u3164健檢",
        "#" + ("\u200b" * 256),
        ("\u200b" * 128) + "#營養師健檢",
        "！#營養師健檢",
        "#營養師健",
        "#營養x師健撿",
        "#養師健檢",
        "#榮養師健檢",
        "#營養師健撿現在請處理",
        "#養師健檢現在請處理",
        "#生活#營養師健檢",
        "正常文字#營養師健檢",
        "#今天跑步#營養師健檢",
        "#營ﬃ養師健檢",
        "#營養#師健檢",
        "#營養﹟師健檢",
        "#營養＃師健檢",
    ):
        assert is_dietitian_health_check_command_intent(variant) is True

    for normal_text in (
        "#生活",
        "#生活營養師健檢",
        "#營養師健康資訊",
        "#營養師健康飲食",
        "營養師健檢",
        "我想找營養師健檢",
        "#營養師",
        (" " * 128) + "今天天氣很好",
        "#今天跑步心得" + ("很棒" * 100),
        "#營養師建議",
        "#營養師健身",
        "#營養師料理",
        "#營養師營養建議",
        "#今天跑步" + (" " * 128),
        "#今天跑步" + (" " * 129),
        "#今天跑步" + ("\t" * 128),
        "#今天跑步" + ("\n" * 128),
        "#今天跑步" + ("\r" * 128),
    ):
        assert is_dietitian_health_check_command_intent(normal_text) is False

    for injected in ("\u034f", "\ufe0f", "\U000e0100", "\u200b", "\u2060"):
        for index in range(1, len(COMMAND_TEXT)):
            variant = COMMAND_TEXT[:index] + injected + COMMAND_TEXT[index:]
            assert is_dietitian_health_check_command_intent(variant) is True


def test_flex_opens_only_the_configured_liff_url():
    message = build_dietitian_health_check_flex(LIFF_ID)
    rendered = json.loads(message.as_json_string())
    text = json.dumps(rendered, ensure_ascii=False)

    assert rendered["type"] == "flex"
    assert rendered["altText"] == "開啟營養師三日健檢"
    assert "三日健檢" in text
    assert "https://liff.line.me/2011528194-EsxeCZ2a" in text
    assert "chat_message.write" not in text
