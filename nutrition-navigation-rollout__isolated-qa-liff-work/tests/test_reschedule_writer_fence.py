import ast
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import server
from workbook_write_lease import (
    FULL_REQUIRED_WRITER_INVENTORY,
    acquire_workbook_lease,
    configure_workbook_writer_capability,
    ensure_workbook_lease_schema,
)


NOW = datetime(2026, 9, 26, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
WORKBOOK = "offline-workbook"
WRITERS = FULL_REQUIRED_WRITER_INVENTORY


def _db(tmp_path):
    path = tmp_path / "writer-fence.sqlite3"
    with sqlite3.connect(path) as conn:
        ensure_workbook_lease_schema(conn)
        configure_workbook_writer_capability(
            conn,
            workbook_id=WORKBOOK,
            controlled_writers=WRITERS,
            external_writer_state="controlled",
            cutover_evidence="offline-test-only",
        )
        conn.commit()
    return path


def test_default_off_preserves_legacy_behavior_without_db_or_lease(tmp_path):
    missing = tmp_path / "must-not-be-created.sqlite3"
    calls = []

    with server.server_workbook_write_fence(
        db_path=str(missing), workbook_id=WORKBOOK,
        writer_id="meal_swap", operation_id="off-1",
        enabled=False, now=NOW,
    ) as fence:
        fence.mark_write_started()
        calls.append("legacy-write")
        fence.confirm()

    assert calls == ["legacy-write"]
    assert not missing.exists()


def test_enabled_incomplete_writer_inventory_fails_closed_before_batch(tmp_path):
    db = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(db) as conn:
        ensure_workbook_lease_schema(conn)
        configure_workbook_writer_capability(
            conn, workbook_id=WORKBOOK, controlled_writers=("meal_swap",),
            external_writer_state="controlled", cutover_evidence="incomplete",
        )
        conn.commit()
    batches = []

    with pytest.raises(RuntimeError, match="inventory is incomplete"):
        with server.server_workbook_write_fence(
            db_path=str(db), workbook_id=WORKBOOK,
            writer_id="meal_swap", operation_id="blocked-incomplete",
            enabled=True, now=NOW,
        ):
            batches.append("must-not-run")

    assert batches == []


def test_two_real_connections_allow_only_one_server_writer_and_zero_extra_batch(tmp_path):
    import threading

    db = _db(tmp_path)
    batches = []
    errors = []

    def competing_writer():
        try:
            with server.server_workbook_write_fence(
                db_path=str(db), workbook_id=WORKBOOK,
                writer_id="meal_swap", operation_id="swap-1",
                enabled=True, now=NOW,
            ):
                batches.append("must-not-run")
        except Exception as exc:
            errors.append(exc)

    with server.server_workbook_write_fence(
        db_path=str(db), workbook_id=WORKBOOK,
        writer_id="subscription_formalization", operation_id="formalize-1",
        enabled=True, now=NOW,
    ):
        worker = threading.Thread(target=competing_writer)
        worker.start()
        worker.join()

    assert len(errors) == 1
    assert "already leased" in str(errors[0])
    assert batches == []


def test_post_write_failure_keeps_unknown_and_blocks_later_real_server_mutator(tmp_path):
    db = _db(tmp_path)
    batches = []

    class Spreadsheet:
        def batch_update(self, body):
            batches.append(body)
            raise TimeoutError("response lost")

    with server.server_workbook_write_fence(
        db_path=str(db), workbook_id=WORKBOOK,
        writer_id="deferred_meal", operation_id="defer-1",
        enabled=True, now=NOW,
    ):
        assert server._atomic_update_meal_cells(
            Spreadsheet(), type("Worksheet", (), {"id": 7})(), ((1, 2, "meal"),)
        ) == "unknown"

    with pytest.raises(RuntimeError, match="already leased"):
        with server.server_workbook_write_fence(
            db_path=str(db), workbook_id=WORKBOOK,
            writer_id="meal_swap", operation_id="swap-after-unknown",
            enabled=True, now=NOW,
        ):
            batches.append("must-not-run")

    assert len(batches) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id=?",
            (WORKBOOK,),
        ).fetchone() == ("active", None)


def test_reentry_uses_unforgeable_active_context_without_second_claim(tmp_path):
    db = _db(tmp_path)

    with server.server_workbook_write_fence(
        db_path=str(db), workbook_id=WORKBOOK,
        writer_id="subscription_formalization", operation_id="formalize-2",
        enabled=True, now=NOW,
    ) as outer:
        with server.server_workbook_write_fence(
            db_path=str(db), workbook_id=WORKBOOK,
            writer_id="master_api_mutation", operation_id="nested-master",
            enabled=True, now=NOW,
        ) as nested:
            assert nested is outer
        outer.confirm()

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id=?",
            (WORKBOOK,),
        ).fetchone() == ("released", "confirmed")


def test_actual_high_risk_server_entries_refuse_before_legacy_google_calls(tmp_path, monkeypatch):
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        acquire_workbook_lease(
            conn, workbook_id=WORKBOOK, writer_id="pair_reschedule",
            operation_id="reschedule-in-flight", now=NOW, ttl_seconds=120,
        )
    calls = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "SPREADSHEET_ID", WORKBOOK)
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "tw_now", lambda: NOW)
    monkeypatch.setattr(server, "_formalize_subscription_snapshot_unfenced", lambda *_: calls.append("formalize"))
    monkeypatch.setattr(server, "_execute_deferred_meal_move_unfenced", lambda *_a, **_k: calls.append("defer"))
    monkeypatch.setattr(server, "_execute_meal_swap_unfenced", lambda *_a, **_k: calls.append("swap"))
    monkeypatch.setattr(server, "_repack_meal_plan_for_user_unfenced", lambda *_: calls.append("master"))

    assert server.formalize_subscription_snapshot(1, {})[0] is False
    assert server.execute_deferred_meal_move("U", "9/27", "午餐", "9/28", "午餐", request_id="1")[0] is False
    assert "未執行" in server.execute_meal_swap("U", "9/27", "午餐", "9/28", "午餐", operation_id="line-ai:U:E1")
    assert server.repack_meal_plan_for_user("U")[0] is False
    assert calls == []


def test_actual_high_risk_server_entries_keep_default_off_delegation(tmp_path, monkeypatch):
    missing = tmp_path / "off-entry.sqlite3"
    calls = []
    monkeypatch.setattr(server, "DB_PATH", str(missing))
    monkeypatch.setattr(server, "SPREADSHEET_ID", WORKBOOK)
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", False)
    monkeypatch.setattr(server, "tw_now", lambda: NOW)
    monkeypatch.setattr(server, "_formalize_subscription_snapshot_unfenced", lambda *_: calls.append("formalize") or (True, "formalized"))
    monkeypatch.setattr(server, "_execute_deferred_meal_move_unfenced", lambda *_a, **_k: calls.append("defer") or (True, "deferred"))
    monkeypatch.setattr(server, "_execute_meal_swap_unfenced", lambda *_a, **_k: calls.append("swap") or "✅ swapped")
    monkeypatch.setattr(server, "_repack_meal_plan_for_user_unfenced", lambda *_: calls.append("master") or (True, "repacked"))

    assert server.formalize_subscription_snapshot(1, {}) == (True, "formalized")
    assert server.execute_deferred_meal_move("U", "9/27", "午餐", "9/28", "午餐", request_id="1") == (True, "deferred")
    assert server.execute_meal_swap("U", "9/27", "午餐", "9/28", "午餐", operation_id="line-ai:U:E1") == "✅ swapped"
    assert server.repack_meal_plan_for_user("U") == (True, "repacked")
    assert calls == ["formalize", "defer", "swap", "master"]
    assert not missing.exists()


def test_formalization_customer_list_failure_is_unknown_not_swallowed(tmp_path, monkeypatch):
    db = _db(tmp_path)

    class BrokenClient:
        def open_by_key(self, _key):
            raise TimeoutError("provider outcome unavailable")

    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "gc", BrokenClient())
    with pytest.raises(RuntimeError, match="結果不明"):
        with server.server_workbook_write_fence(
            db_path=str(db), workbook_id=WORKBOOK,
            writer_id="subscription_formalization", operation_id="formalize-gap",
            enabled=True, now=NOW,
        ):
            server.sync_customer_sheet("U1", "name", "active", 1, "2026-10-01", 1800)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status,final_outcome FROM workbook_write_leases WHERE workbook_id=?",
            (WORKBOOK,),
        ).fetchone() == ("active", None)


def test_uncontrolled_training_and_nutrition_entries_fail_explicitly_before_sheet(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "gc", type("GC", (), {
        "open_by_key": lambda *_: calls.append("sheet")
    })())
    monkeypatch.setattr(server, "sh", type("SH", (), {
        "worksheet": lambda *_: calls.append("sheet")
    })())

    payload = server.UpdateGroupPayload(
        admin_uid=next(iter(server.COACH_UIDS)), target_uid="U1", new_group="1"
    )
    import asyncio
    result = asyncio.run(server.update_student_group(payload))
    assert result["success"] is False
    assert "本次未寫入" in result["error"]
    with pytest.raises(RuntimeError, match="本次未寫入"):
        server._nutrition_ws("foods")
    assert calls == []


def test_ast_reviewed_sheet_mutators_are_gated_or_trusted_inner():
    """Keep the reviewed server Sheet-writer inventory machine checkable."""
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    directly_gated = {
        "setup_garmin_test_sheet", "write_workout_to_sheet", "sync_customer_sheet",
        "update_student_group", "get_or_create_training_assignments_sheet",
        "receive_form_data", "sync_user_sheet_from_master",
        "update_4week_plan_background", "background_log_workout_to_sheet",
        "get_ai_response_with_memory", "replace_schedule", "_upsert_master_api_rows",
        "run_weekly_coach", "_nutrition_ws", "_upsert_raw_sheet_row",
        "_handle_message_impl", "auto_daily_evening_report", "get_lobster_targets",
    }
    trusted_inner = {
        "_atomic_update_meal_cells", "_repack_meal_plan_for_user_unfenced",
        "_formalize_subscription_snapshot_unfenced",
        # These obtain their worksheet only through the gated helper.
        "append_group_training_plan", "append_individual_training_plan",
        "append_weekly_group_training_plan",
    }
    sheet_receivers = {
        "workbook", "worksheet", "ws", "wks", "ss", "sheet", "main_sheet",
        "user_sheet", "api_sheet", "spreadsheet", "users_sheet",
    }
    mutators = {
        "update", "batch_update", "append_row", "append_rows", "clear",
        "delete_rows", "update_cell", "add_worksheet",
    }
    found = set()
    for name, function in functions.items():
        for call in ast.walk(function):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            receiver = call.func.value
            root = receiver.id if isinstance(receiver, ast.Name) else ""
            if call.func.attr in mutators and root in sheet_receivers:
                found.add(name)
    assert found <= directly_gated | trusted_inner
    for name in directly_gated:
        assert name in functions
        assert any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_require_controlled_workbook_writer"
            for call in ast.walk(functions[name])
        ), f"{name} is a reviewed Sheet mutator without the enabled-mode gate"
