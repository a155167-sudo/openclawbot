import pytest

from gspread_pair_reschedule_adapter import (
    PairSheetConflict,
    PersistedMasterProfile,
    build_target_master_row,
)


def profile(*, coaching="0", carb="0", plan_week=""):
    return PersistedMasterProfile(
        owner_user_id="owner", tdee="1800", tomorrow_training="",
        is_coaching_enabled=coaching, plan_type="普通餐", sport_type="",
        plan_week=plan_week, intervals_id="", intervals_api_key="",
        training_freq="", normal_train_time="", long_train_day="",
        run_pace="", bike_ftp="", swim_pace="", user_level="",
        race_date="", is_carb_cycling_enabled=carb,
    )


def target_row(profile_value):
    return build_target_master_row(
        target_date="2026-10-01", owner_user_id="owner",
        lunch_item="午餐", dinner_item="晚餐", profile=profile_value,
    )


def test_static_plan_preserves_blank_calendar():
    row = target_row(profile())
    assert row[9] == ""
    assert row[6] == "0"
    assert row[20] == "0"


@pytest.mark.parametrize("coaching,carb", [("1", "0"), ("0", "1")])
def test_dynamic_plan_requires_calendar(coaching, carb):
    with pytest.raises(PairSheetConflict, match="calendar training/weekday metadata"):
        target_row(profile(coaching=coaching, carb=carb))


def test_nonempty_calendar_still_passes():
    assert target_row(profile(plan_week="第1週-一"))[9] == "第1週-一"
