"""Offline SQLite printer claim/result fence shared with rescheduling.

This repository is deliberately not an HTTP/device adapter.  It trusts no bearer,
request header, or client assertion and performs no printer, Sheet, LINE, or network
I/O.  A later authenticated adapter must derive order/version/owner from the real
dispatch authority, authorize ``admin_scope``, and keep claim tokens server-side.
The existing dispatch-version repository is the only registration seam: callers
cannot create printer authority records here.

``transport_accepted`` means only that the transport accepted bytes; it is not
proof that paper was produced.  An expired or ambiguous attempt becomes a durable
``outcome_unknown`` and is never made claimable again by this primitive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import sqlite3
from typing import Final

from reschedule_dispatch_versions import ensure_reschedule_dispatch_schema


class PrinterClaimConflict(RuntimeError):
    """The requested printer transition cannot be proven safe."""


@dataclass(frozen=True)
class PrinterClaim:
    operation_id: str
    version_id: str
    order_id: int
    store_id: str
    capability_generation: int
    dispatch_row_id: str
    service_date: str
    lease_expires_at: str
    status: str


@dataclass(frozen=True)
class PrinterResult:
    operation_id: str
    status: str
    recorded_at: str


_DDL: Final[tuple[str, ...]] = (
    """CREATE TABLE printer_dispatch_schema_versions (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version=2),
        applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE printer_dispatch_capability_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        store_id TEXT NOT NULL,
        admin_scope TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('v2_active')),
        generation INTEGER NOT NULL CHECK(generation > 0),
        cache_cleared INTEGER NOT NULL CHECK(cache_cleared=1),
        all_consumers_confirmed INTEGER NOT NULL CHECK(all_consumers_confirmed=1),
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
        created_at TEXT NOT NULL,
        UNIQUE(store_id,generation)
    )""",
    """CREATE TABLE printer_dispatch_claims (
        operation_id TEXT PRIMARY KEY NOT NULL,
        version_id TEXT NOT NULL,
        order_id INTEGER NOT NULL,
        store_id TEXT NOT NULL,
        capability_generation INTEGER NOT NULL,
        dispatch_row_id TEXT NOT NULL,
        service_date TEXT NOT NULL,
        legacy_order_hold INTEGER NOT NULL DEFAULT 0 CHECK(legacy_order_hold IN (0,1)),
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        operation_binding_hash TEXT NOT NULL CHECK(length(operation_binding_hash)=64),
        token_hash TEXT NOT NULL CHECK(length(token_hash)=64),
        lease_expires_at TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES reschedule_dispatch_versions(version_id)
    )""",
    """CREATE UNIQUE INDEX printer_dispatch_one_claim_per_version
        ON printer_dispatch_claims(version_id,dispatch_row_id,service_date)""",
    """CREATE TABLE printer_dispatch_results (
        operation_id TEXT PRIMARY KEY NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('transport_accepted','outcome_unknown')),
        ack_hash TEXT NOT NULL,
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
        recorded_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES printer_dispatch_claims(operation_id)
    )""",
    """CREATE TRIGGER printer_dispatch_capability_events_no_update
        BEFORE UPDATE ON printer_dispatch_capability_events
        BEGIN SELECT RAISE(ABORT,'printer capability fact is immutable'); END""",
    """CREATE TRIGGER printer_dispatch_capability_events_no_delete
        BEFORE DELETE ON printer_dispatch_capability_events
        BEGIN SELECT RAISE(ABORT,'printer capability fact is immutable'); END""",
    """CREATE TRIGGER printer_dispatch_claims_no_update
        BEFORE UPDATE ON printer_dispatch_claims
        BEGIN SELECT RAISE(ABORT,'printer claim fact is immutable'); END""",
    """CREATE TRIGGER printer_dispatch_claims_no_delete
        BEFORE DELETE ON printer_dispatch_claims
        BEGIN SELECT RAISE(ABORT,'printer claim fact is immutable'); END""",
    """CREATE TRIGGER printer_dispatch_results_no_update
        BEFORE UPDATE ON printer_dispatch_results
        BEGIN SELECT RAISE(ABORT,'printer result fact is immutable'); END""",
    """CREATE TRIGGER printer_dispatch_results_no_delete
        BEFORE DELETE ON printer_dispatch_results
        BEGIN SELECT RAISE(ABORT,'printer result fact is immutable'); END""",
)

_NAMES: Final[tuple[str, ...]] = (
    "printer_dispatch_schema_versions",
    "printer_dispatch_capability_events",
    "printer_dispatch_claims",
    "printer_dispatch_one_claim_per_version",
    "printer_dispatch_results",
    "printer_dispatch_capability_events_no_update",
    "printer_dispatch_capability_events_no_delete",
    "printer_dispatch_claims_no_update",
    "printer_dispatch_claims_no_delete",
    "printer_dispatch_results_no_update",
    "printer_dispatch_results_no_delete",
)
_EXPECTED = dict(zip(_NAMES, _DDL))


def _fingerprint(sql: str | None) -> str:
    return "".join((sql or "").split()).lower()


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PrinterClaimConflict("timestamp must be canonical aware ISO seconds") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat(timespec="seconds") != value:
        raise PrinterClaimConflict("timestamp must be canonical aware ISO seconds")
    return value


def _full_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PrinterClaimConflict("service date must be full ISO year date") from exc
    if parsed.isoformat() != value:
        raise PrinterClaimConflict("service date must be full ISO year date")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_digest(value: str) -> str:
    if len(value) != 64:
        raise PrinterClaimConflict("evidence digest must be SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PrinterClaimConflict("evidence digest must be SHA-256 hex") from exc
    return value.lower()


def _schema_rows(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE name LIKE 'printer_dispatch_%'
                   AND type IN ('table','index','trigger')"""
        )
        if not str(row[0]).startswith("sqlite_autoindex")
    }


def _verify_schema(conn: sqlite3.Connection) -> None:
    rows = _schema_rows(conn)
    if set(rows) != set(_NAMES):
        raise PrinterClaimConflict("printer dispatch schema is partial or unknown")
    for name, expected in _EXPECTED.items():
        if _fingerprint(rows.get(name)) != _fingerprint(expected):
            raise PrinterClaimConflict(f"printer dispatch schema mismatch: {name}")
    versions = conn.execute(
        "SELECT version,typeof(version) FROM printer_dispatch_schema_versions"
    ).fetchall()
    if [tuple(row) for row in versions] != [(2, "integer")]:
        raise PrinterClaimConflict("printer dispatch schema version mismatch")
    expected_columns = {
        "printer_dispatch_capability_events": 10,
        "printer_dispatch_claims": 13,
        "printer_dispatch_results": 5,
    }
    for table, count in expected_columns.items():
        if len(conn.execute(f"PRAGMA table_xinfo({table})").fetchall()) != count:
            raise PrinterClaimConflict(f"printer dispatch schema mismatch: {table}")


def ensure_printer_claim_schema(conn: sqlite3.Connection) -> None:
    """Install/verify an additive, fingerprinted, append-only schema."""
    # Verify the exact authority schema before layering printer facts on it;
    # merely finding a same-named FK target is not sufficient authority.
    ensure_reschedule_dispatch_schema(conn)
    savepoint = "ensure_printer_dispatch_v1"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        installed = _schema_rows(conn)
        if installed:
            marker = conn.execute(
                "SELECT version,typeof(version) FROM printer_dispatch_schema_versions"
            ).fetchall() if "printer_dispatch_schema_versions" in installed else []
            columns = conn.execute("PRAGMA table_xinfo(printer_dispatch_claims)").fetchall()
            legacy_columns = [str(row[1]) for row in columns]
            expected_legacy_columns = [
                "operation_id", "version_id", "order_id", "store_id",
                "capability_generation", "payload_hash", "operation_binding_hash",
                "token_hash", "lease_expires_at", "claimed_at",
            ]
            if [tuple(row) for row in marker] == [(1, "integer")] and legacy_columns == expected_legacy_columns:
                # Rebuild instead of ALTER-append: the canonical v2 fingerprint fixes
                # column order/defaults.  Every predecessor claim is copied verbatim
                # and marked as an explicit order-wide manual hold; result history is
                # copied too, so migration can never release or erase an old attempt.
                conn.execute("DROP TRIGGER printer_dispatch_claims_no_update")
                conn.execute("DROP TRIGGER printer_dispatch_claims_no_delete")
                conn.execute("DROP TRIGGER printer_dispatch_results_no_update")
                conn.execute("DROP TRIGGER printer_dispatch_results_no_delete")
                conn.execute("DROP INDEX printer_dispatch_one_claim_per_version")
                conn.execute("ALTER TABLE printer_dispatch_results RENAME TO printer_dispatch_results_v1")
                conn.execute("ALTER TABLE printer_dispatch_claims RENAME TO printer_dispatch_claims_v1")
                conn.execute(_DDL[2])
                conn.execute(_DDL[3])
                conn.execute(_DDL[4])
                conn.execute(
                    """INSERT INTO printer_dispatch_claims
                       (operation_id,version_id,order_id,store_id,capability_generation,
                        dispatch_row_id,service_date,legacy_order_hold,payload_hash,
                        operation_binding_hash,token_hash,lease_expires_at,claimed_at)
                       SELECT operation_id,version_id,order_id,store_id,capability_generation,
                              '','',1,payload_hash,operation_binding_hash,token_hash,
                              lease_expires_at,claimed_at
                         FROM printer_dispatch_claims_v1"""
                )
                conn.execute(
                    """INSERT INTO printer_dispatch_results
                       (operation_id,outcome,ack_hash,evidence_digest,recorded_at)
                       SELECT operation_id,outcome,ack_hash,evidence_digest,recorded_at
                         FROM printer_dispatch_results_v1"""
                )
                conn.execute("DROP TABLE printer_dispatch_results_v1")
                conn.execute("DROP TABLE printer_dispatch_claims_v1")
                for statement in _DDL[7:]:
                    conn.execute(statement)
                conn.execute("ALTER TABLE printer_dispatch_schema_versions RENAME TO printer_dispatch_schema_versions_v1")
                conn.execute(_DDL[0])
                conn.execute("INSERT INTO printer_dispatch_schema_versions VALUES(2,datetime('now'))")
                conn.execute("DROP TABLE printer_dispatch_schema_versions_v1")
            elif [tuple(row) for row in marker] != [(2, "integer")]:
                raise PrinterClaimConflict("printer dispatch schema is partial or unknown")
            _verify_schema(conn)
        else:
            # The FK target proves this primitive is layered on the version authority.
            target = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reschedule_dispatch_versions'"
            ).fetchone()
            if not target:
                raise PrinterClaimConflict("reschedule dispatch authority schema is required")
            for statement in _DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO printer_dispatch_schema_versions VALUES(2,?)",
                (datetime.now().astimezone().isoformat(timespec="seconds"),),
            )
            _verify_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def activate_v2_capability(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    store_id: str,
    admin_scope: str,
    generation: int,
    cache_cleared: bool,
    all_consumers_confirmed: bool,
    evidence_digest: str,
    now: str,
) -> int:
    """Record controlled cutover evidence; absence of a fact means legacy blocked."""
    when = _timestamp(now)
    evidence = _evidence_digest(evidence_digest)
    if conn.in_transaction:
        raise PrinterClaimConflict("capability cutover requires transaction ownership")
    if not all((operation_id, store_id, admin_scope)) or isinstance(generation, bool) or generation < 1:
        raise PrinterClaimConflict("capability cutover binding is incomplete")
    if cache_cleared is not True or all_consumers_confirmed is not True:
        raise PrinterClaimConflict("cutover evidence requires cache clear and all consumers confirmed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = conn.execute(
            """SELECT store_id,admin_scope,generation,cache_cleared,
                      all_consumers_confirmed,evidence_digest,created_at
                 FROM printer_dispatch_capability_events WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        expected = (store_id, admin_scope, generation, 1, 1, evidence, when)
        if replay:
            if tuple(replay) != expected:
                raise PrinterClaimConflict("capability operation replay differs")
            conn.commit()
            return generation
        latest = conn.execute(
            """SELECT generation FROM printer_dispatch_capability_events
                 WHERE store_id=? ORDER BY generation DESC LIMIT 1""",
            (store_id,),
        ).fetchone()
        if latest and generation <= int(latest[0]):
            raise PrinterClaimConflict("capability generation must increase")
        conn.execute(
            """INSERT INTO printer_dispatch_capability_events
               (operation_id,store_id,admin_scope,state,generation,cache_cleared,
                all_consumers_confirmed,evidence_digest,created_at)
               VALUES(?,?,?,'v2_active',?,1,1,?,?)""",
            (operation_id, store_id, admin_scope, generation, evidence, when),
        )
        conn.commit()
        return generation
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PrinterClaimConflict("capability cutover conflicts with durable state") from exc
    except Exception:
        conn.rollback()
        raise


def _claim_from_row(row: sqlite3.Row | tuple, status: str) -> PrinterClaim:
    return PrinterClaim(
        operation_id=str(row[0]), version_id=str(row[1]), order_id=int(row[2]),
        store_id=str(row[3]), capability_generation=int(row[4]),
        dispatch_row_id=str(row[5]), service_date=str(row[6]),
        lease_expires_at=str(row[7]), status=status,
    )


def _binding_hash(*, operation_id: str, version_id: str, order_id: int,
                  owner_user_id: str, store_id: str, admin_scope: str,
                  generation: int, dispatch_row_id: str, service_date: str,
                  payload_hash: str, lease_expires_at: str) -> str:
    # Only the digest is persisted; owner/admin input is never duplicated into claim facts.
    fields = (operation_id, version_id, str(order_id), owner_user_id, store_id,
              admin_scope, str(generation), dispatch_row_id, service_date,
              payload_hash, lease_expires_at)
    return _digest("\x1f".join(fields))


def claim_current_dispatch(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    version_id: str,
    order_id: int,
    owner_user_id: str,
    store_id: str,
    admin_scope: str,
    capability_generation: int,
    claim_token: str,
    dispatch_row_id: str,
    service_date: str,
    lease_expires_at: str,
    now: str,
) -> PrinterClaim:
    """Claim exactly one current confirmed version, committing before any I/O."""
    when = _timestamp(now)
    expiry = _timestamp(lease_expires_at)
    service_date = _full_date(service_date)
    if conn.in_transaction:
        raise PrinterClaimConflict("printer claim requires transaction ownership")
    if not all((operation_id, version_id, owner_user_id, store_id, admin_scope, claim_token, dispatch_row_id)):
        raise PrinterClaimConflict("printer claim binding is incomplete")
    if isinstance(order_id, bool) or order_id < 1 or isinstance(capability_generation, bool):
        raise PrinterClaimConflict("printer claim identity is invalid")

    token_hash = _digest(claim_token)
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = list(conn.execute(
            """SELECT v.version_id,v.order_id,v.owner_user_id,v.payload_hash,v.payload_json
                 FROM reschedule_dispatch_versions v
                 JOIN reschedule_dispatch_confirmations c ON c.version_id=v.version_id
                WHERE v.order_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM reschedule_dispatch_supersessions s
                    JOIN reschedule_dispatch_operations o ON o.operation_id=s.operation_id
                    WHERE s.old_version_id=v.version_id AND o.status='confirmed'
                  )""",
            (order_id,),
        ))
        if len(current) != 1 or str(current[0][0]) != version_id:
            raise PrinterClaimConflict("requested version is not the single current version")
        authoritative = current[0]
        if int(authoritative[1]) != order_id or str(authoritative[2]) != owner_user_id:
            raise PrinterClaimConflict("current version owner binding differs")
        payload_hash = str(authoritative[3])
        try:
            authoritative_payload = json.loads(str(authoritative[4]))
        except (TypeError, ValueError) as exc:
            raise PrinterClaimConflict("authoritative dispatch payload is invalid") from exc
        if not isinstance(authoritative_payload, dict) or authoritative_payload.get("store_id") != store_id:
            raise PrinterClaimConflict("authoritative store binding differs")
        payload_rows = authoritative_payload.get("rows")
        if not isinstance(payload_rows, list) or len([
            row for row in payload_rows
            if isinstance(row, dict)
            and str(row.get("dispatch_row_id")) == dispatch_row_id
            and str(row.get("service_date")) == service_date
        ]) != 1:
            raise PrinterClaimConflict("dispatch claim scope is not in current authority")

        # The optional bridge has no route and does not enable itself merely by
        # existing.  Once installed, however, a claim must resolve to immutable
        # persisted dispatch rows + publication receipts; synthetic/unbound version
        # facts are not printer authority.
        try:
            from dispatch_authority_bridge import (
                DispatchAuthorityConflict,
                validate_claim_authority_version,
            )
            validate_claim_authority_version(
                conn, version_id=version_id, order_id=order_id,
                owner_user_id=owner_user_id, store_id=store_id,
            )
        except DispatchAuthorityConflict as exc:
            raise PrinterClaimConflict("dispatch authority version binding is invalid") from exc

        binding_hash = _binding_hash(
            operation_id=operation_id, version_id=version_id, order_id=order_id,
            owner_user_id=owner_user_id, store_id=store_id, admin_scope=admin_scope,
            generation=capability_generation, dispatch_row_id=dispatch_row_id,
            service_date=service_date, payload_hash=payload_hash,
            lease_expires_at=expiry,
        )

        replay = conn.execute(
            """SELECT operation_id,version_id,order_id,store_id,capability_generation,
                      dispatch_row_id,service_date,lease_expires_at,operation_binding_hash,token_hash
                 FROM printer_dispatch_claims WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        if replay:
            if str(replay[8]) != binding_hash or str(replay[9]) != token_hash:
                raise PrinterClaimConflict("printer claim operation replay differs")
            result = conn.execute(
                "SELECT outcome FROM printer_dispatch_results WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not result and datetime.fromisoformat(str(replay[7])) <= datetime.fromisoformat(when):
                evidence = _digest("lease-expired-without-authoritative-result:" + operation_id)
                conn.execute(
                    """INSERT INTO printer_dispatch_results
                       (operation_id,outcome,ack_hash,evidence_digest,recorded_at)
                       VALUES(?,'outcome_unknown','',?,?)""",
                    (operation_id, evidence, when),
                )
                status = "outcome_unknown"
            else:
                status = str(result[0]) if result else "claimed"
            conn.commit()
            return _claim_from_row(replay[:8], status)

        if datetime.fromisoformat(expiry) <= datetime.fromisoformat(when):
            raise PrinterClaimConflict("claim lease must end after claim time")

        capability = conn.execute(
            """SELECT admin_scope,generation,state FROM printer_dispatch_capability_events
                 WHERE store_id=? ORDER BY generation DESC LIMIT 1""",
            (store_id,),
        ).fetchone()
        if not capability:
            raise PrinterClaimConflict("printer capability is legacy blocked")
        if tuple(capability) != (admin_scope, capability_generation, "v2_active"):
            raise PrinterClaimConflict("printer store/admin scope or capability generation differs")

        pending = conn.execute(
            """SELECT 1 FROM reschedule_dispatch_operations
                 WHERE order_id=? AND status IN ('pending','sheet_unknown','manual_hold')
                   AND (source_date=? OR target_date=?) LIMIT 1""",
            (order_id, service_date, service_date),
        ).fetchone()
        if pending:
            raise PrinterClaimConflict("active reschedule fences printer claim")

        prior = conn.execute(
            """SELECT c.operation_id,c.lease_expires_at,r.outcome
                 FROM printer_dispatch_claims c
                 LEFT JOIN printer_dispatch_results r ON r.operation_id=c.operation_id
                WHERE c.order_id=?
                  AND (c.legacy_order_hold=1 OR c.dispatch_row_id=? OR c.service_date=?)
                LIMIT 1""",
            (order_id, dispatch_row_id, service_date),
        ).fetchone()
        if prior:
            if prior[2] is None and datetime.fromisoformat(str(prior[1])) <= datetime.fromisoformat(when):
                evidence = _digest("lease-expired-without-authoritative-result:" + str(prior[0]))
                conn.execute(
                    """INSERT INTO printer_dispatch_results
                       (operation_id,outcome,ack_hash,evidence_digest,recorded_at)
                       VALUES(?,'outcome_unknown','',?,?)""",
                    (prior[0], evidence, when),
                )
                conn.commit()
                raise PrinterClaimConflict("expired printer lease is now durable unknown; manual verification required")
            raise PrinterClaimConflict("order is already fenced by a printer dispatch attempt")

        conn.execute(
            """INSERT INTO printer_dispatch_claims
               (operation_id,version_id,order_id,store_id,capability_generation,
                dispatch_row_id,service_date,legacy_order_hold,payload_hash,
                operation_binding_hash,token_hash,lease_expires_at,claimed_at)
               VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?)""",
            (operation_id, version_id, order_id, store_id, capability_generation,
             dispatch_row_id, service_date, payload_hash, binding_hash, token_hash, expiry, when),
        )
        conn.commit()
        return PrinterClaim(operation_id, version_id, order_id, store_id,
                            capability_generation, dispatch_row_id, service_date,
                            expiry, "claimed")
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PrinterClaimConflict("printer claim conflicts with durable state") from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def record_transport_result(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    claim_token: str,
    outcome: str,
    ack_id: str,
    evidence_digest: str,
    now: str,
) -> PrinterResult:
    """Append an accepted/unknown result; no client-provided 'not sent' unlock exists."""
    when = _timestamp(now)
    evidence = _evidence_digest(evidence_digest)
    if outcome not in ("transport_accepted", "outcome_unknown"):
        raise PrinterClaimConflict("unsupported printer result; verified-not-sent needs a trusted future adapter")
    if outcome == "transport_accepted" and not ack_id:
        raise PrinterClaimConflict("transport acceptance ack is required")
    if outcome == "outcome_unknown" and ack_id:
        raise PrinterClaimConflict("unknown result cannot claim transport acceptance")
    if not operation_id or not claim_token:
        raise PrinterClaimConflict("printer result binding is incomplete")
    if conn.in_transaction:
        raise PrinterClaimConflict("printer result requires transaction ownership")
    ack_hash = _digest(ack_id) if ack_id else ""
    token_hash = _digest(claim_token)
    conn.execute("BEGIN IMMEDIATE")
    try:
        claim = conn.execute(
            "SELECT token_hash FROM printer_dispatch_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if not claim or str(claim[0]) != token_hash:
            raise PrinterClaimConflict("printer result claim token differs")
        replay = conn.execute(
            """SELECT outcome,ack_hash,evidence_digest,recorded_at
                 FROM printer_dispatch_results WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        expected = (outcome, ack_hash, evidence)
        if replay:
            if tuple(replay[:3]) != expected:
                raise PrinterClaimConflict("printer result replay differs")
            conn.commit()
            return PrinterResult(operation_id, outcome, str(replay[3]))
        conn.execute(
            """INSERT INTO printer_dispatch_results
               (operation_id,outcome,ack_hash,evidence_digest,recorded_at)
               VALUES(?,?,?,?,?)""",
            (operation_id, outcome, ack_hash, evidence, when),
        )
        conn.commit()
        return PrinterResult(operation_id, outcome, when)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PrinterClaimConflict("printer result conflicts with durable state") from exc
    except Exception:
        conn.rollback()
        raise
