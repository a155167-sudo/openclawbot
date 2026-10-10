from __future__ import annotations

import sqlite3
import threading

import pytest

import meal_mutation_ledger as ledger
from meal_mutation_ledger import (
    MealMutationBinding,
    complete_meal_mutation,
    ensure_meal_mutation_schema,
    get_claimed_meal_mutation_snapshot,
    mark_meal_mutation_rejected,
    mark_meal_mutation_unknown,
    reserve_meal_mutation,
)


HEALTH_PROFILE_DDL = """CREATE TABLE health_profile (
    user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL, goal TEXT,
    restrictions TEXT, summary_text TEXT, active_days TEXT, sheet_name TEXT,
    today_extra_pro INTEGER DEFAULT 0
)"""

DEFERRED_MEALS_DDL = """CREATE TABLE deferred_meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    customer_name TEXT DEFAULT '',
    original_date TEXT NOT NULL,
    original_meal_type TEXT NOT NULL,
    target_date TEXT NOT NULL,
    target_meal_type TEXT NOT NULL,
    is_cross_period INTEGER DEFAULT 0,
    has_conflict INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    approved_at TEXT DEFAULT '',
    approved_by TEXT DEFAULT ''
)"""


def _install(path, *, profile=True):
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_meal_mutation_schema(conn)
        conn.execute(HEALTH_PROFILE_DDL)
        conn.execute(DEFERRED_MEALS_DDL)
        if profile:
            conn.execute(
                "INSERT INTO health_profile(user_id,name,summary_text,sheet_name) VALUES (?,?,?,?)",
                ("owner-1", "Owner", "existing", "owner-sheet"),
            )


def _binding(*, suffix="1", purpose="swap", request_id="", owner="owner-1", cells=None, payload=None):
    if payload is None:
        payload = {"d1": "2026-09-25", "m1": "午餐", "d2": "2026-09-26", "m2": "晚餐"}
    if cells is None:
        base = int(suffix) * 10
        cells = ({"row_idx": base + 1, "col_idx": 3}, {"row_idx": base + 2, "col_idx": 5})
    return MealMutationBinding(
        operation_id=f"line-ai:{owner}:event-{suffix}",
        event_id=f"event-{suffix}",
        owner_user_id=owner,
        purpose=purpose,
        request_id=request_id,
        payload=payload,
        spreadsheet_id="sheet-1",
        worksheet_id=42,
        worksheet_name="owner-sheet",
        cells=cells,
        before=("Meal A", "Meal B" if purpose == "swap" else ""),
        after=("Meal B" if purpose == "swap" else "無", "Meal A"),
    )


def _claim(path, binding):
    result = reserve_meal_mutation(path, binding)
    assert result.kind == "claimed"
    return result.claim_token


@pytest.mark.parametrize("reason", ["", "   ", "\t\n", b"invalid-text"])
@pytest.mark.parametrize("boundary", ["ensure", "reserve", "lookup", "unknown"])
def test_corrupt_unknown_reason_fails_closed(tmp_path, reason, boundary):
    path = tmp_path / "unknown-reason.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    mark_meal_mutation_unknown(path, binding.operation_id, token, binding.owner_user_id, "original reason")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meal_mutation_operations SET unknown_reason=?", (reason,))
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match="unknown_reason"):
        if boundary == "ensure":
            with sqlite3.connect(path) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                ensure_meal_mutation_schema(conn)
        elif boundary == "reserve":
            reserve_meal_mutation(path, binding)
        elif boundary == "lookup":
            ledger.lookup_meal_mutation_for_request(
                path, event_id=binding.event_id, owner_user_id=binding.owner_user_id,
                purpose=binding.purpose, request_id=binding.request_id, payload=binding.payload,
            )
        else:
            mark_meal_mutation_unknown(path, binding.operation_id, token, binding.owner_user_id, "later reason")
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 2


def _state(path, operation_id):
    with sqlite3.connect(path) as conn:
        operation = conn.execute(
            "SELECT status,result_json,unknown_reason,completed_at FROM meal_mutation_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        locks = conn.execute(
            "SELECT resource_key FROM meal_mutation_resource_locks WHERE operation_id=? ORDER BY resource_key",
            (operation_id,),
        ).fetchall()
        summary = conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id='owner-1'"
        ).fetchone()
        return operation, locks, summary


def _dump(path):
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump())


def _make_terminal_fixture(path, binding, status, completed_at):
    token = _claim(path, binding)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET status=?,result_json=?,completed_at=? "
            "WHERE operation_id=?",
            (status, '{"ok":true}', completed_at, binding.operation_id),
        )
        conn.execute(
            "DELETE FROM meal_mutation_resource_locks WHERE operation_id=?",
            (binding.operation_id,),
        )
    return token


def test_rejected_finalization_is_atomic_and_exact_replay_does_not_rewrite(tmp_path):
    path = tmp_path / "reject.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    result = {"message": "provider rejected", "ok": False}

    first = mark_meal_mutation_rejected(path, binding.operation_id, token, binding.owner_user_id, result)
    before = _state(path, binding.operation_id)
    replay = mark_meal_mutation_rejected(path, binding.operation_id, token, binding.owner_user_id, result)

    assert (first.kind, replay.kind) == ("newly_rejected", "replay_rejected")
    assert first.result == replay.result == result
    assert before == _state(path, binding.operation_id)
    assert before[0][0] == "rejected"
    assert before[1] == []
    assert before[2] == ("existing",)


def test_unknown_keeps_locks_and_first_reason_and_never_grants_resend(tmp_path):
    path = tmp_path / "unknown.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)

    first = mark_meal_mutation_unknown(
        path, binding.operation_id, token, binding.owner_user_id, "response lost"
    )
    replay = mark_meal_mutation_unknown(
        path, binding.operation_id, token, binding.owner_user_id, "later worker guess"
    )

    operation, locks, _summary = _state(path, binding.operation_id)
    assert (first.kind, replay.kind) == ("newly_unknown", "replay_unknown")
    assert operation[0:3] == ("outcome_unknown", "{}", "response lost")
    assert len(locks) == 2
    assert reserve_meal_mutation(path, binding).kind == "blocked_unresolved"


def test_complete_swap_appends_current_summary_and_cleans_locks_once(tmp_path):
    path = tmp_path / "complete.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    result = {"message": "done", "ok": True}

    first = complete_meal_mutation(
        path,
        binding.operation_id,
        token,
        binding.owner_user_id,
        summary_line="\n🔄 mutation one",
        result=result,
    )
    before = _state(path, binding.operation_id)
    replay = complete_meal_mutation(
        path,
        binding.operation_id,
        token,
        binding.owner_user_id,
        summary_line="\n🔄 mutation one",
        result=result,
    )

    assert (first.kind, replay.kind) == ("newly_completed", "replay_completed")
    assert first.result == replay.result == result
    assert before == _state(path, binding.operation_id)
    assert before[0][0] == "completed"
    assert before[1] == []
    assert before[2] == ("existing\n🔄 mutation one",)


def test_complete_defer_revalidates_request_and_completes_it_in_same_transaction(tmp_path):
    path = tmp_path / "defer.sqlite"
    _install(path)
    with sqlite3.connect(path) as conn:
        request_id = conn.execute(
            "INSERT INTO deferred_meals(user_id,original_date,original_meal_type,target_date,target_meal_type,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("owner-1", "2026-09-25", "午餐", "2026-09-26", "晚餐"),
        ).lastrowid
    binding = _binding(suffix="2", purpose="defer", request_id=str(request_id))
    token = _claim(path, binding)

    outcome = complete_meal_mutation(
        path,
        binding.operation_id,
        token,
        binding.owner_user_id,
        summary_line="\n⏸ deferred",
        result={"ok": True, "request_id": request_id},
        approved_by="admin-7",
    )

    assert outcome.kind == "newly_completed"
    with sqlite3.connect(path) as conn:
        deferred = conn.execute(
            "SELECT status,approved_by,length(approved_at) FROM deferred_meals WHERE id=?",
            (request_id,),
        ).fetchone()
    assert deferred[0:2] == ("completed", "admin-7")
    assert deferred[2] > 0
    assert _state(path, binding.operation_id)[1] == []


def test_completed_defer_replay_revalidates_full_request_contract(tmp_path):
    path = tmp_path / "defer-replay-drift.sqlite"
    _install(path)
    with sqlite3.connect(path) as conn:
        request_id = conn.execute(
            "INSERT INTO deferred_meals(user_id,original_date,original_meal_type,target_date,target_meal_type,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("owner-1", "2026-09-25", "午餐", "2026-09-26", "晚餐"),
        ).lastrowid
    binding = _binding(suffix="2", purpose="defer", request_id=str(request_id))
    token = _claim(path, binding)
    kwargs = dict(
        summary_line="\n⏸ deferred",
        result={"ok": True, "request_id": request_id},
        approved_by="admin-7",
    )
    complete_meal_mutation(
        path, binding.operation_id, token, binding.owner_user_id, **kwargs
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE deferred_meals SET target_meal_type='早餐' WHERE id=?",
            (request_id,),
        )
    before = _state(path, binding.operation_id)

    with pytest.raises(ledger.MealMutationConflict, match="deferred.*replay"):
        complete_meal_mutation(
            path, binding.operation_id, token, binding.owner_user_id, **kwargs
        )

    assert _state(path, binding.operation_id) == before


@pytest.mark.parametrize(
    "wrong",
    [
        {"owner_user_id": "attacker"},
        {"claim_token": "f" * 64},
    ],
)
def test_finalizers_reject_wrong_owner_or_token_without_disclosure_or_mutation(tmp_path, wrong):
    path = tmp_path / "wrong-identity.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    before = _state(path, binding.operation_id)
    args = dict(
        db_path=path,
        operation_id=binding.operation_id,
        claim_token=token,
        owner_user_id=binding.owner_user_id,
        result={"ok": False},
    )
    args.update(wrong)

    with pytest.raises(ledger.MealMutationConflict, match="claim"):
        mark_meal_mutation_rejected(**args)

    assert _state(path, binding.operation_id) == before


@pytest.mark.parametrize("bad_result", [None, [], True, {"x": float("nan")}, {"x": float("inf")}])
def test_terminal_result_must_be_canonical_dict_data_before_write(tmp_path, bad_result):
    path = tmp_path / "bad-result.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    before = _state(path, binding.operation_id)

    with pytest.raises((TypeError, ValueError)):
        mark_meal_mutation_rejected(path, binding.operation_id, token, binding.owner_user_id, bad_result)

    assert _state(path, binding.operation_id) == before


@pytest.mark.parametrize(
    "mutate",
    ["owner", "payload", "original", "target", "status"],
)
def test_defer_completion_rejects_owner_payload_or_request_drift(tmp_path, mutate):
    path = tmp_path / f"defer-{mutate}.sqlite"
    _install(path)
    with sqlite3.connect(path) as conn:
        request_id = conn.execute(
            "INSERT INTO deferred_meals(user_id,original_date,original_meal_type,target_date,target_meal_type,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("owner-1", "2026-09-25", "午餐", "2026-09-26", "晚餐"),
        ).lastrowid
    payload = {"d1": "2026-09-25", "m1": "午餐", "d2": "2026-09-26", "m2": "晚餐"}
    binding = _binding(suffix="2", purpose="defer", request_id=str(request_id), payload=payload)
    token = _claim(path, binding)
    with sqlite3.connect(path) as conn:
        if mutate == "owner":
            conn.execute("UPDATE deferred_meals SET user_id='other' WHERE id=?", (request_id,))
        elif mutate == "original":
            conn.execute("UPDATE deferred_meals SET original_date='2099-01-01' WHERE id=?", (request_id,))
        elif mutate == "target":
            conn.execute("UPDATE deferred_meals SET target_meal_type='早餐' WHERE id=?", (request_id,))
        elif mutate == "status":
            conn.execute("UPDATE deferred_meals SET status='rejected' WHERE id=?", (request_id,))
        else:
            conn.execute("UPDATE deferred_meals SET original_meal_type='早餐' WHERE id=?", (request_id,))
    before = _state(path, binding.operation_id)

    with pytest.raises(ledger.MealMutationConflict, match="deferred"):
        complete_meal_mutation(
            path,
            binding.operation_id,
            token,
            binding.owner_user_id,
            summary_line="\nshould not append",
            result={"ok": True},
            approved_by="admin",
        )

    assert _state(path, binding.operation_id) == before


def test_missing_health_profile_fails_instead_of_false_success(tmp_path):
    path = tmp_path / "missing-profile.sqlite"
    _install(path, profile=False)
    binding = _binding()
    token = _claim(path, binding)

    with pytest.raises(ledger.MealMutationConflict, match="health profile"):
        complete_meal_mutation(
            path,
            binding.operation_id,
            token,
            binding.owner_user_id,
            summary_line="\nline",
            result={"ok": True},
        )

    operation, locks, summary = _state(path, binding.operation_id)
    assert operation[0] == "reserved"
    assert len(locks) == 2
    assert summary is None


def test_late_worker_cannot_overwrite_manual_terminal_fixture(tmp_path):
    path = tmp_path / "manual-terminal.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET status='manual_not_applied',result_json=?,"
            "resolution_note=?,resolved_by=?,completed_at=?,updated_at=? WHERE operation_id=?",
            (
                '{"message":"人工查核確認遠端變更未套用且不會晚到。","outcome":"manual_not_applied"}',
                "legacy fixture evidence",
                "admin-fixture",
                "2026-09-25T12:34:56Z",
                "2026-09-25T12:34:56Z",
                binding.operation_id,
            ),
        )
        conn.execute("DELETE FROM meal_mutation_resource_locks WHERE operation_id=?", (binding.operation_id,))
    before = _state(path, binding.operation_id)

    with pytest.raises(ledger.MealMutationConflict, match="terminal"):
        complete_meal_mutation(
            path,
            binding.operation_id,
            token,
            binding.owner_user_id,
            summary_line="\nlate",
            result={"ok": True},
        )

    assert _state(path, binding.operation_id) == before


def test_completed_replay_requires_exact_result_and_summary_contract(tmp_path):
    path = tmp_path / "replay-contract.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    complete_meal_mutation(
        path, binding.operation_id, token, binding.owner_user_id,
        summary_line="\nexact", result={"ok": True}
    )
    before = _state(path, binding.operation_id)

    for summary_line, result in (("\nchanged", {"ok": True}), ("\nexact", {"ok": False})):
        with pytest.raises(ledger.MealMutationConflict, match="replay"):
            complete_meal_mutation(
                path, binding.operation_id, token, binding.owner_user_id,
                summary_line=summary_line, result=result
            )

    assert _state(path, binding.operation_id) == before


@pytest.mark.parametrize("failure_point", ["summary", "status", "delete"])
def test_statement_failure_rolls_back_summary_status_result_and_locks(tmp_path, failure_point):
    path = tmp_path / f"failure-{failure_point}.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    trigger_sql = {
        "summary": """CREATE TRIGGER injected_failure BEFORE UPDATE ON health_profile
            BEGIN SELECT RAISE(ABORT, 'injected summary failure'); END""",
        "status": """CREATE TRIGGER injected_failure BEFORE UPDATE ON meal_mutation_operations
            WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'injected status failure'); END""",
        "delete": """CREATE TRIGGER injected_failure BEFORE DELETE ON meal_mutation_resource_locks
            BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END""",
    }[failure_point]
    with sqlite3.connect(path) as conn:
        conn.execute(trigger_sql)
    before = _state(path, binding.operation_id)

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        complete_meal_mutation(
            path, binding.operation_id, token, binding.owner_user_id,
            summary_line="\nline", result={"ok": True}
        )

    assert _state(path, binding.operation_id) == before
    with sqlite3.connect(path, timeout=0.1) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


class _InjectedConnection(sqlite3.Connection):
    fail_commit_with = None
    fail_execute_fragment = ""

    def execute(self, sql, parameters=()):
        if self.fail_execute_fragment and self.fail_execute_fragment in sql:
            raise SystemExit("injected SystemExit")
        return super().execute(sql, parameters)

    def commit(self):
        if self.fail_commit_with is not None:
            raise self.fail_commit_with
        return super().commit()


def _patch_connect(monkeypatch, *, fail_commit_with=None, fail_execute_fragment=""):
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs["factory"] = _InjectedConnection
        conn = real_connect(*args, **kwargs)
        conn.fail_commit_with = fail_commit_with
        conn.fail_execute_fragment = fail_execute_fragment
        return conn

    monkeypatch.setattr(ledger.sqlite3, "connect", connect)


def test_systemexit_rolls_back_and_releases_writer_lock(tmp_path, monkeypatch):
    path = tmp_path / "systemexit.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    before = _state(path, binding.operation_id)
    _patch_connect(monkeypatch, fail_execute_fragment="DELETE FROM meal_mutation_resource_locks")

    with pytest.raises(SystemExit, match="injected"):
        complete_meal_mutation(
            path, binding.operation_id, token, binding.owner_user_id,
            summary_line="\nline", result={"ok": True}
        )

    monkeypatch.undo()
    assert _state(path, binding.operation_id) == before
    with sqlite3.connect(path, timeout=0.1) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


@pytest.mark.parametrize("exc", [sqlite3.OperationalError("commit failed"), SystemExit("commit exit")])
def test_final_commit_failure_rolls_back_all_local_effects(tmp_path, monkeypatch, exc):
    path = tmp_path / "commit-failure.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    before = _state(path, binding.operation_id)
    _patch_connect(monkeypatch, fail_commit_with=exc)

    with pytest.raises(type(exc), match="commit"):
        complete_meal_mutation(
            path, binding.operation_id, token, binding.owner_user_id,
            summary_line="\nline", result={"ok": True}
        )

    monkeypatch.undo()
    assert _state(path, binding.operation_id) == before


def test_unknown_commit_failure_restores_reserved_state_and_both_locks(tmp_path, monkeypatch):
    path = tmp_path / "unknown-commit-failure.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    before = _state(path, binding.operation_id)
    _patch_connect(monkeypatch, fail_commit_with=sqlite3.OperationalError("commit failed"))

    with pytest.raises(sqlite3.OperationalError, match="commit failed"):
        mark_meal_mutation_unknown(
            path, binding.operation_id, token, binding.owner_user_id, "response lost"
        )

    monkeypatch.undo()
    after = _state(path, binding.operation_id)
    assert after == before
    assert after[0][0] == "reserved"
    assert len(after[1]) == 2
    with sqlite3.connect(path, timeout=0.1) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


def test_two_connections_append_summary_without_lost_update(tmp_path):
    path = tmp_path / "parallel.sqlite"
    _install(path)
    bindings = [_binding(suffix="1"), _binding(suffix="2")]
    tokens = [_claim(path, binding) for binding in bindings]
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []

    def worker(index):
        try:
            barrier.wait(timeout=5)
            outcomes.append(
                complete_meal_mutation(
                    path,
                    bindings[index].operation_id,
                    tokens[index],
                    bindings[index].owner_user_id,
                    summary_line=f"\nline-{index}",
                    result={"index": index, "ok": True},
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert [outcome.kind for outcome in outcomes] == ["newly_completed", "newly_completed"]
    with sqlite3.connect(path) as conn:
        summary = conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id='owner-1'"
        ).fetchone()[0]
        results = conn.execute(
            "SELECT result_json FROM meal_mutation_operations ORDER BY operation_id"
        ).fetchall()
    assert summary.startswith("existing")
    assert summary.count("line-0") == summary.count("line-1") == 1
    assert results == [('{"index":0,"ok":true}',), ('{"index":1,"ok":true}',)]


def test_claimed_snapshot_requires_exact_claim_and_never_returns_token(tmp_path):
    path = tmp_path / "snapshot.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)

    snapshot = get_claimed_meal_mutation_snapshot(
        path, binding.operation_id, token, binding.owner_user_id
    )

    assert snapshot == binding
    assert not hasattr(snapshot, "claim_token")
    with pytest.raises(ledger.MealMutationConflict, match="claim"):
        get_claimed_meal_mutation_snapshot(path, binding.operation_id, token, "attacker")


def test_completed_defer_replay_requires_approved_at_to_equal_operation_completion(tmp_path):
    path = tmp_path / "defer-approved-at-replay.sqlite"
    _install(path)
    with sqlite3.connect(path) as conn:
        request_id = conn.execute(
            "INSERT INTO deferred_meals(user_id,original_date,original_meal_type,target_date,target_meal_type,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("owner-1", "2026-09-25", "午餐", "2026-09-26", "晚餐"),
        ).lastrowid
    binding = _binding(suffix="2", purpose="defer", request_id=str(request_id))
    token = _claim(path, binding)
    kwargs = {
        "summary_line": "\n⏸ 系統紀錄：延後午餐",
        "result": {"ok": True, "request_id": request_id},
        "approved_by": "admin-7",
    }
    complete_meal_mutation(path, binding.operation_id, token, binding.owner_user_id, **kwargs)
    with sqlite3.connect(path) as conn:
        completed_at = conn.execute(
            "SELECT completed_at FROM meal_mutation_operations WHERE operation_id=?",
            (binding.operation_id,),
        ).fetchone()[0]
        approved_at = conn.execute(
            "SELECT approved_at FROM deferred_meals WHERE id=?", (request_id,)
        ).fetchone()[0]
        assert approved_at == completed_at
        conn.execute(
            "UPDATE deferred_meals SET approved_at='2099-01-01T00:00:00Z' WHERE id=?",
            (request_id,),
        )
    before = _dump(path)

    with pytest.raises(ledger.MealMutationConflict, match="deferred.*replay"):
        complete_meal_mutation(path, binding.operation_id, token, binding.owner_user_id, **kwargs)

    assert _dump(path) == before


def test_completed_replay_rejects_summary_line_substring_without_full_line(tmp_path):
    path = tmp_path / "summary-substring.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    kwargs = {"summary_line": "\nEXACT", "result": {"ok": True}}
    complete_meal_mutation(path, binding.operation_id, token, binding.owner_user_id, **kwargs)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE health_profile SET summary_text='unrelated\nEXACTLY-other-record' "
            "WHERE user_id=?",
            (binding.owner_user_id,),
        )
    before = _dump(path)

    with pytest.raises(ledger.MealMutationConflict, match="summary"):
        complete_meal_mutation(path, binding.operation_id, token, binding.owner_user_id, **kwargs)

    assert _dump(path) == before


def test_completed_replay_accepts_bounded_chinese_multiline_before_later_append(tmp_path):
    path = tmp_path / "summary-multiline.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    kwargs = {
        "summary_line": "\n🔄 系統紀錄：午餐與晚餐互換。\n營養師備註：維持原份量。",
        "result": {"ok": True},
    }
    complete_meal_mutation(path, binding.operation_id, token, binding.owner_user_id, **kwargs)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE health_profile SET summary_text=summary_text || '\n合法後續紀錄' "
            "WHERE user_id=?",
            (binding.owner_user_id,),
        )
    before = _dump(path)

    replay = complete_meal_mutation(
        path, binding.operation_id, token, binding.owner_user_id, **kwargs
    )

    assert replay.kind == "replay_completed"
    assert _dump(path) == before


def test_distinct_events_may_append_the_same_complete_summary_line(tmp_path):
    path = tmp_path / "same-summary-new-event.sqlite"
    _install(path)
    line = "\n🔄 系統紀錄：同文但不同事件。"
    bindings = (_binding(suffix="1"), _binding(suffix="2"))
    for binding in bindings:
        token = _claim(path, binding)
        outcome = complete_meal_mutation(
            path,
            binding.operation_id,
            token,
            binding.owner_user_id,
            summary_line=line,
            result={"ok": True},
        )
        assert outcome.kind == "newly_completed"
    with sqlite3.connect(path) as conn:
        summary = conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id='owner-1'"
        ).fetchone()[0]
    assert summary.count(line) == 2


@pytest.mark.parametrize(
    "summary_line",
    ["EXACT", "\n", "\nEXACT\n", "\nEXACT\n\nOTHER", "\r\nEXACT"],
)
def test_summary_line_requires_explicit_nonempty_complete_line_boundaries(tmp_path, summary_line):
    path = tmp_path / "summary-boundary.sqlite"
    _install(path)
    binding = _binding()
    token = _claim(path, binding)
    before = _dump(path)

    with pytest.raises(ValueError, match="summary_line"):
        complete_meal_mutation(
            path,
            binding.operation_id,
            token,
            binding.owner_user_id,
            summary_line=summary_line,
            result={"ok": True},
        )

    assert _dump(path) == before


@pytest.mark.parametrize("status", ["completed", "rejected"])
@pytest.mark.parametrize(
    "completed_at",
    ["", "not-a-timestamp", "2026-09-25T12:34:56"],
    ids=["empty", "invalid", "naive"],
)
def test_terminal_completed_at_integrity_matrix_rejects_without_writes(
    tmp_path, status, completed_at
):
    path = tmp_path / f"{status}-{completed_at or 'empty'}.sqlite"
    _install(path)
    binding = _binding()
    _make_terminal_fixture(path, binding, status, completed_at)
    before = _dump(path)

    with sqlite3.connect(path) as conn, pytest.raises(
        RuntimeError, match="completed_at"
    ):
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_meal_mutation_schema(conn)

    assert _dump(path) == before


@pytest.mark.parametrize(
    "boundary,status,completed_at",
    [
        ("reserve", "completed", ""),
        ("lookup", "rejected", "not-a-timestamp"),
        ("reject-finalizer", "rejected", "2026-09-25T12:34:56"),
        ("complete-finalizer", "completed", "2026-02-30T12:34:56Z"),
    ],
)
def test_all_trusted_result_boundaries_reject_bad_terminal_completion_receipts(
    tmp_path, boundary, status, completed_at
):
    path = tmp_path / f"terminal-boundary-{boundary}.sqlite"
    _install(path)
    binding = _binding()
    token = _make_terminal_fixture(path, binding, status, completed_at)
    if status == "completed":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE health_profile SET summary_text=summary_text || '\nexact' "
                "WHERE user_id=?",
                (binding.owner_user_id,),
            )
    before = _dump(path)

    with pytest.raises(RuntimeError, match="completed_at"):
        if boundary == "reserve":
            reserve_meal_mutation(path, binding)
        elif boundary == "lookup":
            ledger.lookup_meal_mutation_for_request(
                path,
                event_id=binding.event_id,
                owner_user_id=binding.owner_user_id,
                purpose=binding.purpose,
                request_id=binding.request_id,
                payload=binding.payload,
            )
        elif boundary == "reject-finalizer":
            mark_meal_mutation_rejected(
                path, binding.operation_id, token, binding.owner_user_id, {"ok": True}
            )
        else:
            complete_meal_mutation(
                path,
                binding.operation_id,
                token,
                binding.owner_user_id,
                summary_line="\nexact",
                result={"ok": True},
            )

    assert _dump(path) == before


@pytest.mark.parametrize("status", ["completed", "rejected"])
def test_terminal_completed_at_valid_utc_producer_format_is_accepted(tmp_path, status):
    path = tmp_path / f"terminal-valid-{status}.sqlite"
    _install(path)
    binding = _binding()
    _make_terminal_fixture(path, binding, status, "2026-09-25T12:34:56Z")

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_meal_mutation_schema(conn)
