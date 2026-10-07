from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

import pytest

from meal_mutation_ledger import (
    MealMutationBinding,
    ensure_meal_mutation_schema,
    lookup_meal_mutation_for_request,
    reserve_meal_mutation,
)


def _dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def _objects(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name LIKE 'meal_mutation_%' "
        "OR name='idx_meal_mutation_locks_operation' ORDER BY type,name"
    ).fetchall()


def test_fresh_schema_is_exact_and_second_ensure_is_no_write(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.sqlite")
    conn.execute("PRAGMA foreign_keys=ON")

    ensure_meal_mutation_schema(conn)
    assert conn.execute(
        "SELECT component,version,typeof(version),length(applied_at) "
        "FROM meal_mutation_schema_versions"
    ).fetchone() == ("meal_mutation", 1, "integer", 20)
    assert [row[:3] for row in _objects(conn)] == [
        ("index", "idx_meal_mutation_locks_operation", "meal_mutation_resource_locks"),
        ("index", "meal_mutation_operations_event_id", "meal_mutation_operations"),
        ("table", "meal_mutation_operations", "meal_mutation_operations"),
        ("table", "meal_mutation_resource_locks", "meal_mutation_resource_locks"),
        ("table", "meal_mutation_schema_versions", "meal_mutation_schema_versions"),
        ("trigger", "meal_mutation_active_lock_no_delete", "meal_mutation_resource_locks"),
        ("trigger", "meal_mutation_binding_immutable", "meal_mutation_operations"),
        ("trigger", "meal_mutation_lock_immutable", "meal_mutation_resource_locks"),
        ("trigger", "meal_mutation_no_delete", "meal_mutation_operations"),
    ]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.commit()
    before = _dump(conn)
    changes = conn.total_changes

    ensure_meal_mutation_schema(conn)

    assert _dump(conn) == before
    assert conn.total_changes == changes
    assert not conn.in_transaction


def test_schema_helper_preserves_caller_transaction_and_rollback(tmp_path):
    conn = sqlite3.connect(tmp_path / "caller.sqlite")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO sentinel VALUES ('keep-pending')")

    ensure_meal_mutation_schema(conn)

    assert conn.in_transaction
    assert conn.execute("SELECT value FROM sentinel").fetchone() == ("keep-pending",)
    conn.rollback()
    assert conn.execute("SELECT * FROM sentinel").fetchall() == []
    assert _objects(conn) == []


def test_partial_same_name_schema_fails_closed_without_mutation(tmp_path):
    conn = sqlite3.connect(tmp_path / "malformed.sqlite")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE meal_mutation_operations(operation_id TEXT)")
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('original')")
    conn.commit()
    before = _dump(conn)

    with pytest.raises(RuntimeError, match="malformed|partial|contract"):
        ensure_meal_mutation_schema(conn)

    assert _dump(conn) == before
    assert not conn.in_transaction


class _FailingDDLConnection(sqlite3.Connection):
    fail_fragment = ""

    def execute(self, sql, parameters=()):
        if self.fail_fragment and self.fail_fragment in sql:
            raise sqlite3.OperationalError("injected DDL failure")
        return super().execute(sql, parameters)


def test_schema_ddl_failure_rolls_back_component_but_keeps_caller_sentinel(tmp_path):
    conn = sqlite3.connect(tmp_path / "ddl-fault.sqlite", factory=_FailingDDLConnection)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO sentinel VALUES ('caller-write')")
    conn.fail_fragment = "CREATE INDEX idx_meal_mutation_locks_operation"

    with pytest.raises(sqlite3.OperationalError, match="injected DDL failure"):
        ensure_meal_mutation_schema(conn)

    conn.fail_fragment = ""
    assert conn.in_transaction
    assert conn.execute("SELECT * FROM sentinel").fetchall() == [("caller-write",)]
    assert _objects(conn) == []
    conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()


def test_weakened_current_check_is_rejected_without_churn(tmp_path):
    path = tmp_path / "weakened.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_meal_mutation_schema(conn)
    conn.commit()
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql=replace(sql, "
        "\"CHECK(purpose IN ('swap','defer'))\", \"CHECK(purpose <> '')\") "
        "WHERE name='meal_mutation_operations'"
    )
    conn.execute("PRAGMA schema_version=9876")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    before = _dump(conn)

    with pytest.raises(RuntimeError, match="contract"):
        ensure_meal_mutation_schema(conn)

    assert _dump(conn) == before
    assert conn.total_changes == 0


def test_weakened_current_foreign_key_is_rejected_without_churn(tmp_path):
    path = tmp_path / "weakened-fk.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_meal_mutation_schema(conn)
    conn.commit()
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql=replace(sql, "
        "'ON UPDATE RESTRICT ON DELETE RESTRICT', 'ON UPDATE CASCADE ON DELETE CASCADE') "
        "WHERE name='meal_mutation_resource_locks'"
    )
    conn.execute("PRAGMA schema_version=9877")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    before = _dump(conn)

    with pytest.raises(RuntimeError, match="contract"):
        ensure_meal_mutation_schema(conn)

    assert _dump(conn) == before
    assert conn.total_changes == 0


def _install(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_meal_mutation_schema(conn)
    conn.commit()
    conn.close()


def _binding(**changes):
    values = dict(
        operation_id="line-ai:owner-1:event-1",
        event_id="event-1",
        owner_user_id="owner-1",
        purpose="swap",
        request_id="",
        payload={"d1": "2026-09-25", "m1": "breakfast", "d2": "2026-09-26", "m2": "dinner"},
        spreadsheet_id="sheet-1",
        worksheet_id=42,
        worksheet_name="owner-1",
        cells=({"row_idx": 2, "col_idx": 3}, {"row_idx": 8, "col_idx": 5}),
        before=("A", "B"),
        after=("B", "A"),
    )
    values.update(changes)
    return MealMutationBinding(**values)


def test_binding_and_history_triggers_enforce_immutability(tmp_path):
    path = tmp_path / "immutable.sqlite"
    _install(path)
    reserve_meal_mutation(path, _binding())
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="binding is immutable"):
            conn.execute(
                "UPDATE meal_mutation_operations SET before_json='[]' WHERE event_id='event-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM meal_mutation_operations WHERE event_id='event-1'")


def test_active_locks_cannot_be_deleted_or_transferred_but_terminal_cleanup_is_atomic(tmp_path):
    path = tmp_path / "lock-lifecycle.sqlite"
    _install(path)
    binding = _binding()
    reserve_meal_mutation(path, binding)
    with sqlite3.connect(path) as conn:
        original = conn.execute(
            "SELECT resource_key,operation_id FROM meal_mutation_resource_locks ORDER BY resource_key"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="active.*cannot be deleted"):
            conn.execute("DELETE FROM meal_mutation_resource_locks WHERE operation_id=?", (binding.operation_id,))
        with pytest.raises(sqlite3.IntegrityError, match="lock is immutable"):
            conn.execute(
                "UPDATE meal_mutation_resource_locks SET operation_id='other' WHERE operation_id=?",
                (binding.operation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="lock is immutable"):
            conn.execute(
                "UPDATE meal_mutation_resource_locks SET resource_key=resource_key || ':moved' "
                "WHERE operation_id=?",
                (binding.operation_id,),
            )
        assert conn.execute(
            "SELECT resource_key,operation_id FROM meal_mutation_resource_locks ORDER BY resource_key"
        ).fetchall() == original
        conn.rollback()

        conn.execute("BEGIN")
        conn.execute(
            "UPDATE meal_mutation_operations SET status='completed',result_json='{}',completed_at=? "
            "WHERE operation_id=?",
            ("2026-09-25T12:34:56Z", binding.operation_id),
        )
        conn.execute("DELETE FROM meal_mutation_resource_locks WHERE operation_id=?", (binding.operation_id,))
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 0
        conn.rollback()
        assert conn.execute(
            "SELECT status FROM meal_mutation_operations WHERE operation_id=?", (binding.operation_id,)
        ).fetchone() == ("reserved",)
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 2

        conn.execute("BEGIN")
        conn.execute(
            "UPDATE meal_mutation_operations SET status='completed',result_json=?,completed_at=? "
            "WHERE operation_id=?",
            ('{"ok":true}', "2026-09-25T12:34:56Z", binding.operation_id),
        )
        conn.execute("DELETE FROM meal_mutation_resource_locks WHERE operation_id=?", (binding.operation_id,))
        conn.commit()

    assert lookup_meal_mutation_for_request(
        path,
        event_id=binding.event_id,
        owner_user_id=binding.owner_user_id,
        purpose=binding.purpose,
        request_id=binding.request_id,
        payload=binding.payload,
    ).kind == "stored_result"


def test_reserve_exact_replay_lookup_and_changed_binding_conflict(tmp_path):
    path = tmp_path / "reserve.sqlite"
    _install(path)
    binding = _binding()

    first = reserve_meal_mutation(path, binding)
    replay = reserve_meal_mutation(path, binding)
    lookup = lookup_meal_mutation_for_request(
        path,
        event_id=binding.event_id,
        owner_user_id=binding.owner_user_id,
        purpose=binding.purpose,
        request_id=binding.request_id,
        payload=binding.payload,
    )

    assert first.kind == "claimed"
    assert len(first.claim_token) == 64
    assert replay.kind == "blocked_unresolved"
    assert replay.claim_token == ""
    assert lookup.kind == "blocked_unresolved"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 2
        original = conn.execute(
            "SELECT owner_user_id,payload_json,before_json,after_json FROM meal_mutation_operations"
        ).fetchone()

    for changed in (
        {"owner_user_id": "attacker"},
        {"payload": {**binding.payload, "m2": "lunch"}},
        {"before": ("changed", "B")},
        {"cells": ({"row_idx": 2, "col_idx": 3}, {"row_idx": 9, "col_idx": 5})},
    ):
        assert reserve_meal_mutation(path, _binding(**changed)).kind == "identity_conflict"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT owner_user_id,payload_json,before_json,after_json FROM meal_mutation_operations"
        ).fetchone() == original


def test_lookup_uses_only_stable_request_inputs_and_does_not_disclose_cross_owner(tmp_path):
    path = tmp_path / "lookup.sqlite"
    _install(path)
    binding = _binding()
    reserve_meal_mutation(path, binding)

    assert lookup_meal_mutation_for_request(
        path, event_id="missing", owner_user_id="owner-1", purpose="swap", request_id="", payload=binding.payload
    ).kind == "not_found"
    conflict = lookup_meal_mutation_for_request(
        path, event_id="event-1", owner_user_id="other", purpose="swap", request_id="", payload=binding.payload
    )
    assert conflict.kind == "identity_conflict"
    assert conflict.operation_id == ""
    assert conflict.result is None


def test_terminal_rows_return_stored_result_without_current_snapshot_inputs(tmp_path):
    path = tmp_path / "terminal.sqlite"
    _install(path)
    binding = _binding()
    reserve_meal_mutation(path, binding)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET status='completed',result_json=?,completed_at=? "
            "WHERE event_id=?",
            ('{"ok":true}', "2026-09-25T12:34:56Z", binding.event_id),
        )
        conn.execute("DELETE FROM meal_mutation_resource_locks")
        conn.commit()

    lookup = lookup_meal_mutation_for_request(
        path,
        event_id=binding.event_id,
        owner_user_id=binding.owner_user_id,
        purpose=binding.purpose,
        request_id=binding.request_id,
        payload=binding.payload,
    )
    replay = reserve_meal_mutation(path, binding)
    assert (lookup.kind, lookup.result) == ("stored_result", {"ok": True})
    assert (replay.kind, replay.result) == ("replay_completed", {"ok": True})


def test_outcome_unknown_keeps_exact_locks_and_blocks_replay(tmp_path):
    path = tmp_path / "outcome-unknown.sqlite"
    _install(path)
    binding = _binding()
    assert reserve_meal_mutation(path, binding).kind == "claimed"
    # This direct SQL is only a lifecycle fixture; finalization APIs are a later slice.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET status='outcome_unknown',unknown_reason='response lost' "
            "WHERE operation_id=?",
            (binding.operation_id,),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_meal_mutation_schema(conn)
        with pytest.raises(sqlite3.IntegrityError, match="active.*cannot be deleted"):
            conn.execute("DELETE FROM meal_mutation_resource_locks WHERE operation_id=?", (binding.operation_id,))
        conn.rollback()
    assert reserve_meal_mutation(path, binding).kind == "blocked_unresolved"
    assert lookup_meal_mutation_for_request(
        path,
        event_id=binding.event_id,
        owner_user_id=binding.owner_user_id,
        purpose=binding.purpose,
        request_id=binding.request_id,
        payload=binding.payload,
    ).kind == "blocked_unresolved"


def _offline_corrupt(path, corruption):
    """Bypass writers, then restore exact triggers so readers reach row validation."""
    with sqlite3.connect(path) as conn:
        triggers = conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name IN ('meal_mutation_operations','meal_mutation_resource_locks') "
            "ORDER BY name"
        ).fetchall()
        for name, _sql in triggers:
            conn.execute(f'DROP TRIGGER "{name}"')

        payload = conn.execute(
            "SELECT payload_json FROM meal_mutation_operations WHERE event_id='event-1'"
        ).fetchone()[0]
        mutations = {
            "missing_lock": (
                "DELETE FROM meal_mutation_resource_locks WHERE resource_key=("
                "SELECT MIN(resource_key) FROM meal_mutation_resource_locks WHERE operation_id=?)",
                ("line-ai:owner-1:event-1",),
            ),
            "terminal_locks": (
                "UPDATE meal_mutation_operations SET status='completed',result_json=? "
                "WHERE event_id='event-1'",
                ('{"ok":true}',),
            ),
            "rejected_with_locks": (
                "UPDATE meal_mutation_operations SET status='rejected',result_json='{}' "
                "WHERE event_id='event-1'",
                (),
            ),
            "manual_not_applied_with_locks": (
                "UPDATE meal_mutation_operations SET status='manual_not_applied',result_json='{}' "
                "WHERE event_id='event-1'",
                (),
            ),
            "extra_lock": (
                "INSERT INTO meal_mutation_resource_locks(resource_key,operation_id,created_at) "
                "VALUES ('sheet:sheet-1:worksheet:42:r99c99','line-ai:owner-1:event-1','offline')",
                (),
            ),
            "wrong_key": (
                "UPDATE meal_mutation_resource_locks SET resource_key=resource_key || ':wrong' "
                "WHERE resource_key=(SELECT MIN(resource_key) FROM meal_mutation_resource_locks)",
                (),
            ),
            "wrong_operation": (
                "UPDATE meal_mutation_resource_locks SET operation_id='line-ai:owner-1:event-2' "
                "WHERE operation_id='line-ai:owner-1:event-1' AND resource_key=("
                "SELECT MIN(resource_key) FROM meal_mutation_resource_locks "
                "WHERE operation_id='line-ai:owner-1:event-1')",
                (),
            ),
            "orphan_lock": (
                "INSERT INTO meal_mutation_resource_locks(resource_key,operation_id,created_at) "
                "VALUES ('orphan:key','absent-operation','offline')",
                (),
            ),
            "wrong_digest": (
                "UPDATE meal_mutation_operations SET payload_sha256=? WHERE event_id='event-1'",
                ("f" * 64,),
            ),
            "malformed_digest": (
                "UPDATE meal_mutation_operations SET payload_sha256=? WHERE event_id='event-1'",
                ("z" * 64,),
            ),
            "malformed_payload": (
                "UPDATE meal_mutation_operations SET payload_json=?,payload_sha256=? WHERE event_id='event-1'",
                ("{", hashlib.sha256(b"{").hexdigest()),
            ),
            "noncanonical_payload": (
                "UPDATE meal_mutation_operations SET payload_json=?,payload_sha256=? WHERE event_id='event-1'",
                (
                    json.dumps(json.loads(payload), ensure_ascii=False, sort_keys=False, separators=(", ", ": ")),
                    "",  # replaced below with the digest of these exact noncanonical bytes
                ),
            ),
            "cells_wrong_shape": (
                "UPDATE meal_mutation_operations SET cells_json='[{\"col_idx\":3,\"row_idx\":2}]' "
                "WHERE event_id='event-1'",
                (),
            ),
            "snapshot_wrong_type": (
                "UPDATE meal_mutation_operations SET before_json='[1,\"B\"]' WHERE event_id='event-1'",
                (),
            ),
            "worksheet_id_wrong_type": (
                "UPDATE meal_mutation_operations SET worksheet_id='not-an-integer' WHERE event_id='event-1'",
                (),
            ),
            "owner_wrong_type": (
                "UPDATE meal_mutation_operations SET owner_user_id=? WHERE event_id='event-1'",
                (sqlite3.Binary(b"owner-1"),),
            ),
            "unsorted_cell_mapping": (
                "UPDATE meal_mutation_operations SET "
                "cells_json='[{\"col_idx\":5,\"row_idx\":8},{\"col_idx\":3,\"row_idx\":2}]' "
                "WHERE event_id='event-1'",
                (),
            ),
        }
        sql, params = mutations[corruption]
        if corruption == "noncanonical_payload":
            noncanonical = params[0]
            params = (noncanonical, hashlib.sha256(noncanonical.encode("utf-8")).hexdigest())
        conn.execute(sql, params)
        for _name, sql in triggers:
            conn.execute(sql)
        conn.commit()


def _call_integrity_boundary(path, boundary, binding):
    if boundary == "ensure":
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_meal_mutation_schema(conn)
        return
    if boundary == "reserve":
        reserve_meal_mutation(path, binding)
        return
    assert boundary == "lookup"
    lookup_meal_mutation_for_request(
        path,
        event_id=binding.event_id,
        owner_user_id=binding.owner_user_id,
        purpose=binding.purpose,
        request_id=binding.request_id,
        payload=binding.payload,
    )


@pytest.mark.parametrize("boundary", ["ensure", "reserve", "lookup"])
@pytest.mark.parametrize(
    "corruption",
    [
        "missing_lock",
        "terminal_locks",
        "rejected_with_locks",
        "manual_not_applied_with_locks",
        "extra_lock",
        "wrong_key",
        "wrong_operation",
        "orphan_lock",
        "wrong_digest",
        "malformed_digest",
        "malformed_payload",
        "noncanonical_payload",
        "cells_wrong_shape",
        "snapshot_wrong_type",
        "worksheet_id_wrong_type",
        "owner_wrong_type",
        "unsorted_cell_mapping",
    ],
)
def test_all_public_boundaries_reject_exact_ddl_offline_row_corruption_without_mutation(
    tmp_path, boundary, corruption
):
    path = tmp_path / f"{boundary}-{corruption}.sqlite"
    _install(path)
    binding = _binding()
    assert reserve_meal_mutation(path, binding).kind == "claimed"
    if corruption == "wrong_operation":
        second = _binding(
            operation_id="line-ai:owner-1:event-2",
            event_id="event-2",
            cells=({"row_idx": 20, "col_idx": 30}, {"row_idx": 30, "col_idx": 40}),
        )
        assert reserve_meal_mutation(path, second).kind == "claimed"
    with sqlite3.connect(path) as conn:
        exact_objects = _objects(conn)
    _offline_corrupt(path, corruption)
    with sqlite3.connect(path) as conn:
        assert _objects(conn) == exact_objects
        if corruption != "orphan_lock":
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        before = _dump(conn)

    with pytest.raises(RuntimeError, match="meal mutation .*integrity|foreign key"):
        _call_integrity_boundary(path, boundary, binding)

    with sqlite3.connect(path) as conn:
        assert _dump(conn) == before


def test_new_event_cannot_claim_when_an_unresolved_operation_lost_a_lock(tmp_path):
    path = tmp_path / "new-event-missing-lock.sqlite"
    _install(path)
    assert reserve_meal_mutation(path, _binding()).kind == "claimed"
    _offline_corrupt(path, "missing_lock")
    second = _binding(operation_id="line-ai:owner-1:event-2", event_id="event-2")
    with sqlite3.connect(path) as conn:
        before = _dump(conn)

    with pytest.raises(RuntimeError, match="integrity"):
        reserve_meal_mutation(path, second)

    with sqlite3.connect(path) as conn:
        assert _dump(conn) == before
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_mutation_operations WHERE event_id='event-2'"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": ""},
        {"event_id": " "},
        {"owner_user_id": ""},
        {"purpose": "other"},
        {"request_id": "unexpected"},
        {"worksheet_id": True},
        {"worksheet_id": "42"},
        {"payload": {"d1": "x"}},
        {"payload": {"d1": "x", "m1": "a", "d2": "y", "m2": float("nan")}},
        {"cells": ({"row_idx": 2, "col_idx": 3}, {"row_idx": 2, "col_idx": 3})},
        {"cells": ({"row_idx": True, "col_idx": 3}, {"row_idx": 8, "col_idx": 5})},
        {"before": ("A",)},
    ],
)
def test_invalid_bindings_are_rejected_before_writes(tmp_path, changes):
    path = tmp_path / "invalid.sqlite"
    _install(path)
    with pytest.raises((TypeError, ValueError)):
        reserve_meal_mutation(path, _binding(**changes))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 0


def test_overlap_and_second_lock_failure_leave_no_partial_writes(tmp_path):
    path = tmp_path / "locks.sqlite"
    _install(path)
    assert reserve_meal_mutation(path, _binding()).kind == "claimed"
    overlap = _binding(
        operation_id="line-ai:owner-1:event-2",
        event_id="event-2",
        cells=({"row_idx": 8, "col_idx": 5}, {"row_idx": 12, "col_idx": 7}),
    )
    assert reserve_meal_mutation(path, overlap).kind == "resource_blocked"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TRIGGER fail_second_lock BEFORE INSERT ON meal_mutation_resource_locks
            WHEN NEW.resource_key LIKE '%:r30c40'
            BEGIN SELECT RAISE(ABORT, 'injected second lock failure'); END""")
        conn.commit()
    failing = _binding(
        operation_id="line-ai:owner-1:event-3",
        event_id="event-3",
        cells=({"row_idx": 20, "col_idx": 30}, {"row_idx": 30, "col_idx": 40}),
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected second lock failure"):
        reserve_meal_mutation(path, failing)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 2
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


def test_two_threads_same_event_or_overlapping_resource_produce_one_claim(tmp_path):
    path = tmp_path / "race.sqlite"
    _install(path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker(binding):
        try:
            barrier.wait(timeout=5)
            results.append(reserve_meal_mutation(path, binding))
        except BaseException as exc:  # surfaced below, not swallowed
            errors.append(exc)

    first = _binding()
    second = _binding(
        operation_id="line-ai:owner-1:event-2",
        event_id="event-2",
        cells=({"row_idx": 2, "col_idx": 3}, {"row_idx": 99, "col_idx": 9}),
    )
    threads = [threading.Thread(target=worker, args=(value,)) for value in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(result.kind for result in results) == ["claimed", "resource_blocked"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 2


@pytest.mark.parametrize("boundary", ["ensure", "reserve", "lookup"])
@pytest.mark.parametrize("raw", ['{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1,"x":2}', '{"x":{"a":1,"a":2}}', '[]', 'null', 'true', '"ok"', '{'])
def test_invalid_persisted_result_rejected_without_mutation(tmp_path, boundary, raw):
    path = tmp_path / "bad-result.sqlite"
    _install(path)
    binding = _binding()
    reserve_meal_mutation(path, binding)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET status='completed', result_json=?, completed_at=?",
            (raw, "2026-09-25T12:34:56Z"),
        )
        conn.execute("DELETE FROM meal_mutation_resource_locks")
    with sqlite3.connect(path) as conn:
        before = _dump(conn)
    with pytest.raises(RuntimeError, match="integrity"):
        _call_integrity_boundary(path, boundary, binding)
    with sqlite3.connect(path) as conn:
        assert _dump(conn) == before


def test_two_threads_same_event_produce_one_claim_and_one_block(tmp_path):
    path = tmp_path / "same-event-race.sqlite"
    _install(path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(reserve_meal_mutation(path, _binding()))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert sorted(result.kind for result in results) == ["blocked_unresolved", "claimed"]
