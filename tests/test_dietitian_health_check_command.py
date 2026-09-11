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
    ):
        assert is_dietitian_health_check_command_intent(variant) is True

    for normal_text in ("#生活", "營養師健檢", "我想找營養師健檢", "#營養師"):
        assert is_dietitian_health_check_command_intent(normal_text) is False

    for injected in ("\u034f", "\ufe0f", "\U000e0100", "\u200b", "\u2060"):
        for index in range(1, len(COMMAND_TEXT)):
            variant = COMMAND_TEXT[:index] + injected + COMMAND_TEXT[index:]
            assert is_dietitian_health_check_command_intent(variant) is True


def test_dietitian_command_intent_does_not_match_natural_hashtag_sentences():
    for normal_text in (
        "#營養方面想請教師傅，健身後需要檢查嗎？",
        "#營養補充想請教師傅；我有健身，想檢查飲食。",
    ):
        assert is_dietitian_health_check_command_intent(normal_text) is False


def test_dietitian_command_intent_fails_closed_for_long_ignorable_prefixes():
    for ignored in (" ", "\n", "\u200b", "\U000e0100"):
        assert is_dietitian_health_check_command_intent(
            ignored * 129 + COMMAND_TEXT
        ) is True


def test_dietitian_command_intent_ignores_visual_unicode_blanks():
    for blank in ("\u115f", "\u1160", "\u3164", "\u2800"):
        for index in range(1, len(COMMAND_TEXT)):
            variant = COMMAND_TEXT[:index] + blank + COMMAND_TEXT[index:]
            assert is_dietitian_health_check_command_intent(variant) is True


def test_dietitian_command_intent_ignores_inserted_hash_confusables():
    for inserted_hash in ("#", "＃"):
        for repeat in (1, 129):
            for index in range(len(COMMAND_TEXT) + 1):
                variant = (
                    COMMAND_TEXT[:index]
                    + inserted_hash * repeat
                    + COMMAND_TEXT[index:]
                )
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
