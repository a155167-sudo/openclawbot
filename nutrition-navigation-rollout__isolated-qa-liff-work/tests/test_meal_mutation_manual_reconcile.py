from __future__ import annotations

import json
import sqlite3

import pytest

import meal_mutation_ledger as ledger
from meal_mutation_ledger import (
    MealMutationBinding,
    ensure_meal_mutation_schema,
    inspect_meal_mutation_for_manual_reconcile,
    manually_reconcile_meal_mutation,
    mark_meal_mutation_unknown,
    reserve_meal_mutation,
)


HEALTH_PROFILE_DDL = """CREATE TABLE health_profile (
    user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL, goal TEXT,
    restrictions TEXT, summary_text TEXT, active_days TEXT, sheet_name TEXT,
    today_extra_pro INTEGER DEFAULT 0
)"""
DEFERRED_MEALS_DDL = """CREATE TABLE deferred_meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    customer_name TEXT DEFAULT '', original_date TEXT NOT NULL,
    original_meal_type TEXT NOT NULL, target_date TEXT NOT NULL,
    target_meal_type TEXT NOT NULL, is_cross_period INTEGER DEFAULT 0,
    has_conflict INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
    note TEXT DEFAULT '', created_at TEXT DEFAULT '', approved_at TEXT DEFAULT '',
    approved_by TEXT DEFAULT ''
)"""


def _install(path):
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_meal_mutation_schema(conn)
        conn.execute(HEALTH_PROFILE_DDL)
        conn.execute(DEFERRED_MEALS_DDL)
        conn.execute(
            "INSERT INTO health_profile(user_id,name,summary_text,sheet_name) VALUES (?,?,?,?)",
            ("owner-12345678", "Owner", "existing", "owner-sheet"),
        )


def _binding(*, suffix="1", purpose="swap", request_id="", cells=None):
    if cells is None:
        cells = ({"row_idx": 11, "col_idx": 3}, {"row_idx": 12, "col_idx": 5})
    return MealMutationBinding(
        operation_id=f"operation-{suffix}", event_id=f"event-{suffix}",
        owner_user_id="owner-12345678", purpose=purpose, request_id=request_id,
        payload={"d1": "2026-09-25", "m1": "午餐", "d2": "2026-09-26", "m2": "晚餐"},
        spreadsheet_id="sheet-1", worksheet_id=42, worksheet_name="owner-sheet",
        cells=cells, before=("Meal A", "Meal B" if purpose == "swap" else ""),
        after=("Meal B" if purpose == "swap" else "無", "Meal A"),
    )


def _reserve(path, binding, *, unknown=False):
    claim = reserve_meal_mutation(path, binding)
    assert claim.kind == "claimed"
    if unknown:
        mark_meal_mutation_unknown(
            path, binding.operation_id, claim.claim_token, binding.owner_user_id,
            "first unknown reason",
        )
    return claim.claim_token


def _defer(path):
    with sqlite3.connect(path) as conn:
        request_id = conn.execute(
            "INSERT INTO deferred_meals(user_id,original_date,original_meal_type,target_date,target_meal_type,status,note) "
            "VALUES (?,?,?,?,?,'pending','original note')",
            ("owner-12345678", "2026-09-25", "午餐", "2026-09-26", "晚餐"),
        ).lastrowid
    return _binding(suffix="defer", purpose="defer", request_id=str(request_id)), request_id


def _dump(path):
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump())


def _manual(path, binding, disposition, *, evidence="ticket-42: provider queue drained", admin="admin-7", confirmed=True):
    return manually_reconcile_meal_mutation(
        path, operation_id=binding.operation_id, owner_user_id=binding.owner_user_id,
        disposition=disposition, evidence_note=evidence, admin_uid=admin,
        no_outstanding_late_request_confirmed=confirmed,
    )


def test_read_only_inspection_masks_owner_and_never_exposes_claim_token(tmp_path):
    path = tmp_path / "inspect.sqlite"
    _install(path)
    binding = _binding()
    token = _reserve(path, binding, unknown=True)
    before = _dump(path)

    view = inspect_meal_mutation_for_manual_reconcile(
        path, operation_id=binding.operation_id, owner_user_id=binding.owner_user_id
    )

    assert view.operation_id == binding.operation_id
    assert view.owner_masked != binding.owner_user_id and "12345678" not in view.owner_masked
    assert view.before == binding.before and view.after == binding.after
    assert view.unknown_reason == "first unknown reason"
    assert token not in repr(view) and not hasattr(view, "claim_token")
    assert _dump(path) == before
    with pytest.raises(ledger.MealMutationConflict):
        inspect_meal_mutation_for_manual_reconcile(
            path, operation_id=binding.operation_id, owner_user_id="wrong-owner"
        )


@pytest.mark.parametrize("initial", ["reserved", "outcome_unknown"])
def test_manual_applied_swap_builds_trusted_receipt_and_atomically_unlocks(tmp_path, initial):
    path = tmp_path / f"swap-{initial}.sqlite"
    _install(path)
    binding = _binding()
    _reserve(path, binding, unknown=initial == "outcome_unknown")

    result = _manual(path, binding, "applied")

    assert result.kind == "newly_completed"
    assert result.result["outcome"] == "completed"
    assert binding.operation_id in result.result["summary_line"]
    assert "Meal A" in result.result["summary_line"] and "Meal B" in result.result["summary_line"]
    with sqlite3.connect(path) as conn:
        op = conn.execute(
            "SELECT status,unknown_reason,resolution_note,resolved_by,completed_at FROM meal_mutation_operations"
        ).fetchone()
        summary = conn.execute("SELECT summary_text FROM health_profile").fetchone()[0]
        locks = conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0]
    assert op[0] == "completed" and op[2:4] == ("ticket-42: provider queue drained", "admin-7")
    assert op[4] and locks == 0 and result.result["summary_line"] in summary
    assert op[1] == ("first unknown reason" if initial == "outcome_unknown" else "")


@pytest.mark.parametrize("disposition", ["applied", "not_applied"])
def test_manual_defer_applied_completes_request_but_not_applied_does_not(tmp_path, disposition):
    path = tmp_path / f"defer-{disposition}.sqlite"
    _install(path)
    binding, request_id = _defer(path)
    _reserve(path, binding, unknown=True)
    original_summary = "existing"

    result = _manual(path, binding, disposition)

    with sqlite3.connect(path) as conn:
        request = conn.execute(
            "SELECT status,approved_by,approved_at,note FROM deferred_meals WHERE id=?", (request_id,)
        ).fetchone()
        summary = conn.execute("SELECT summary_text FROM health_profile").fetchone()[0]
        op = conn.execute(
            "SELECT status,unknown_reason,resolution_note,resolved_by,completed_at,result_json FROM meal_mutation_operations"
        ).fetchone()
        locks = conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0]
    assert locks == 0 and op[1] == "first unknown reason"
    assert op[2:4] == ("ticket-42: provider queue drained", "admin-7") and op[4]
    if disposition == "applied":
        assert result.kind == "newly_completed" and op[0] == "completed"
        assert request[:2] == ("completed", "admin-7") and request[2] == op[4]
        assert request[3] == "original note"
        assert result.result["approved_by"] == "admin-7"
        assert result.result["summary_line"] in summary
    else:
        assert result.kind == "newly_manual_not_applied" and op[0] == "manual_not_applied"
        assert request == ("pending", "", "", "original note")
        assert summary == original_summary


@pytest.mark.parametrize(
    "override,exc",
    [
        ({"evidence": ""}, ValueError), ({"evidence": "   "}, ValueError),
        ({"evidence": " evidence "}, ValueError),
        ({"admin": ""}, ValueError), ({"admin": " admin "}, ValueError), ({"confirmed": False}, ValueError),
        ({"disposition": "maybe"}, ValueError), ({"owner": "wrong-owner"}, ledger.MealMutationConflict),
    ],
)
def test_manual_inputs_fail_closed_without_mutation(tmp_path, override, exc):
    path = tmp_path / "invalid.sqlite"
    _install(path)
    binding = _binding()
    _reserve(path, binding, unknown=True)
    before = _dump(path)
    kwargs = dict(
        operation_id=binding.operation_id, owner_user_id=override.get("owner", binding.owner_user_id),
        disposition=override.get("disposition", "applied"),
        evidence_note=override.get("evidence", "evidence"), admin_uid=override.get("admin", "admin"),
        no_outstanding_late_request_confirmed=override.get("confirmed", True),
    )
    with pytest.raises(exc):
        manually_reconcile_meal_mutation(path, **kwargs)
    assert _dump(path) == before


def test_exact_manual_replay_is_noop_but_changed_resolution_fails_closed(tmp_path):
    path = tmp_path / "replay.sqlite"
    _install(path)
    binding = _binding()
    _reserve(path, binding, unknown=True)
    first = _manual(path, binding, "applied")
    before = _dump(path)

    replay = _manual(path, binding, "applied")
    assert replay.kind == "replay_completed" and replay.result == first.result
    assert _dump(path) == before
    for changed in (
        {"evidence": "changed"}, {"admin": "admin-8"}, {"disposition": "not_applied"}
    ):
        with pytest.raises(ledger.MealMutationConflict, match="replay|terminal"):
            _manual(path, binding, changed.get("disposition", "applied"),
                    evidence=changed.get("evidence", "ticket-42: provider queue drained"),
                    admin=changed.get("admin", "admin-7"))
        assert _dump(path) == before


def test_nonmanual_terminal_cannot_be_overridden(tmp_path):
    path = tmp_path / "terminal.sqlite"
    _install(path)
    binding = _binding()
    token = _reserve(path, binding)
    ledger.mark_meal_mutation_rejected(
        path, binding.operation_id, token, binding.owner_user_id,
        {"message": "provider rejected", "outcome": "rejected"},
    )
    before = _dump(path)
    with pytest.raises(ledger.MealMutationConflict, match="terminal"):
        _manual(path, binding, "not_applied")
    assert _dump(path) == before


def test_manual_defer_exact_replay_rejects_changed_linked_request(tmp_path):
    path = tmp_path / "defer-changed-replay.sqlite"
    _install(path)
    binding, request_id = _defer(path)
    _reserve(path, binding, unknown=True)
    _manual(path, binding, "applied")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE deferred_meals SET target_meal_type='早餐' WHERE id=?", (request_id,)
        )
    before = _dump(path)

    with pytest.raises(ledger.MealMutationConflict, match="deferred.*replay"):
        _manual(path, binding, "applied")

    assert _dump(path) == before


def test_manual_terminal_metadata_corruption_fails_closed_without_disclosure(tmp_path):
    path = tmp_path / "manual-metadata.sqlite"
    _install(path)
    binding = _binding()
    _reserve(path, binding, unknown=True)
    _manual(path, binding, "not_applied")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET resolution_note='' WHERE operation_id=?",
            (binding.operation_id,),
        )
    before = _dump(path)

    with pytest.raises(RuntimeError, match="manual resolution metadata"):
        inspect_meal_mutation_for_manual_reconcile(
            path, operation_id=binding.operation_id, owner_user_id=binding.owner_user_id
        )

    assert _dump(path) == before


class _FailCommit(sqlite3.Connection):
    fail = False
    def commit(self):
        if self.fail:
            raise sqlite3.OperationalError("manual commit failed")
        return super().commit()


def test_commit_failure_rolls_back_operation_profile_defer_and_locks(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite"
    _install(path)
    binding, _request_id = _defer(path)
    _reserve(path, binding, unknown=True)
    before = _dump(path)
    real_connect = sqlite3.connect
    def connect(*args, **kwargs):
        kwargs["factory"] = _FailCommit
        conn = real_connect(*args, **kwargs)
        conn.fail = True
        return conn
    monkeypatch.setattr(ledger.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="manual commit"):
        _manual(path, binding, "applied")
    monkeypatch.undo()
    assert _dump(path) == before
    with sqlite3.connect(path, timeout=0.1) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


def test_old_manual_replay_does_not_touch_lock_owned_by_new_event(tmp_path):
    path = tmp_path / "new-lock.sqlite"
    _install(path)
    old = _binding(suffix="old")
    _reserve(path, old, unknown=True)
    first = _manual(path, old, "not_applied")
    new = _binding(suffix="new")
    new_token = _reserve(path, new)
    before = _dump(path)

    replay = _manual(path, old, "not_applied")

    assert replay.kind == "replay_manual_not_applied" and replay.result == first.result
    assert _dump(path) == before
    with sqlite3.connect(path) as conn:
        locks = conn.execute(
            "SELECT operation_id,COUNT(*) FROM meal_mutation_resource_locks GROUP BY operation_id"
        ).fetchall()
    assert locks == [(new.operation_id, 2)] and new_token


def _lookup(path, binding):
    return ledger.lookup_meal_mutation_for_request(
        path,
        event_id=binding.event_id,
        owner_user_id=binding.owner_user_id,
        purpose=binding.purpose,
        request_id=binding.request_id,
        payload=binding.payload,
    )


def _terminal_entry(path, binding, token, result, entry, disposition):
    if entry == "ensure":
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            return ensure_meal_mutation_schema(conn)
    if entry == "reserve":
        return reserve_meal_mutation(path, binding)
    if entry == "lookup":
        return _lookup(path, binding)
    if entry == "inspect":
        return inspect_meal_mutation_for_manual_reconcile(
            path, operation_id=binding.operation_id,
            owner_user_id=binding.owner_user_id,
        )
    if entry == "complete":
        return ledger.complete_meal_mutation(
            path, binding.operation_id, token, binding.owner_user_id,
            summary_line=result.get("summary_line", "\nunused terminal replay"),
            result=result,
            approved_by=result.get("approved_by", ""),
        )
    assert entry == "manual"
    return _manual(path, binding, disposition)


@pytest.mark.parametrize(
    "entry", ["ensure", "reserve", "lookup", "inspect", "complete", "manual"]
)
@pytest.mark.parametrize(
    "attack",
    [
        "summary_missing",
        "summary_substring",
        "canonical_wrong_receipt",
        "metadata_mismatch",
        "metadata_missing",
    ],
)
def test_every_public_terminal_entry_rejects_drifted_manual_applied_receipt_without_writes(
    tmp_path, entry, attack
):
    path = tmp_path / f"manual-applied-{attack}-{entry}.sqlite"
    _install(path)
    if attack == "metadata_mismatch":
        binding, _request_id = _defer(path)
    else:
        binding = _binding()
    token = _reserve(path, binding, unknown=True)
    fresh = _manual(path, binding, "applied")
    with sqlite3.connect(path) as conn:
        if attack == "summary_missing":
            conn.execute(
                "UPDATE health_profile SET summary_text='existing' WHERE user_id=?",
                (binding.owner_user_id,),
            )
        elif attack == "summary_substring":
            conn.execute(
                "UPDATE health_profile SET summary_text=? WHERE user_id=?",
                ("existing" + fresh.result["summary_line"] + "-suffix", binding.owner_user_id),
            )
        elif attack == "canonical_wrong_receipt":
            forged = dict(fresh.result)
            forged["message"] = "FORGED"
            conn.execute(
                "UPDATE meal_mutation_operations SET result_json=? WHERE operation_id=?",
                (json.dumps(forged, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 binding.operation_id),
            )
        elif attack == "metadata_mismatch":
            conn.execute(
                "UPDATE meal_mutation_operations SET resolved_by='admin-8' WHERE operation_id=?",
                (binding.operation_id,),
            )
        else:
            conn.execute(
                "UPDATE meal_mutation_operations SET resolution_note='',resolved_by='' "
                "WHERE operation_id=?",
                (binding.operation_id,),
            )
    before = _dump(path)

    with pytest.raises((RuntimeError, ledger.MealMutationConflict)):
        _terminal_entry(path, binding, token, fresh.result, entry, "applied")

    assert _dump(path) == before


@pytest.mark.parametrize(
    "entry", ["ensure", "reserve", "lookup", "inspect", "complete", "manual"]
)
def test_every_public_terminal_entry_rejects_forged_manual_not_applied_receipt_without_writes(
    tmp_path, entry
):
    path = tmp_path / f"manual-not-applied-forged-{entry}.sqlite"
    _install(path)
    binding = _binding()
    token = _reserve(path, binding, unknown=True)
    fresh = _manual(path, binding, "not_applied")
    forged = {"message": "FORGED", "outcome": "manual_not_applied"}
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET result_json=? WHERE operation_id=?",
            (json.dumps(forged, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             binding.operation_id),
        )
    before = _dump(path)

    with pytest.raises((RuntimeError, ledger.MealMutationConflict)):
        _terminal_entry(path, binding, token, fresh.result, entry, "not_applied")

    assert _dump(path) == before


@pytest.mark.parametrize(
    "entry", ["ensure", "reserve", "lookup", "inspect", "complete", "manual"]
)
def test_valid_manual_applied_receipt_remains_accepted_by_every_public_terminal_entry(
    tmp_path, entry
):
    path = tmp_path / f"manual-applied-control-{entry}.sqlite"
    _install(path)
    binding = _binding()
    token = _reserve(path, binding, unknown=True)
    fresh = _manual(path, binding, "applied")
    before = _dump(path)

    outcome = _terminal_entry(path, binding, token, fresh.result, entry, "applied")

    assert entry == "ensure" or outcome is not None
    assert _dump(path) == before


@pytest.mark.parametrize("entry", ["ensure", "reserve", "lookup", "inspect", "manual"])
def test_valid_manual_not_applied_receipt_remains_accepted_by_its_public_entries(
    tmp_path, entry
):
    path = tmp_path / f"manual-not-applied-control-{entry}.sqlite"
    _install(path)
    binding = _binding()
    token = _reserve(path, binding, unknown=True)
    fresh = _manual(path, binding, "not_applied")
    before = _dump(path)

    outcome = _terminal_entry(path, binding, token, fresh.result, entry, "not_applied")

    assert entry == "ensure" or outcome is not None
    assert _dump(path) == before
