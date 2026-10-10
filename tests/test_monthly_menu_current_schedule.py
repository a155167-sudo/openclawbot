"""包月菜單 must show the confirmed current schedule, including rescheduled meals."""
import sqlite3

from normal_reschedule_context import current_order_schedule_rows
from normal_reschedule_policy import normal_pair_policy
from pair_reschedule_coordinator import verify_admin_context
from reschedule_service_integration import (
    approve_customer_pair_reschedule, submit_customer_pair_reschedule_pending,
    verify_customer_reschedule_context,
)
from subscription_meal_plan import render_schedule_menu_text
from test_normal_reschedule_integration import TARGET, normal_db
from test_pair_reschedule_coordinator import ADMIN, NOW, OWNER, SOURCE
from test_reschedule_service_integration import sheet_fixture


def _cols(day, lunch, dinner):
    cols = [""] * 14
    cols[0], cols[2], cols[5] = day.replace("-", "/"), lunch, dinner
    return cols


def test_render_uses_weekday_blocks_and_skips_vacated_dates():
    text = render_schedule_menu_text([
        ("2026-10-13", _cols("2026-10-13", "豬肉低碳", "雞肉低碳")),
        ("2026-10-12", _cols("2026-10-12", "", "")),          # moved away
        ("2026-10-14", _cols("2026-10-14", "鮭魚食蔬", "")),  # one meal left
    ])
    assert text == (
        "2026/10/13（週二）\n午：豬肉低碳\n晚：雞肉低碳\n\n"
        "2026/10/14（週三）\n午：鮭魚食蔬\n晚：無"
    )


def _move(conn, sheet, request_id, source, target):
    context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule_pending(
        conn, context=context, source_date=source, target_date=target,
        request_id=request_id, now=NOW, feature_enabled=True)
    return approve_customer_pair_reschedule(
        conn, sheet, request_id=request_id, admin_context=verify_admin_context(conn, ADMIN),
        now=NOW, feature_enabled=True, policy_validator=normal_pair_policy)


def _menu(conn):
    conn.row_factory = sqlite3.Row
    rows = current_order_schedule_rows(conn, order_id=1, owner_user_id=OWNER)
    return None if rows is None else render_schedule_menu_text(rows)


def test_menu_before_reschedule_is_published_baseline(tmp_path):
    conn = normal_db(tmp_path / "base.sqlite3")
    text = _menu(conn)
    assert SOURCE.replace("-", "/") in text and TARGET.replace("-", "/") not in text


def test_menu_after_confirmed_reschedule_shows_new_date(tmp_path):
    conn = normal_db(tmp_path / "moved.sqlite3")
    sheet, _ = sheet_fixture()
    before = _menu(conn)
    source_block = next(b for b in before.split("\n\n") if b.startswith(SOURCE.replace("-", "/")))
    meals = source_block.split("\n", 1)[1]
    assert _move(conn, sheet, "RS_menu_1", SOURCE, TARGET).status == "confirmed"
    after = _menu(conn)
    assert SOURCE.replace("-", "/") not in after
    target_block = next(b for b in after.split("\n\n") if b.startswith(TARGET.replace("-", "/")))
    assert target_block.split("\n", 1)[1] == meals


def test_menu_unknown_while_sheet_outcome_is_uncertain(tmp_path):
    conn = normal_db(tmp_path / "unknown.sqlite3")
    sheet, book = sheet_fixture()
    book.outcome = "apply_then_timeout"
    assert _move(conn, sheet, "RS_menu_u", SOURCE, TARGET).status == "sheet_unknown"
    assert _menu(conn) is None  # caller falls back; never shows a guessed schedule
