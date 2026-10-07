import hashlib
import sqlite3
import threading

import pytest

from printer_dispatch_claims import (
    PrinterClaimConflict,
    activate_v2_capability,
    claim_current_dispatch,
    ensure_printer_claim_schema,
    record_transport_result,
)
from reschedule_dispatch_versions import (
    RescheduleConflict,
    confirm_sheet_readback,
    create_initial_version,
    ensure_reschedule_dispatch_schema,
    reserve_reschedule,
)

OWNER = "U" + "1" * 32
NOW = "2026-09-26T10:00:00+08:00"
LATER = "2026-09-26T10:03:00+08:00"
STORE = "store-nanjing"
SCOPE = "printer:claim:store-nanjing"
TOKEN = "claim-secret-never-store"
PAYLOAD_OLD = {"store_id": STORE, "rows": [{"dispatch_row_id": "dispatch-a", "service_date": "2026-10-23", "lunch": "A", "dinner": "B"}]}
PAYLOAD_NEW = {"store_id": STORE, "rows": [{"dispatch_row_id": "dispatch-new", "service_date": "2026-09-26", "lunch": "A", "dinner": "B"}]}


def open_db(path):
    conn = sqlite3.connect(path, timeout=2.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def seed(path, *, payload=PAYLOAD_OLD):
    conn = open_db(path)
    ensure_reschedule_dispatch_schema(conn)
    ensure_printer_claim_schema(conn)
    create_initial_version(
        conn,
        version_id="version-old",
        order_id=4,
        owner_user_id=OWNER,
        payload=payload,
        confirmed_at=NOW,
    )
    conn.commit()
    return conn


def cutover(conn):
    return activate_v2_capability(
        conn,
        operation_id="cutover-1",
        store_id=STORE,
        admin_scope=SCOPE,
        generation=1,
        cache_cleared=True,
        all_consumers_confirmed=True,
        evidence_digest="a" * 64,
        now=NOW,
    )


def claim(conn, **overrides):
    values = dict(
        operation_id="print-1",
        version_id="version-old",
        order_id=4,
        owner_user_id=OWNER,
        store_id=STORE,
        admin_scope=SCOPE,
        capability_generation=1,
        claim_token=TOKEN,
        dispatch_row_id="dispatch-a",
        service_date="2026-10-23",
        lease_expires_at="2026-09-26T10:02:00+08:00",
        now=NOW,
    )
    values.update(overrides)
    return claim_current_dispatch(conn, **values)


def reserve(conn, **overrides):
    values = dict(
        operation_id="reschedule-1",
        request_id="request-1",
        old_version_id="version-old",
        new_version_id="version-new",
        order_id=4,
        owner_user_id=OWNER,
        requested_by=OWNER,
        approved_by="persisted-admin-uid",
        source_date="2026-10-23",
        target_date="2026-09-26",
        new_payload=PAYLOAD_NEW,
        claim_token="reschedule-token",
        now=NOW,
    )
    values.update(overrides)
    return reserve_reschedule(conn, **values)


def confirm(conn):
    return confirm_sheet_readback(
        conn,
        operation_id="reschedule-1",
        claim_token="reschedule-token",
        observed_payload=PAYLOAD_NEW,
        now=NOW,
    )


def test_capability_is_legacy_blocked_until_complete_operator_cutover(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    with pytest.raises(PrinterClaimConflict, match="legacy blocked"):
        claim(conn)
    with pytest.raises(PrinterClaimConflict, match="cutover evidence"):
        activate_v2_capability(
            conn, operation_id="bad", store_id=STORE, admin_scope=SCOPE,
            generation=1, cache_cleared=False, all_consumers_confirmed=True,
            evidence_digest="a" * 64, now=NOW,
        )
    cutover(conn)
    assert claim(conn).status == "claimed"


def test_printer_schema_refuses_weakened_dispatch_authority_schema(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    conn.execute("DROP TRIGGER reschedule_dispatch_versions_no_update")
    conn.execute("""CREATE TRIGGER reschedule_dispatch_versions_no_update
        BEFORE UPDATE ON reschedule_dispatch_versions BEGIN SELECT 1; END""")
    conn.commit()
    with pytest.raises(RescheduleConflict, match="schema mismatch"):
        ensure_printer_claim_schema(conn)


def test_claim_binds_current_authoritative_version_owner_store_admin_and_generation(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    cutover(conn)
    for changed in (
        {"version_id": "caller-invented"}, {"owner_user_id": "other"},
        {"store_id": "other"}, {"admin_scope": "wrong"},
        {"capability_generation": 0},
    ):
        with pytest.raises(PrinterClaimConflict):
            claim(conn, operation_id="attempt-" + next(iter(changed)), **changed)
    assert conn.execute("SELECT count(*) FROM printer_dispatch_claims").fetchone()[0] == 0


def test_claim_requires_store_in_immutable_authoritative_payload(tmp_path):
    conn = seed(tmp_path / "db.sqlite3", payload={"rows": PAYLOAD_OLD["rows"]})
    cutover(conn)
    with pytest.raises(PrinterClaimConflict, match="authoritative store"):
        claim(conn)


def test_success_means_transport_accepted_and_exact_ack_replays_only(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    cutover(conn)
    first = claim(conn)
    replay = claim(conn)
    assert replay == first
    result = record_transport_result(
        conn, operation_id="print-1", claim_token=TOKEN,
        outcome="transport_accepted", ack_id="transport-ack-7",
        evidence_digest="b" * 64, now=NOW,
    )
    assert result.status == "transport_accepted"
    replayed_result = record_transport_result(
        conn, operation_id="print-1", claim_token=TOKEN,
        outcome="transport_accepted", ack_id="transport-ack-7",
        evidence_digest="b" * 64, now=LATER,
    )
    assert replayed_result == result
    with pytest.raises(PrinterClaimConflict, match="result replay differs"):
        record_transport_result(
            conn, operation_id="print-1", claim_token=TOKEN,
            outcome="transport_accepted", ack_id="different",
            evidence_digest="b" * 64, now=NOW,
        )
    dump = "\n".join(conn.iterdump())
    assert TOKEN not in dump
    assert OWNER not in conn.execute("SELECT operation_binding_hash FROM printer_dispatch_claims").fetchone()[0]
    assert conn.execute("SELECT token_hash FROM printer_dispatch_claims").fetchone()[0] == hashlib.sha256(TOKEN.encode()).hexdigest()


def test_unknown_and_wrong_token_permanently_fail_closed(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    cutover(conn)
    claim(conn)
    with pytest.raises(PrinterClaimConflict, match="token"):
        record_transport_result(
            conn, operation_id="print-1", claim_token="wrong",
            outcome="outcome_unknown", ack_id="", evidence_digest="c" * 64, now=NOW,
        )
    record_transport_result(
        conn, operation_id="print-1", claim_token=TOKEN,
        outcome="outcome_unknown", ack_id="", evidence_digest="c" * 64, now=NOW,
    )
    with pytest.raises(PrinterClaimConflict, match="already fenced"):
        claim(conn, operation_id="print-2", claim_token="second-token")


def test_expired_lease_becomes_durable_unknown_not_reclaimable(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    cutover(conn)
    claim(conn)
    replay = claim(conn, now=LATER, lease_expires_at="2026-09-26T10:02:00+08:00")
    assert replay.status == "outcome_unknown"
    with pytest.raises(PrinterClaimConflict, match="already fenced"):
        claim(conn, operation_id="print-2", claim_token="second-token", now=LATER,
              lease_expires_at="2026-09-26T10:05:00+08:00")
    result = conn.execute("SELECT outcome FROM printer_dispatch_results WHERE operation_id='print-1'").fetchone()
    assert result[0] == "outcome_unknown"


def test_claim_rejects_pending_reschedule_and_cached_old_version_after_confirmation(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    cutover(conn)
    reserve(conn)
    with pytest.raises(PrinterClaimConflict, match="reschedule"):
        claim(conn)
    confirm(conn)
    with pytest.raises(PrinterClaimConflict, match="current version"):
        claim(conn, operation_id="cached-old")
    assert claim(
        conn, operation_id="new-version", version_id="version-new",
        dispatch_row_id="dispatch-new", service_date="2026-09-26",
    ).version_id == "version-new"


def test_reschedule_rejects_claimed_or_unknown_print_attempt(tmp_path):
    conn = seed(tmp_path / "db.sqlite3")
    cutover(conn)
    claim(conn)
    with pytest.raises(RescheduleConflict, match="printer dispatch"):
        reserve(conn)
    record_transport_result(
        conn, operation_id="print-1", claim_token=TOKEN,
        outcome="outcome_unknown", ack_id="", evidence_digest="c" * 64, now=NOW,
    )
    with pytest.raises(RescheduleConflict, match="printer dispatch"):
        reserve(conn)


@pytest.mark.parametrize("result_outcome", [None, "transport_accepted", "outcome_unknown"])
def test_claim_is_date_scoped_so_unrelated_reschedule_succeeds_but_intersection_blocks(
    tmp_path, result_outcome,
):
    payload = {
        "store_id": STORE,
        "rows": [
            {"dispatch_row_id": "dispatch-a", "service_date": "2026-10-23", "lunch": "A", "dinner": "B"},
            {"dispatch_row_id": "dispatch-b", "service_date": "2026-10-24", "lunch": "C", "dinner": "D"},
        ],
    }
    conn = seed(tmp_path / f"scoped-{result_outcome or 'claimed'}.sqlite3", payload=payload)
    cutover(conn)
    assert claim(conn).service_date == "2026-10-23"
    if result_outcome:
        record_transport_result(
            conn, operation_id="print-1", claim_token=TOKEN,
            outcome=result_outcome,
            ack_id="transport-ack" if result_outcome == "transport_accepted" else "",
            evidence_digest="c" * 64, now=NOW,
        )
    assert reserve(
        conn, source_date="2026-10-24", target_date="2026-10-25",
        new_payload={"store_id": STORE, "rows": []},
    ).status == "pending"

    other = seed(tmp_path / f"intersect-{result_outcome or 'claimed'}.sqlite3", payload=payload)
    cutover(other)
    claim(other)
    with pytest.raises(RescheduleConflict, match="scope"):
        reserve(other, target_date="2026-10-25")


def test_v1_order_wide_claim_migrates_as_manual_hold_without_deleting_history(tmp_path):
    conn = open_db(tmp_path / "legacy-migration.sqlite3")
    ensure_reschedule_dispatch_schema(conn)
    create_initial_version(
        conn, version_id="version-old", order_id=4, owner_user_id=OWNER,
        payload=PAYLOAD_OLD, confirmed_at=NOW,
    )
    conn.executescript("""
      CREATE TABLE printer_dispatch_schema_versions (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version=1), applied_at TEXT NOT NULL);
      INSERT INTO printer_dispatch_schema_versions VALUES(1,'legacy-installed');
      CREATE TABLE printer_dispatch_capability_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE, store_id TEXT NOT NULL,
        admin_scope TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('v2_active')),
        generation INTEGER NOT NULL CHECK(generation > 0),
        cache_cleared INTEGER NOT NULL CHECK(cache_cleared=1),
        all_consumers_confirmed INTEGER NOT NULL CHECK(all_consumers_confirmed=1),
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
        created_at TEXT NOT NULL, UNIQUE(store_id,generation));
      CREATE TABLE printer_dispatch_claims (
        operation_id TEXT PRIMARY KEY NOT NULL, version_id TEXT NOT NULL,
        order_id INTEGER NOT NULL, store_id TEXT NOT NULL,
        capability_generation INTEGER NOT NULL,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        operation_binding_hash TEXT NOT NULL CHECK(length(operation_binding_hash)=64),
        token_hash TEXT NOT NULL CHECK(length(token_hash)=64),
        lease_expires_at TEXT NOT NULL, claimed_at TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES reschedule_dispatch_versions(version_id));
      CREATE UNIQUE INDEX printer_dispatch_one_claim_per_version
        ON printer_dispatch_claims(version_id);
      CREATE TABLE printer_dispatch_results (
        operation_id TEXT PRIMARY KEY NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('transport_accepted','outcome_unknown')),
        ack_hash TEXT NOT NULL,
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
        recorded_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES printer_dispatch_claims(operation_id));
      CREATE TRIGGER printer_dispatch_capability_events_no_update
        BEFORE UPDATE ON printer_dispatch_capability_events
        BEGIN SELECT RAISE(ABORT,'printer capability fact is immutable'); END;
      CREATE TRIGGER printer_dispatch_capability_events_no_delete
        BEFORE DELETE ON printer_dispatch_capability_events
        BEGIN SELECT RAISE(ABORT,'printer capability fact is immutable'); END;
      CREATE TRIGGER printer_dispatch_claims_no_update
        BEFORE UPDATE ON printer_dispatch_claims
        BEGIN SELECT RAISE(ABORT,'printer claim fact is immutable'); END;
      CREATE TRIGGER printer_dispatch_claims_no_delete
        BEFORE DELETE ON printer_dispatch_claims
        BEGIN SELECT RAISE(ABORT,'printer claim fact is immutable'); END;
      CREATE TRIGGER printer_dispatch_results_no_update
        BEFORE UPDATE ON printer_dispatch_results
        BEGIN SELECT RAISE(ABORT,'printer result fact is immutable'); END;
      CREATE TRIGGER printer_dispatch_results_no_delete
        BEFORE DELETE ON printer_dispatch_results
        BEGIN SELECT RAISE(ABORT,'printer result fact is immutable'); END;
    """)
    payload_hash = conn.execute(
        "SELECT payload_hash FROM reschedule_dispatch_versions WHERE version_id='version-old'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO printer_dispatch_claims VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("legacy-print", "version-old", 4, STORE, 1, payload_hash,
         "b" * 64, "c" * 64, LATER, NOW),
    )
    conn.execute(
        "INSERT INTO printer_dispatch_results VALUES(?,?,?,?,?)",
        ("legacy-print", "outcome_unknown", "", "d" * 64, NOW),
    )
    conn.commit()

    ensure_printer_claim_schema(conn)
    conn.commit()

    migrated = conn.execute(
        "SELECT operation_id,dispatch_row_id,service_date,legacy_order_hold "
        "FROM printer_dispatch_claims"
    ).fetchall()
    assert [tuple(row) for row in migrated] == [("legacy-print", "", "", 1)]
    assert conn.execute("SELECT version FROM printer_dispatch_schema_versions").fetchone()[0] == 2
    with pytest.raises(RescheduleConflict, match="printer dispatch"):
        reserve(
            conn, source_date="2026-10-24", target_date="2026-10-25",
            new_payload={"store_id": STORE, "rows": []},
        )
    assert conn.execute(
        "SELECT count(*) FROM printer_dispatch_claims WHERE operation_id='legacy-print'"
    ).fetchone()[0] == 1
    assert tuple(conn.execute(
        "SELECT outcome,evidence_digest FROM printer_dispatch_results "
        "WHERE operation_id='legacy-print'"
    ).fetchone()) == ("outcome_unknown", "d" * 64)


def test_same_meal_scope_cannot_be_reprinted_after_unrelated_version_change(tmp_path):
    initial = {
        "store_id": STORE,
        "rows": [
            {"dispatch_row_id": "dispatch-a", "service_date": "2026-10-23", "lunch": "A", "dinner": "B"},
            {"dispatch_row_id": "dispatch-b", "service_date": "2026-10-24", "lunch": "C", "dinner": "D"},
        ],
    }
    successor = {
        "store_id": STORE,
        "rows": [
            {"dispatch_row_id": "dispatch-a", "service_date": "2026-10-23", "lunch": "A", "dinner": "B"},
            {"dispatch_row_id": "dispatch-c", "service_date": "2026-10-25", "lunch": "C", "dinner": "D"},
        ],
    }
    conn = seed(tmp_path / "cross-version-scope.sqlite3", payload=initial)
    cutover(conn)
    claim(conn)
    reservation = reserve(
        conn, source_date="2026-10-24", target_date="2026-10-25",
        new_payload=successor,
    )
    confirm_sheet_readback(
        conn, operation_id=reservation.operation_id, claim_token="reschedule-token",
        observed_payload=successor, now=NOW,
    )

    with pytest.raises(PrinterClaimConflict, match="current version"):
        claim(conn, operation_id="cached-old-version")
    with pytest.raises(PrinterClaimConflict, match="already fenced"):
        claim(
            conn, operation_id="same-scope-new-version", version_id="version-new",
            dispatch_row_id="dispatch-a", service_date="2026-10-23",
        )
    assert claim(
        conn, operation_id="different-scope-new-version", version_id="version-new",
        dispatch_row_id="dispatch-c", service_date="2026-10-25",
    ).dispatch_row_id == "dispatch-c"


@pytest.mark.parametrize("first_action", ["claim", "reschedule"])
def test_two_real_connections_serialize_claim_and_reschedule_both_directions(tmp_path, first_action):
    path = tmp_path / f"{first_action}.sqlite3"
    setup = seed(path)
    cutover(setup)
    setup.close()
    barrier = threading.Barrier(2)
    release = threading.Event()
    outcomes = []

    def run(action):
        conn = open_db(path)
        barrier.wait()
        if action != first_action:
            release.wait(2)
        try:
            outcomes.append((action, claim(conn) if action == "claim" else reserve(conn)))
        except (PrinterClaimConflict, RescheduleConflict) as exc:
            outcomes.append((action, exc))
        finally:
            if action == first_action:
                release.set()
            conn.close()

    threads = [threading.Thread(target=run, args=(name,)) for name in ("claim", "reschedule")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    winners = [name for name, value in outcomes if not isinstance(value, Exception)]
    assert winners == [first_action]
    check = open_db(path)
    assert check.execute("SELECT count(*) FROM printer_dispatch_claims").fetchone()[0] + check.execute(
        "SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0] == 1


def test_real_two_connection_start_barrier_never_allows_both_writers(tmp_path):
    for attempt in range(20):
        path = tmp_path / f"race-{attempt}.sqlite3"
        setup = seed(path)
        cutover(setup)
        setup.close()
        barrier = threading.Barrier(2)
        outcomes = []

        def race(action):
            conn = open_db(path)
            barrier.wait()
            try:
                value = claim(conn) if action == "claim" else reserve(conn)
                outcomes.append((action, value))
            except (PrinterClaimConflict, RescheduleConflict) as exc:
                outcomes.append((action, exc))
            finally:
                conn.close()

        threads = [threading.Thread(target=race, args=(name,)) for name in ("claim", "reschedule")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert sum(not isinstance(value, Exception) for _, value in outcomes) == 1
        check = open_db(path)
        facts = check.execute("SELECT count(*) FROM printer_dispatch_claims").fetchone()[0]
        facts += check.execute("SELECT count(*) FROM reschedule_dispatch_operations").fetchone()[0]
        assert facts == 1
        check.close()
