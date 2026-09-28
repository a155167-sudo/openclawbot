from __future__ import annotations

import json
import hashlib
import itertools
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    from vip_health_check import configure_vip_health_check_connection

    configure_vip_health_check_connection(connection)
    yield connection
    connection.close()


def test_connection_helper_enables_and_requires_foreign_keys_before_transaction():
    from vip_health_check import (
        configure_vip_health_check_connection,
        require_vip_health_check_connection,
    )

    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert configure_vip_health_check_connection(connection) is connection
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert require_vip_health_check_connection(connection) is connection
    finally:
        connection.close()


def test_connection_helper_fails_closed_when_transaction_started_with_fk_off():
    from vip_health_check import configure_vip_health_check_connection

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match="foreign_keys"):
            configure_vip_health_check_connection(connection)
        assert connection.in_transaction is True
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        connection.rollback()
        connection.close()


def test_startup_accepts_legacy_delivery_status_contract_with_outcome_unknown():
    from vip_health_check import configure_vip_health_check_connection, ensure_vip_health_check_schema

    connection = sqlite3.connect(":memory:")
    try:
        configure_vip_health_check_connection(connection)
        ensure_vip_health_check_schema(connection)
        connection.execute("DROP TABLE vip_health_check_deliveries")
        connection.executescript(
            """
            CREATE TABLE vip_health_check_deliveries (
                delivery_id TEXT PRIMARY KEY NOT NULL,
                report_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                delivery_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','outcome_unknown','failed','delivered')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
            );
            CREATE INDEX idx_vip_health_check_deliveries_status
                ON vip_health_check_deliveries(status, created_at);
            """
        )
        connection.commit()
        ensure_vip_health_check_schema(connection)
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='vip_health_check_deliveries'"
        ).fetchone()[0].count("outcome_unknown") == 1
    finally:
        connection.close()


def test_vip_write_fails_before_transaction_when_connection_was_not_configured():
    from vip_health_check import create_first_vip_health_check_case

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE vip_health_check_cases(case_id TEXT)")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="foreign_keys"):
            create_first_vip_health_check_case(
                connection,
                user_id="U1",
                first_vip_activation_id="activation-1",
                activation_event_key="event-1",
                activated_at=datetime.now(timezone.utc),
            )
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_feature_flag_is_off_by_default_and_requires_explicit_truthy_value():
    from vip_health_check import is_vip_health_check_enabled

    assert is_vip_health_check_enabled({}) is False
    assert is_vip_health_check_enabled({"VIP_HEALTH_CHECK_ENABLED": "false"}) is False
    assert is_vip_health_check_enabled({"VIP_HEALTH_CHECK_ENABLED": "true"}) is True
    assert is_vip_health_check_enabled({"VIP_HEALTH_CHECK_ENABLED": "1"}) is True


def test_schema_creates_isolated_health_check_tables_with_required_unique_constraints(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "vip_health_check_activation_events",
        "vip_health_check_cases",
        "vip_health_check_valid_days",
        "vip_health_check_source_refs",
        "vip_health_check_reviews",
        "vip_health_check_reports",
        "vip_health_check_deliveries",
        "vip_health_check_audit_log",
        "dietitian_coaching_orders",
    } <= tables

    with pytest.raises(sqlite3.IntegrityError):
        conn.executescript(
            """
            INSERT INTO vip_health_check_cases
              (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
               window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
            VALUES
              ('case-1','U1','first_vip_baseline_check','act-1','event-1',
               '2026-09-03T09:00:00+08:00','2026-09-10T09:00:00+08:00','collecting',0,
               '2026-09-03T09:00:00+08:00','2026-09-03T09:00:00+08:00'),
              ('case-2','U1','first_vip_baseline_check','act-2','event-2',
               '2026-09-04T09:00:00+08:00','2026-09-11T09:00:00+08:00','collecting',0,
               '2026-09-04T09:00:00+08:00','2026-09-04T09:00:00+08:00');
            """
        )


@pytest.mark.parametrize(
    ("relation", "sql"),
    [
        (
            "valid_days.case_id",
            "INSERT INTO vip_health_check_valid_days VALUES "
            "('missing','2026-09-03','v1',2,'qualified','2026-09-03T10:00:00+08:00')",
        ),
        (
            "source_refs.case_id",
            "INSERT INTO vip_health_check_source_refs VALUES "
            "('missing','log-1',1,'2026-09-03','reason','hash','2026-09-03T10:00:00+08:00')",
        ),
        (
            "reviews.case_id",
            "INSERT INTO vip_health_check_reviews "
            "(review_id,case_id,review_version,status,source_manifest_hash,created_at,updated_at) "
            "VALUES ('orphan-review','missing',2,'draft','hash','now','now')",
        ),
        (
            "reports.case_id",
            "INSERT INTO vip_health_check_reports "
            "(report_id,case_id,review_id,report_version,report_json,source_manifest_hash,published_by,published_at) "
            "VALUES ('orphan-case-report','missing','valid-review',1,'{}','hash','dietitian','now')",
        ),
        (
            "reports.review_id",
            "INSERT INTO vip_health_check_reports "
            "(report_id,case_id,review_id,report_version,report_json,source_manifest_hash,published_by,published_at) "
            "VALUES ('orphan-review-report','valid-case','missing',1,'{}','hash','dietitian','now')",
        ),
        (
            "deliveries.report_id",
            "INSERT INTO vip_health_check_deliveries "
            "(delivery_id,report_id,user_id,delivery_key,created_at) "
            "VALUES ('orphan-delivery','missing','U1','delivery-key','now')",
        ),
        (
            "audit.case_id",
            "INSERT INTO vip_health_check_audit_log "
            "(case_id,actor_type,to_status,created_at) VALUES ('missing','system','collecting','now')",
        ),
        (
            "coaching.case_id",
            "INSERT INTO dietitian_coaching_orders "
            "(order_id,user_id,case_id,operation_key,requested_at,updated_at) "
            "VALUES ('orphan-order','U1','missing','operation-key','now','now')",
        ),
    ],
)
def test_schema_rejects_every_vip_health_check_orphan(conn, relation, sql):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,created_at,updated_at)
           VALUES ('valid-case','U-valid','first_vip_baseline_check','activation-valid',
                   'event-valid','start','end','now','now')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('valid-review','valid-case',1,'approved','hash','dietitian','now','now','now')"""
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == [], relation


def test_schema_upgrades_earlier_reports_table_with_review_link(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.executescript(
        """
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version)
        );
        """
    )

    ensure_vip_health_check_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(vip_health_check_reports)")}
    assert "review_id" in columns
    conn.executescript(
        """
        INSERT INTO vip_health_check_cases
          (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
           window_started_at,window_ends_at,created_at,updated_at)
        VALUES
          ('c1','U-c1','first_vip_baseline_check','a-c1','e-c1','start','end','now','now'),
          ('c2','U-c2','first_vip_baseline_check','a-c2','e-c2','start','end','now','now');
        INSERT INTO vip_health_check_reviews
          (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
           approved_at,created_at,updated_at)
        VALUES ('review-1','c1',1,'approved','manifest','dietitian','now','now','now');
        """
    )
    values = ("{}", "manifest", "dietitian", "2026-09-03T10:00:00+08:00")
    conn.execute(
        """INSERT INTO vip_health_check_reports
           (report_id,case_id,review_id,report_kind,report_version,report_json,
            source_manifest_hash,published_by,published_at)
           VALUES ('r1','c1','review-1','baseline_3day',1,?,?,?,?)""",
        values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,review_id,report_kind,report_version,report_json,
                source_manifest_hash,published_by,published_at)
               VALUES ('r2','c2','review-1','baseline_3day',1,?,?,?,?)""",
            values,
        )


class _FailingMigrationConnection(sqlite3.Connection):
    fail_on_report_review_index = False
    fail_on_full_graph_report_restore = False
    fail_on_schema_release = False

    def execute(self, sql, parameters=()):
        if (
            self.fail_on_schema_release
            and sql.startswith("RELEASE SAVEPOINT vip_health_check_schema_")
        ):
            self.fail_on_schema_release = False
            raise sqlite3.IntegrityError("forced schema release failure")
        if (
            self.fail_on_report_review_index
            and (
                "INSERT INTO vip_health_check_reports_rebuild_" in sql
                or (
                    'INSERT INTO "vip_health_check_reports"' in sql
                    and 'temp."vip_c566_stage_' in sql
                )
            )
        ):
            raise sqlite3.IntegrityError("forced report rebuild failure")
        if (
            self.fail_on_full_graph_report_restore
            and 'INSERT INTO "vip_health_check_reports"' in sql
            and 'temp."vip_c566_stage_' in sql
        ):
            raise sqlite3.IntegrityError("forced full graph rebuild failure")
        return super().execute(sql, parameters)


def _prepare_legacy_report_migration_failure_connection():
    from vip_health_check import ensure_vip_health_check_schema

    connection = sqlite3.connect(":memory:", factory=_FailingMigrationConnection)
    ensure_vip_health_check_schema(connection)
    connection.executescript(
        """
        INSERT INTO vip_health_check_cases
          (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
           window_started_at,window_ends_at,source_manifest_hash,created_at,updated_at)
        VALUES ('legacy-case','U-legacy','first_vip_baseline_check','legacy-activation',
                'legacy-event','start','end','manifest','now','now');
        INSERT INTO vip_health_check_reviews
          (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
           approved_at,created_at,updated_at)
        VALUES ('legacy-review','legacy-case',1,'approved','manifest','dietitian',
                'now','now','now');
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version)
        );
        CREATE TRIGGER vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN
            SELECT RAISE(ABORT, 'published health-check reports are immutable');
        END;
        INSERT INTO vip_health_check_reports
          (report_id,case_id,report_kind,report_version,report_json,
           source_manifest_hash,published_by,published_at)
        VALUES
          ('legacy-report','legacy-case','baseline_3day',1,'{}','manifest',
           'dietitian','2026-09-03T10:00:00+08:00');
        CREATE TABLE migration_sentinel(value TEXT NOT NULL);
        """
    )
    return connection


def test_schema_migration_failure_does_not_commit_caller_transaction():
    from vip_health_check import ensure_vip_health_check_schema

    connection = _prepare_legacy_report_migration_failure_connection()
    try:
        connection.execute("BEGIN")
        connection.execute("INSERT INTO migration_sentinel VALUES ('must-rollback')")
        connection.fail_on_report_review_index = True

        with pytest.raises(sqlite3.IntegrityError, match="forced report rebuild failure"):
            ensure_vip_health_check_schema(connection)
        connection.rollback()

        assert connection.execute("SELECT COUNT(*) FROM migration_sentinel").fetchone()[0] == 0
    finally:
        connection.close()


def test_schema_migration_failure_restores_strict_report_immutability():
    from vip_health_check import ensure_vip_health_check_schema

    connection = _prepare_legacy_report_migration_failure_connection()
    try:
        connection.fail_on_report_review_index = True
        with pytest.raises(sqlite3.IntegrityError, match="forced report rebuild failure"):
            ensure_vip_health_check_schema(connection)
        connection.rollback()

        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='vip_health_check_reports_no_update'"
        ).fetchone()[0]
        assert "WHEN" not in trigger_sql.upper()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="published health-check reports are immutable",
        ):
            connection.execute(
                "UPDATE vip_health_check_reports SET report_json='arbitrary-mutation' "
                "WHERE report_id='legacy-report'"
            )
    finally:
        connection.close()


def test_schema_report_review_backfill_is_idempotent(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,status,valid_day_count,source_manifest_hash,
            created_at,updated_at)
           VALUES ('legacy-case','U-legacy','first_vip_baseline_check','activation-legacy',
                   'event-legacy','2026-09-03T09:00:00+08:00',
                   '2026-09-10T09:00:00+08:00','approved_pending_delivery',0,'manifest',
                   '2026-09-03T09:00:00+08:00','2026-09-03T10:00:00+08:00')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('approved-review','legacy-case',1,'approved','manifest','dietitian',
                   '2026-09-03T10:00:00+08:00','2026-09-03T09:30:00+08:00',
                   '2026-09-03T10:00:00+08:00')"""
    )
    conn.executescript(
        """
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version)
        );
        INSERT INTO vip_health_check_reports
          (report_id,case_id,report_kind,report_version,report_json,
           source_manifest_hash,published_by,published_at)
        VALUES ('legacy-report','legacy-case','baseline_3day',1,'{}','manifest',
                'dietitian','2026-09-03T10:00:00+08:00');
        """
    )

    ensure_vip_health_check_schema(conn)
    ensure_vip_health_check_schema(conn)

    assert conn.execute(
        "SELECT review_id FROM vip_health_check_reports WHERE report_id='legacy-report'"
    ).fetchone()[0] == "approved-review"
    assert conn.execute(
        "SELECT COUNT(*) FROM vip_health_check_reports WHERE report_id='legacy-report'"
    ).fetchone()[0] == 1


def _install_c566_reports_schema(conn, *, report_id="c566-report", report_kind="baseline_3day"):
    """Install the exact shipped c566 reports predecessor, including its FKs/objects."""
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.executescript(
        """
        INSERT INTO vip_health_check_cases
          (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
           window_started_at,window_ends_at,status,source_manifest_hash,created_at,updated_at)
        VALUES ('c566-case','case-owner','first_vip_baseline_check','c566-act','c566-event',
                'start','end','delivered','c566-manifest','created','updated');
        INSERT INTO vip_health_check_reviews
          (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
           approved_at,created_at,updated_at)
        VALUES ('c566-review','c566-case',1,'approved','c566-manifest','dietitian',
                'approved','created','updated');
        DROP TABLE vip_health_check_deliveries;
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            review_id TEXT NOT NULL UNIQUE,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id, report_kind, report_version),
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
            FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id),
            FOREIGN KEY(case_id, review_id)
                REFERENCES vip_health_check_reviews(case_id, review_id)
        );
        CREATE TRIGGER vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
        CREATE TRIGGER vip_health_check_reports_no_delete
        BEFORE DELETE ON vip_health_check_reports
        BEGIN SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
        CREATE TABLE vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','failed','delivered')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        );
        CREATE INDEX idx_vip_health_check_deliveries_status
            ON vip_health_check_deliveries(status, created_at);
        """
    )
    conn.execute(
        """INSERT INTO vip_health_check_reports
           (report_id,case_id,review_id,report_kind,report_version,report_json,
            source_manifest_hash,published_by,published_at)
           VALUES (?,'c566-case','c566-review',?,1,'{"good":"preserve exactly"}',
                   'c566-manifest','dietitian','published')""",
        (report_id, report_kind),
    )
    if report_id is not None:
        conn.execute(
            """INSERT INTO vip_health_check_deliveries
               (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
                created_at,delivered_at)
               VALUES ('c566-delivery',?,'historical-other-recipient','c566-key','delivered',
                       3,'historical-error','created','delivered')""",
            (report_id,),
        )
    conn.commit()


def _install_c566_full_graph_schema(conn):
    """Build the shipped nullable-PK schema with every inbound case/report FK lane populated."""
    _install_c566_reports_schema(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    for table_name, primary_key, indexes in (
        ("vip_health_check_cases", "case_id", (
            "CREATE INDEX idx_vip_health_check_cases_status "
            "ON vip_health_check_cases(status,updated_at)",
        )),
        ("vip_health_check_reviews", "review_id", (
            "CREATE UNIQUE INDEX idx_vip_health_check_reviews_case_review "
            "ON vip_health_check_reviews(case_id,review_id)",
        )),
        ("vip_health_check_deliveries", "delivery_id", (
            "CREATE INDEX idx_vip_health_check_deliveries_status "
            "ON vip_health_check_deliveries(status,created_at)",
        )),
        ("dietitian_coaching_orders", "order_id", ()),
    ):
        original_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()[0]
        columns = [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table_name}")')]
        column_list = ",".join(f'"{column}"' for column in columns)
        backup = table_name + "_canonical_fixture"
        conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{backup}"')
        predecessor_sql = original_sql.replace(
            f"{primary_key} TEXT PRIMARY KEY NOT NULL",
            f"{primary_key} TEXT PRIMARY KEY",
        )
        if table_name == "vip_health_check_reviews":
            predecessor_sql = predecessor_sql.replace(
                "            UNIQUE(case_id, review_id),\n", ""
            ).replace(", UNIQUE(case_id, review_id)", "")
        conn.execute(predecessor_sql)
        conn.execute(
            f'INSERT INTO "{table_name}" ({column_list}) '
            f'SELECT {column_list} FROM "{backup}"'
        )
        conn.execute(f'DROP TABLE "{backup}"')
        for index_sql in indexes:
            conn.execute(index_sql)
    conn.execute(
        """CREATE TABLE vip_health_check_notifications (
            notification_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
            notification_kind TEXT NOT NULL
              CHECK(notification_kind='dietitian_ready_for_review'),
            status TEXT NOT NULL DEFAULT 'pending'
              CHECK(status IN ('pending','sending','delivered')),
            attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '', UNIQUE(case_id,notification_kind),
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id))"""
    )
    conn.execute(
        "CREATE INDEX idx_vip_health_check_notifications_status "
        "ON vip_health_check_notifications(status,updated_at)"
    )
    conn.execute(
        "INSERT INTO vip_health_check_valid_days VALUES "
        "('c566-case','day','v1',3,'complete','evaluated')"
    )
    conn.execute(
        "INSERT INTO vip_health_check_source_refs VALUES "
        "('c566-case','food-log',7,'day','included','source-hash','created')"
    )
    conn.execute(
        """INSERT INTO vip_health_check_audit_log
           (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
           VALUES ('c566-case','system','','approved_pending_delivery','delivered','','created')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_notifications
           (notification_id,case_id,notification_kind,status,created_at,updated_at)
           VALUES ('notice','c566-case','dietitian_ready_for_review','delivered','created','updated')"""
    )
    conn.execute(
        """INSERT INTO dietitian_coaching_orders
           (order_id,user_id,case_id,operation_key,requested_at,updated_at)
           VALUES ('order','case-owner','c566-case','operation','requested','updated')"""
    )
    conn.commit()
    conn.execute("PRAGMA legacy_alter_table=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


_ACCEPTED_REBUILD_TABLES = (
    "vip_health_check_cases",
    "vip_health_check_reviews",
    "vip_health_check_reports",
    "vip_health_check_deliveries",
    "dietitian_coaching_orders",
)
_ACCEPTED_REBUILD_SETS = tuple(
    frozenset(route)
    for size in range(1, len(_ACCEPTED_REBUILD_TABLES) + 1)
    for route in itertools.combinations(_ACCEPTED_REBUILD_TABLES, size)
)


def _install_mixed_predecessor_graph(conn, rebuild_tables):
    """Start populated/canonical, then downgrade exactly the requested shipped routes."""
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_full_graph_schema(conn)
    ensure_vip_health_check_schema(conn)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    table_specs = {
        "vip_health_check_cases": ("case_id", (
            "CREATE INDEX idx_vip_health_check_cases_status "
            "ON vip_health_check_cases(status,updated_at)",
        )),
        "vip_health_check_reviews": ("review_id", (
            "CREATE UNIQUE INDEX idx_vip_health_check_reviews_case_review "
            "ON vip_health_check_reviews(case_id,review_id)",
        )),
        "vip_health_check_deliveries": ("delivery_id", (
            "CREATE INDEX idx_vip_health_check_deliveries_status "
            "ON vip_health_check_deliveries(status,created_at)",
        )),
        "dietitian_coaching_orders": ("order_id", ()),
    }
    for table_name in rebuild_tables & table_specs.keys():
        primary_key, indexes = table_specs[table_name]
        original_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()[0]
        columns = [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table_name}")')]
        column_list = ",".join(f'"{column}"' for column in columns)
        backup = table_name + "_canonical_fixture"
        conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{backup}"')
        predecessor_sql = original_sql.replace(
            f"{primary_key} TEXT PRIMARY KEY NOT NULL",
            f"{primary_key} TEXT PRIMARY KEY",
        )
        if table_name == "vip_health_check_reviews":
            predecessor_sql = predecessor_sql.replace(
                "            UNIQUE(case_id, review_id),\n", ""
            ).replace(", UNIQUE(case_id, review_id)", "")
        conn.execute(predecessor_sql)
        conn.execute(
            f'INSERT INTO "{table_name}" ({column_list}) '
            f'SELECT {column_list} FROM "{backup}"'
        )
        conn.execute(f'DROP TABLE "{backup}"')
        for index_sql in indexes:
            conn.execute(index_sql)

    if "vip_health_check_reports" in rebuild_tables:
        table_name = "vip_health_check_reports"
        original_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()[0]
        columns = [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table_name}")')]
        column_list = ",".join(f'"{column}"' for column in columns)
        backup = table_name + "_canonical_fixture"
        conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{backup}"')
        predecessor_sql = original_sql.replace(
            "report_id TEXT PRIMARY KEY NOT NULL", "report_id TEXT PRIMARY KEY"
        ).replace("\n          CHECK(report_kind='baseline_3day')", "")
        conn.execute(predecessor_sql)
        conn.execute(
            f'INSERT INTO "{table_name}" ({column_list}) '
            f'SELECT {column_list} FROM "{backup}"'
        )
        conn.execute(f'DROP TABLE "{backup}"')
        conn.executescript(
            """
            CREATE TRIGGER vip_health_check_reports_no_update BEFORE UPDATE
            ON vip_health_check_reports BEGIN
              SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
            CREATE TRIGGER vip_health_check_reports_no_delete BEFORE DELETE
            ON vip_health_check_reports BEGIN
              SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
            """
        )
    conn.commit()
    conn.execute("PRAGMA legacy_alter_table=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("rebuild_tables", _ACCEPTED_REBUILD_SETS)
def test_every_accepted_mixed_predecessor_rebuild_set_releases_cleanly(
    conn, rebuild_tables
):
    from vip_health_check import ensure_vip_health_check_schema

    _install_mixed_predecessor_graph(conn, rebuild_tables)
    tracked_tables = (
        "vip_health_check_cases", "vip_health_check_valid_days",
        "vip_health_check_source_refs", "vip_health_check_reviews",
        "vip_health_check_reports", "vip_health_check_deliveries",
        "vip_health_check_audit_log", "vip_health_check_notifications",
        "dietitian_coaching_orders",
    )
    before = {
        table: tuple(conn.execute(f'SELECT * FROM "{table}"'))
        for table in tracked_tables
    }

    ensure_vip_health_check_schema(conn)

    assert conn.in_transaction is False
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT name FROM temp.sqlite_master WHERE name LIKE 'vip_c566_stage_%'"
    ).fetchall() == []
    for table, rows in before.items():
        assert tuple(conn.execute(f'SELECT * FROM "{table}"')) == rows


@pytest.mark.parametrize("caller_transaction", [False, True])
@pytest.mark.parametrize("previous_deferred", [0, 1])
def test_schema_release_failure_rolls_back_exactly_and_preserves_boundary_state(
    caller_transaction, previous_deferred
):
    from vip_health_check import ensure_vip_health_check_schema

    conn = sqlite3.connect(":memory:", factory=_FailingMigrationConnection)
    conn.row_factory = sqlite3.Row
    try:
        _install_mixed_predecessor_graph(
            conn, frozenset({"vip_health_check_reviews"})
        )
        conn.execute("CREATE TABLE migration_sentinel(value TEXT NOT NULL)")
        conn.commit()
        if caller_transaction:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO migration_sentinel VALUES ('caller-write')")
        before = tuple(conn.iterdump())
        conn.execute(f"PRAGMA defer_foreign_keys={previous_deferred}")
        conn.fail_on_schema_release = True

        with pytest.raises(sqlite3.IntegrityError, match="forced schema release failure"):
            ensure_vip_health_check_schema(conn)

        assert conn.in_transaction is caller_transaction
        assert conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == previous_deferred
        assert tuple(conn.iterdump()) == before
        assert conn.execute(
            "SELECT name FROM temp.sqlite_master WHERE name LIKE 'vip_c566_stage_%'"
        ).fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        if caller_transaction:
            assert conn.execute("SELECT value FROM migration_sentinel").fetchone()[0] == "caller-write"
            conn.rollback()
            assert conn.execute("SELECT COUNT(*) FROM migration_sentinel").fetchone()[0] == 0
    finally:
        conn.close()


def _install_legacy_report_in_c566_full_graph(
    conn, *, with_empty_review_id=False, report_manifest="c566-manifest"
):
    """Combine either trusted legacy report shape with the populated nullable-PK graph."""
    _install_c566_full_graph_schema(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        DROP TABLE vip_health_check_deliveries;
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        """
    )
    review_column = "review_id TEXT NOT NULL DEFAULT ''," if with_empty_review_id else ""
    review_index = (
        "CREATE UNIQUE INDEX idx_vip_health_check_reports_review "
        "ON vip_health_check_reports(review_id) WHERE review_id<>'';"
        if with_empty_review_id else ""
    )
    conn.executescript(
        f"""
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            {review_column}
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version)
        );
        {review_index}
        CREATE TRIGGER vip_health_check_reports_no_update BEFORE UPDATE
        ON vip_health_check_reports BEGIN
          SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
        CREATE TRIGGER vip_health_check_reports_no_delete BEFORE DELETE
        ON vip_health_check_reports BEGIN
          SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
        CREATE TABLE vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','failed','delivered')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        );
        CREATE INDEX idx_vip_health_check_deliveries_status
            ON vip_health_check_deliveries(status,created_at);
        """
    )
    report_columns = (
        "report_id,case_id,review_id,report_kind,report_version,report_json,"
        "source_manifest_hash,published_by,published_at"
        if with_empty_review_id else
        "report_id,case_id,report_kind,report_version,report_json,"
        "source_manifest_hash,published_by,published_at"
    )
    report_values = (
        "'c566-report','c566-case','','baseline_3day',1,'{\"good\":\"preserve exactly\"}',"
        if with_empty_review_id else
        "'c566-report','c566-case','baseline_3day',1,'{\"good\":\"preserve exactly\"}',"
    )
    conn.execute(
        f"INSERT INTO vip_health_check_reports ({report_columns}) "
        f"VALUES ({report_values}?,'dietitian','published')",
        (report_manifest,),
    )
    conn.execute(
        """INSERT INTO vip_health_check_deliveries
           (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
            created_at,delivered_at)
           VALUES ('c566-delivery','c566-report','historical-other-recipient','c566-key',
                   'delivered',3,'historical-error','created','delivered')"""
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


@pytest.mark.parametrize("with_empty_review_id", [False, True])
def test_legacy_report_mapping_is_applied_inside_nullable_pk_full_graph_twice(
    conn, with_empty_review_id
):
    from vip_health_check import ensure_vip_health_check_schema

    _install_legacy_report_in_c566_full_graph(
        conn, with_empty_review_id=with_empty_review_id
    )
    tracked_tables = (
        "vip_health_check_cases", "vip_health_check_valid_days",
        "vip_health_check_source_refs", "vip_health_check_reviews",
        "vip_health_check_deliveries", "vip_health_check_audit_log",
        "vip_health_check_notifications", "dietitian_coaching_orders",
    )
    before = {
        table: tuple(conn.execute(f'SELECT * FROM "{table}"'))
        for table in tracked_tables
    }

    ensure_vip_health_check_schema(conn)
    ensure_vip_health_check_schema(conn)

    assert tuple(conn.execute(
        "SELECT report_id,case_id,review_id,report_json FROM vip_health_check_reports"
    ).fetchone()) == (
        "c566-report", "c566-case", "c566-review", '{"good":"preserve exactly"}'
    )
    for table, rows in before.items():
        assert tuple(conn.execute(f'SELECT * FROM "{table}"')) == rows
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT name FROM temp.sqlite_master WHERE name LIKE 'vip_c566_stage_%'"
    ).fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE vip_health_check_reports SET report_json='changed'")


@pytest.mark.parametrize("with_empty_review_id", [False, True])
def test_legacy_report_nullable_pk_graph_restore_failure_preserves_caller_transaction(
    with_empty_review_id,
):
    from vip_health_check import ensure_vip_health_check_schema

    conn = sqlite3.connect(":memory:", factory=_FailingMigrationConnection)
    try:
        _install_legacy_report_in_c566_full_graph(
            conn, with_empty_review_id=with_empty_review_id
        )
        conn.execute("CREATE TABLE migration_sentinel(value TEXT NOT NULL)")
        conn.commit()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO migration_sentinel VALUES ('must-rollback')")
        before = tuple(conn.iterdump())
        conn.fail_on_full_graph_report_restore = True

        with pytest.raises(sqlite3.IntegrityError, match="forced full graph rebuild failure"):
            ensure_vip_health_check_schema(conn)

        assert tuple(conn.iterdump()) == before
        assert conn.in_transaction is True
        assert conn.execute(
            "SELECT name FROM temp.sqlite_master WHERE name LIKE 'vip_c566_stage_%'"
        ).fetchall() == []
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM migration_sentinel").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("with_empty_review_id", [False, True])
def test_legacy_report_nullable_pk_graph_trust_mismatch_rejects_before_drop(
    conn, with_empty_review_id
):
    from vip_health_check import ensure_vip_health_check_schema

    _install_legacy_report_in_c566_full_graph(
        conn, with_empty_review_id=with_empty_review_id, report_manifest="wrong"
    )
    before = tuple(conn.iterdump())
    traced = []
    conn.set_trace_callback(traced.append)

    with pytest.raises(sqlite3.IntegrityError, match="identity or manifest mismatch"):
        ensure_vip_health_check_schema(conn)

    conn.set_trace_callback(None)
    assert tuple(conn.iterdump()) == before
    assert not any(
        statement.lstrip().upper().startswith(("DROP ", "ALTER "))
        for statement in traced
    )


def test_c566_full_graph_releases_savepoint_and_preserves_every_fk_lane(conn):
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_full_graph_schema(conn)
    before = {
        table: tuple(conn.execute(f'SELECT * FROM "{table}"'))
        for table in (
            "vip_health_check_cases", "vip_health_check_valid_days",
            "vip_health_check_source_refs", "vip_health_check_reviews",
            "vip_health_check_reports", "vip_health_check_deliveries",
            "vip_health_check_audit_log", "vip_health_check_notifications",
            "dietitian_coaching_orders",
        )
    }

    ensure_vip_health_check_schema(conn)
    ensure_vip_health_check_schema(conn)

    assert conn.in_transaction is False
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for table, rows in before.items():
        assert tuple(conn.execute(f'SELECT * FROM "{table}"')) == rows


def test_c566_full_graph_failure_rolls_back_main_and_temp_schema():
    from vip_health_check import ensure_vip_health_check_schema

    conn = sqlite3.connect(":memory:", factory=_FailingMigrationConnection)
    conn.row_factory = sqlite3.Row
    try:
        _install_c566_full_graph_schema(conn)
        before = tuple(conn.iterdump())
        conn.fail_on_full_graph_report_restore = True
        with pytest.raises(sqlite3.IntegrityError, match="forced full graph rebuild failure"):
            ensure_vip_health_check_schema(conn)
        assert tuple(conn.iterdump()) == before
        assert conn.execute(
            "SELECT name FROM temp.sqlite_master WHERE name LIKE 'vip_c566_stage_%'"
        ).fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_c566_reports_schema_migrates_losslessly_and_idempotently_without_recipient_rewrite(conn):
    from vip_health_check import ensure_vip_health_check_schema, get_customer_health_check_state

    _install_c566_reports_schema(conn)
    report_before = tuple(conn.execute("SELECT * FROM vip_health_check_reports").fetchone())
    delivery_before = tuple(conn.execute("SELECT * FROM vip_health_check_deliveries").fetchone())

    ensure_vip_health_check_schema(conn)
    ensure_vip_health_check_schema(conn)

    assert tuple(conn.execute("SELECT * FROM vip_health_check_reports").fetchone()) == report_before
    assert tuple(conn.execute("SELECT * FROM vip_health_check_deliveries").fetchone()) == delivery_before
    report_info = {row[1]: row for row in conn.execute("PRAGMA table_info(vip_health_check_reports)")}
    assert report_info["report_id"][3] == 1
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,review_id,report_kind,report_version,report_json,
                source_manifest_hash,published_by,published_at)
               VALUES ('bad-kind','c566-case','c566-review','other',2,'{}',
                       'c566-manifest','dietitian','now')"""
        )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE vip_health_check_reports SET report_json='{}'")
    # Historical recipient mismatch stays quarantined: neither owner can read the report.
    owner_state = get_customer_health_check_state(conn, user_id="case-owner")
    assert owner_state is not None
    assert owner_state["report"] is None
    assert get_customer_health_check_state(conn, user_id="historical-other-recipient") is None


@pytest.mark.parametrize(
    ("report_id", "report_kind"),
    [(None, "baseline_3day"), ("c566-report", "unsupported-kind")],
)
def test_c566_reports_migration_rejects_invalid_historical_rows_without_mutation(
    conn, report_id, report_kind
):
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_reports_schema(conn, report_id=report_id, report_kind=report_kind)
    before = tuple(conn.iterdump())
    traced = []
    conn.set_trace_callback(traced.append)
    with pytest.raises(sqlite3.IntegrityError):
        ensure_vip_health_check_schema(conn)
    conn.set_trace_callback(None)
    assert tuple(conn.iterdump()) == before
    assert not any(
        statement.lstrip().upper().startswith(("DROP ", "ALTER "))
        for statement in traced
    )


@pytest.mark.parametrize(
    "table_name",
    (
        "vip_health_check_cases",
        "vip_health_check_valid_days",
        "vip_health_check_source_refs",
        "vip_health_check_reviews",
        "vip_health_check_reports",
        "vip_health_check_deliveries",
        "vip_health_check_audit_log",
        "vip_health_check_notifications",
        "dietitian_coaching_orders",
    ),
)
def test_c566_full_graph_rejects_unknown_trigger_before_destructive_ddl(conn, table_name):
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_full_graph_schema(conn)
    conn.execute(
        f'''CREATE TRIGGER unknown_{table_name}_guard BEFORE DELETE ON "{table_name}"
            BEGIN SELECT RAISE(ABORT, 'must survive'); END'''
    )
    conn.commit()
    before = tuple(conn.iterdump())
    traced = []
    conn.set_trace_callback(traced.append)

    with pytest.raises(sqlite3.IntegrityError, match="trigger"):
        ensure_vip_health_check_schema(conn)

    conn.set_trace_callback(None)
    assert tuple(conn.iterdump()) == before
    assert not any(
        statement.lstrip().upper().startswith(("DROP ", "ALTER "))
        for statement in traced
    )


def test_c566_full_graph_rejects_missing_required_report_trigger_before_destructive_ddl(conn):
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_full_graph_schema(conn)
    conn.execute("DROP TRIGGER vip_health_check_reports_no_delete")
    conn.commit()
    before = tuple(conn.iterdump())
    traced = []
    conn.set_trace_callback(traced.append)

    with pytest.raises(sqlite3.IntegrityError, match="missing required immutable trigger"):
        ensure_vip_health_check_schema(conn)

    conn.set_trace_callback(None)
    assert tuple(conn.iterdump()) == before
    assert not any(
        statement.lstrip().upper().startswith(("DROP ", "ALTER "))
        for statement in traced
    )


@pytest.mark.parametrize(
    "invalid_column,invalid_value", (("report_id", None), ("report_kind", "other"))
)
def test_c566_full_graph_rejects_invalid_report_before_destructive_ddl(
    conn, invalid_column, invalid_value
):
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_full_graph_schema(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TRIGGER vip_health_check_reports_no_update")
    conn.execute("DROP TRIGGER vip_health_check_reports_no_delete")
    conn.execute(
        f'UPDATE vip_health_check_reports SET "{invalid_column}"=?', (invalid_value,)
    )
    conn.executescript(
        """CREATE TRIGGER vip_health_check_reports_no_update BEFORE UPDATE
           ON vip_health_check_reports BEGIN
             SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
           CREATE TRIGGER vip_health_check_reports_no_delete BEFORE DELETE
           ON vip_health_check_reports BEGIN
             SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;"""
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    before = tuple(conn.iterdump())
    traced = []
    conn.set_trace_callback(traced.append)

    with pytest.raises(sqlite3.IntegrityError, match="invalid canonical data"):
        ensure_vip_health_check_schema(conn)

    conn.set_trace_callback(None)
    assert tuple(conn.iterdump()) == before
    assert not any(
        statement.lstrip().upper().startswith(("DROP ", "ALTER "))
        for statement in traced
    )


def test_c566_reports_near_miss_schema_is_rejected_before_drop(conn):
    from vip_health_check import ensure_vip_health_check_schema

    _install_c566_reports_schema(conn)
    conn.execute("CREATE UNIQUE INDEX c566_unknown_unique ON vip_health_check_reports(published_by)")
    conn.commit()
    traced = []
    conn.set_trace_callback(traced.append)
    with pytest.raises(sqlite3.IntegrityError, match="unsupported"):
        ensure_vip_health_check_schema(conn)
    conn.set_trace_callback(None)
    assert not any(statement.lstrip().upper().startswith("DROP") for statement in traced)
    assert conn.execute("SELECT report_json FROM vip_health_check_reports").fetchone()[0] == (
        '{"good":"preserve exactly"}'
    )


def test_c566_reports_rebuild_failure_rolls_back_schema_rows_and_recipient():
    from vip_health_check import ensure_vip_health_check_schema

    conn = sqlite3.connect(":memory:", factory=_FailingMigrationConnection)
    conn.row_factory = sqlite3.Row
    try:
        _install_c566_reports_schema(conn)
        before = tuple(conn.iterdump())
        conn.fail_on_report_review_index = True
        with pytest.raises(sqlite3.IntegrityError, match="forced report rebuild failure"):
            ensure_vip_health_check_schema(conn)
        assert tuple(conn.iterdump()) == before
        assert conn.execute(
            "SELECT user_id FROM vip_health_check_deliveries"
        ).fetchone()[0] == "historical-other-recipient"
    finally:
        conn.close()


def _install_legacy_report_with_delivery(conn, *, report_version=1):
    conn.executescript(
        """
        DROP TABLE vip_health_check_deliveries;
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version)
        );
        CREATE TRIGGER vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
        CREATE TRIGGER vip_health_check_reports_no_delete
        BEFORE DELETE ON vip_health_check_reports
        BEGIN SELECT RAISE(ABORT, 'published health-check reports are immutable'); END;
        CREATE TABLE vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        );
        """
    )
    conn.execute(
        """INSERT INTO vip_health_check_reports
           (report_id,case_id,report_kind,report_version,report_json,
            source_manifest_hash,published_by,published_at)
           VALUES ('legacy-report','legacy-case','baseline_3day',?,'{}','manifest',
                   'dietitian','2026-09-03T10:00:00+08:00')""",
        (report_version,),
    )
    conn.execute(
        """INSERT INTO vip_health_check_deliveries
           (delivery_id,report_id,user_id,delivery_key,created_at)
           VALUES ('legacy-delivery','legacy-report','U-legacy','legacy-key','now')"""
    )
    conn.commit()


def _install_trusted_legacy_report_route(conn, *, with_empty_review_id=False):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,status,source_manifest_hash,created_at,updated_at)
           VALUES ('legacy-case','U-legacy','first_vip_baseline_check','act','event',
                   'start','end','approved_pending_delivery','manifest','now','now')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('legacy-review','legacy-case',1,'approved','manifest','dietitian',
                   'now','now','now')"""
    )
    conn.commit()
    _install_legacy_report_with_delivery(conn)
    if with_empty_review_id:
        conn.executescript(
            """
            DROP TABLE vip_health_check_deliveries;
            DROP TRIGGER vip_health_check_reports_no_update;
            DROP TRIGGER vip_health_check_reports_no_delete;
            DROP TABLE vip_health_check_reports;
            CREATE TABLE vip_health_check_reports (
                report_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                review_id TEXT NOT NULL DEFAULT '',
                report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
                report_version INTEGER NOT NULL, report_json TEXT NOT NULL,
                source_manifest_hash TEXT NOT NULL, published_by TEXT NOT NULL,
                published_at TEXT NOT NULL, UNIQUE(case_id,report_kind,report_version)
            );
            CREATE UNIQUE INDEX idx_vip_health_check_reports_review
                ON vip_health_check_reports(review_id) WHERE review_id<>'';
            CREATE TRIGGER vip_health_check_reports_no_update BEFORE UPDATE
                ON vip_health_check_reports BEGIN SELECT RAISE(ABORT,
                'published health-check reports are immutable'); END;
            CREATE TRIGGER vip_health_check_reports_no_delete BEFORE DELETE
                ON vip_health_check_reports BEGIN SELECT RAISE(ABORT,
                'published health-check reports are immutable'); END;
            CREATE TABLE vip_health_check_deliveries (
                delivery_id TEXT PRIMARY KEY, report_id TEXT NOT NULL,
                user_id TEXT NOT NULL, delivery_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
            );
            INSERT INTO vip_health_check_reports
              VALUES ('legacy-report','legacy-case','','baseline_3day',1,'{}','manifest',
                      'dietitian','2026-09-03T10:00:00+08:00');
            INSERT INTO vip_health_check_deliveries
              (delivery_id,report_id,user_id,delivery_key,created_at)
              VALUES ('legacy-delivery','legacy-report','U-legacy','legacy-key','now');
            """
        )
        conn.commit()


@pytest.mark.parametrize("with_empty_review_id", [False, True])
@pytest.mark.parametrize("tamper", ["case", "review", "report"])
def test_legacy_report_hash_mismatch_rejects_before_drop_and_preserves_rows(
    conn, with_empty_review_id, tamper
):
    from vip_health_check import ensure_vip_health_check_schema

    _install_trusted_legacy_report_route(conn, with_empty_review_id=with_empty_review_id)
    conn.execute("DROP TRIGGER vip_health_check_reports_no_update")
    if tamper == "case":
        conn.execute("UPDATE vip_health_check_cases SET source_manifest_hash='wrong'")
    elif tamper == "review":
        conn.execute("UPDATE vip_health_check_reviews SET source_manifest_hash='wrong'")
    else:
        conn.execute("UPDATE vip_health_check_reports SET source_manifest_hash='wrong'")
    conn.execute("""CREATE TRIGGER vip_health_check_reports_no_update BEFORE UPDATE
                 ON vip_health_check_reports BEGIN SELECT RAISE(ABORT,
                 'published health-check reports are immutable'); END""")
    conn.commit()
    before = tuple(conn.iterdump())
    traced = []
    conn.set_trace_callback(traced.append)
    with pytest.raises(sqlite3.IntegrityError, match="identity or manifest mismatch"):
        ensure_vip_health_check_schema(conn)
    conn.set_trace_callback(None)
    assert tuple(conn.iterdump()) == before
    assert not any(statement.lstrip().upper().startswith("DROP ") for statement in traced)


def test_nine_column_empty_review_id_backfills_from_fully_trusted_mapping_twice(conn):
    from vip_health_check import ensure_vip_health_check_schema

    _install_trusted_legacy_report_route(conn, with_empty_review_id=True)
    ensure_vip_health_check_schema(conn)
    ensure_vip_health_check_schema(conn)
    assert conn.execute("SELECT review_id FROM vip_health_check_reports").fetchone()[0] == "legacy-review"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_report_rebuild_rejects_unknown_nonempty_column_without_data_loss(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,created_at,updated_at)
           VALUES ('legacy-case','U-legacy','first_vip_baseline_check','act','event',
                   'start','end','now','now')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('legacy-review','legacy-case',1,'approved','manifest','dietitian',
                   'now','now','now')"""
    )
    conn.commit()
    _install_legacy_report_with_delivery(conn)
    conn.execute(
        "ALTER TABLE vip_health_check_reports "
        "ADD COLUMN poison TEXT NOT NULL DEFAULT 'must-survive'"
    )
    before = tuple(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="unsupported legacy"):
        ensure_vip_health_check_schema(conn)

    assert tuple(conn.iterdump()) == before
    assert conn.execute(
        "SELECT poison FROM vip_health_check_reports WHERE report_id='legacy-report'"
    ).fetchone()[0] == "must-survive"


def test_legacy_report_rebuild_adds_both_fks_and_preserves_report_delivery_idempotently(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,status,source_manifest_hash,created_at,updated_at)
           VALUES ('legacy-case','U-legacy','first_vip_baseline_check','legacy-activation',
                   'legacy-event','start','end','approved_pending_delivery','manifest','now','now')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('legacy-review','legacy-case',1,'approved','manifest','dietitian',
                   'now','now','now')"""
    )
    conn.commit()
    _install_legacy_report_with_delivery(conn)

    ensure_vip_health_check_schema(conn)
    ensure_vip_health_check_schema(conn)

    fk_targets = {
        (row[3], row[2], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(vip_health_check_reports)")
    }
    assert fk_targets == {
        ("case_id", "vip_health_check_cases", "case_id"),
        ("review_id", "vip_health_check_reviews", "review_id"),
        ("case_id", "vip_health_check_reviews", "case_id"),
    }
    assert tuple(map(tuple, conn.execute("SELECT report_id,case_id,review_id FROM vip_health_check_reports"))) == (
        ("legacy-report", "legacy-case", "legacy-review"),
    )
    assert tuple(map(tuple, conn.execute("SELECT delivery_id,report_id FROM vip_health_check_deliveries"))) == (
        ("legacy-delivery", "legacy-report"),
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for operation in ("UPDATE vip_health_check_reports SET report_json='x'", "DELETE FROM vip_health_check_reports"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(operation)


def test_legacy_report_rebuild_rejects_unbackfillable_report_and_restores_old_schema(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,created_at,updated_at)
           VALUES ('legacy-case','U-legacy','first_vip_baseline_check','legacy-activation',
                   'legacy-event','start','end','now','now')"""
    )
    conn.commit()
    _install_legacy_report_with_delivery(conn, report_version=9)
    before_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vip_health_check_reports'"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="unique approved review"):
        ensure_vip_health_check_schema(conn)

    assert conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vip_health_check_reports'"
    ).fetchone()[0] == before_sql
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_deliveries").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE vip_health_check_reports SET report_json='x'")


def test_schema_rejects_historical_orphan_written_while_fk_checks_were_off(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """INSERT INTO vip_health_check_valid_days
           (case_id,local_date,rule_version,qualifying_meal_count,
            completeness_status,evaluated_at)
           VALUES ('missing-case','2026-09-03','v1',2,'qualified','now')"""
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(sqlite3.IntegrityError, match="foreign key violations"):
        ensure_vip_health_check_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_valid_days").fetchone()[0] == 1


def test_report_review_must_belong_to_the_same_case(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.executescript(
        """
        INSERT INTO vip_health_check_cases
          (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
           window_started_at,window_ends_at,created_at,updated_at)
        VALUES
          ('case-a','U-a','first_vip_baseline_check','act-a','event-a','start','end','now','now'),
          ('case-b','U-b','first_vip_baseline_check','act-b','event-b','start','end','now','now');
        INSERT INTO vip_health_check_reviews
          (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
           approved_at,created_at,updated_at)
        VALUES ('review-b','case-b',1,'approved','manifest','dietitian','now','now','now');
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,review_id,report_version,report_json,
                source_manifest_hash,published_by,published_at)
               VALUES ('cross-case','case-a','review-b',1,'{}','manifest','dietitian','now')"""
        )


def test_legacy_nonempty_review_link_is_preserved_or_rejected_never_rewritten(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.executescript(
        """
        INSERT INTO vip_health_check_cases
          (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
           window_started_at,window_ends_at,created_at,updated_at)
        VALUES ('legacy-case','U-legacy','first_vip_baseline_check','act','event',
                'start','end','now','now');
        INSERT INTO vip_health_check_reviews
          (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
           approved_at,created_at,updated_at)
        VALUES
          ('review-v1','legacy-case',1,'approved','manifest','dietitian','now','now','now'),
          ('review-v2','legacy-case',2,'approved','manifest','dietitian','now','now','now');
        DROP TABLE vip_health_check_deliveries;
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
            review_id TEXT NOT NULL DEFAULT '', report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL, report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL, published_by TEXT NOT NULL,
            published_at TEXT NOT NULL, UNIQUE(case_id,report_kind,report_version)
        );
        INSERT INTO vip_health_check_reports
          (report_id,case_id,review_id,report_version,report_json,
           source_manifest_hash,published_by,published_at)
        VALUES ('legacy-report','legacy-case','review-v2',1,'{}','manifest','dietitian','now');
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="unique approved review"):
        ensure_vip_health_check_schema(conn)

    assert conn.execute(
        "SELECT review_id FROM vip_health_check_reports WHERE report_id='legacy-report'"
    ).fetchone()[0] == "review-v2"


def _prepare_legacy_report_for_schema_object_test(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
            window_started_at,window_ends_at,created_at,updated_at)
           VALUES ('legacy-case','U-legacy','first_vip_baseline_check','act','event',
                   'start','end','now','now')"""
    )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('legacy-review','legacy-case',1,'approved','manifest','dietitian',
                   'now','now','now')"""
    )
    conn.commit()
    _install_legacy_report_with_delivery(conn)


def test_legacy_rebuild_fails_closed_on_unknown_schema_object(conn):
    from vip_health_check import ensure_vip_health_check_schema

    _prepare_legacy_report_for_schema_object_test(conn)
    conn.execute(
        "CREATE INDEX custom_legacy_publisher ON vip_health_check_reports(published_by)"
    )

    with pytest.raises(sqlite3.IntegrityError, match="unsupported custom schema objects"):
        ensure_vip_health_check_schema(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='custom_legacy_publisher'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("object_name", "create_sql"),
    [
        (
            "idx_vip_health_check_reports_review",
            "CREATE INDEX idx_vip_health_check_reports_review "
            "ON vip_health_check_reports(published_by)",
        ),
        (
            "idx_vip_health_check_deliveries_status",
            "CREATE INDEX idx_vip_health_check_deliveries_status "
            "ON vip_health_check_deliveries(user_id)",
        ),
    ],
)
def test_legacy_rebuild_rejects_allowlisted_index_name_with_wrong_definition(
    conn, object_name, create_sql
):
    from vip_health_check import ensure_vip_health_check_schema

    _prepare_legacy_report_for_schema_object_test(conn)
    conn.execute(create_sql)

    with pytest.raises(sqlite3.IntegrityError, match="unsupported custom schema objects"):
        ensure_vip_health_check_schema(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name=? AND sql=?",
        (object_name, create_sql),
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "custom_message",
    [
        "different custom policy",
        "published  health-check reports are immutable",
        "Published health-check reports are immutable",
        "published ifnotexists health-check reports are immutable",
    ],
)
def test_legacy_rebuild_rejects_allowlisted_trigger_name_with_wrong_definition(
    conn, custom_message
):
    from vip_health_check import ensure_vip_health_check_schema

    _prepare_legacy_report_for_schema_object_test(conn)
    conn.execute("DROP TRIGGER vip_health_check_reports_no_update")
    malicious_definition = f"""CREATE TRIGGER vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN SELECT RAISE(ABORT, '{custom_message}'); END"""
    conn.execute(malicious_definition)

    with pytest.raises(sqlite3.IntegrityError, match="unsupported custom schema objects"):
        ensure_vip_health_check_schema(conn)

    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='vip_health_check_reports_no_update'"
    ).fetchone()[0]
    assert custom_message in stored


def test_legacy_rebuild_rejects_ifnotexists_identifier_outside_create_header(conn):
    from vip_health_check import ensure_vip_health_check_schema

    _prepare_legacy_report_for_schema_object_test(conn)
    conn.execute("DROP TRIGGER vip_health_check_reports_no_update")
    alias_definition = """CREATE TRIGGER vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN
            SELECT RAISE(ABORT, 'published health-check reports are immutable') ifnotexists;
        END"""
    conn.execute(alias_definition)

    with pytest.raises(sqlite3.IntegrityError, match="unsupported custom schema objects"):
        ensure_vip_health_check_schema(conn)

    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='vip_health_check_reports_no_update'"
    ).fetchone()[0]
    assert "ifnotexists" in stored


def test_first_vip_entitlement_is_idempotent_and_renewal_keeps_original_window(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    ensure_vip_health_check_schema(conn)
    started_at = datetime(2026, 9, 3, 9, 30, tzinfo=timezone(timedelta(hours=8)))

    first = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-1",
        activation_event_key="vip-redemption:event-1",
        activated_at=started_at,
    )
    renewal = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-2",
        activation_event_key="vip-redemption:event-2",
        activated_at=started_at + timedelta(days=30),
    )

    rows = conn.execute("SELECT * FROM vip_health_check_cases").fetchall()
    assert first["created"] is True
    assert renewal["created"] is False
    assert renewal["case_id"] == first["case_id"]
    assert len(rows) == 1
    assert rows[0]["benefit_key"] == "first_vip_baseline_check"
    assert rows[0]["first_vip_activation_id"] == "activation-1"
    assert rows[0]["activation_event_key"] == "vip-redemption:event-1"
    assert rows[0]["window_started_at"] == started_at.isoformat(timespec="seconds")
    assert rows[0]["window_ends_at"] == (started_at + timedelta(days=7)).isoformat(timespec="seconds")


def _prepare_server_db(server, monkeypatch, tmp_path):
    db_path = tmp_path / "server-health-check.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    return db_path


def test_server_init_creates_health_check_schema(monkeypatch, tmp_path):
    import server

    db_path = _prepare_server_db(server, monkeypatch, tmp_path)
    with sqlite3.connect(db_path) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vip_health_check_cases'"
        ).fetchone()
    assert exists == (1,)


def test_server_redeem_records_first_activation_while_routes_are_dark_and_renewal_is_idempotent(
    monkeypatch, tmp_path
):
    import server

    db_path = _prepare_server_db(server, monkeypatch, tmp_path)
    first_day = datetime(2026, 9, 3, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    current_time = [first_day]
    monkeypatch.setattr(server, "tw_now", lambda: current_time[0])
    monkeypatch.setattr(server, "tw_today", lambda: current_time[0].date())

    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO vips(code,meals,duration_days,chat_limit,is_used) VALUES (?,?,?,?,0)",
            [("#VIP-FIRST", 10, 31, 20), ("#VIP-RENEW", 10, 31, 20), ("#VIP-OFF", 10, 31, 20)],
        )

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    assert server.redeem_code("U-OFF", "#VIP-OFF")[0] is not None
    with sqlite3.connect(db_path) as db:
        dark_case = db.execute(
            "SELECT window_started_at FROM vip_health_check_cases WHERE user_id='U-OFF'"
        ).fetchone()
    assert dark_case == (first_day.isoformat(timespec="seconds"),)

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    assert server.redeem_code("U1", "#VIP-FIRST")[0] is not None
    current_time[0] = first_day + timedelta(days=30)
    assert server.redeem_code("U1", "#VIP-RENEW")[0] is not None

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM vip_health_check_cases WHERE user_id='U1'"
        ).fetchall()
        event_types = [
            row[0]
            for row in db.execute(
                """SELECT activation_type
                   FROM vip_health_check_activation_events
                   WHERE user_id='U1' ORDER BY occurred_at, rowid"""
            )
        ]
        dark_event_type = db.execute(
            """SELECT activation_type FROM vip_health_check_activation_events
               WHERE user_id='U-OFF'"""
        ).fetchone()[0]
    assert len(rows) == 1
    assert event_types == ["lifetime_first", "renewal"]
    assert dark_event_type == "lifetime_first"
    assert rows[0]["window_started_at"] == first_day.isoformat(timespec="seconds")
    assert rows[0]["window_ends_at"] == (first_day + timedelta(days=7)).isoformat(timespec="seconds")
    assert rows[0]["first_vip_activation_id"].startswith("vip_redemption_event:")
    assert rows[0]["activation_event_key"] == rows[0]["first_vip_activation_id"]
    assert "#VIP-FIRST" not in rows[0]["first_vip_activation_id"]
    assert "#VIP-FIRST" not in rows[0]["activation_event_key"]


@pytest.mark.parametrize(
    "prior_status,prior_expiry",
    [
        ("vip", "2099-01-01"),
        ("vip", "2026-01-01"),
        ("free", ""),
    ],
)
def test_preexisting_usage_redemption_is_audited_without_granting_first_case(
    monkeypatch, tmp_path, prior_status, prior_expiry
):
    import server

    db_path = _prepare_server_db(server, monkeypatch, tmp_path)
    uid = f"U-HIST-{prior_status}-{prior_expiry or 'none'}"
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO vips(code,meals,duration_days,chat_limit,is_used) VALUES (?,?,?,?,0)",
            [("#VIP-HIST-ONE", 10, 31, 20), ("#VIP-HIST-TWO", 10, 31, 20)],
        )
        db.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES (?,20,3,'2026-09-01',?,?,20)""",
            (uid, prior_status, prior_expiry),
        )
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)

    assert server.redeem_code(uid, "#VIP-HIST-ONE")[0] is not None
    assert server.redeem_code(uid, "#VIP-HIST-TWO")[0] is not None

    with sqlite3.connect(db_path) as db:
        events = db.execute(
            """SELECT activation_type,prior_usage_status,prior_expiry_date
               FROM vip_health_check_activation_events
               WHERE user_id=? ORDER BY rowid""",
            (uid,),
        ).fetchall()
        case_count = db.execute(
            "SELECT COUNT(*) FROM vip_health_check_cases WHERE user_id=?", (uid,)
        ).fetchone()[0]
    assert events[0] == ("historical_existing", prior_status, prior_expiry)
    assert events[1][0] == "renewal"
    assert case_count == 0


def test_schema_rejects_existing_deliveries_table_without_report_foreign_key(conn):
    from vip_health_check import ensure_vip_health_check_schema

    conn.execute(
        """CREATE TABLE vip_health_check_deliveries (
               delivery_id TEXT PRIMARY KEY,
               report_id TEXT NOT NULL,
               user_id TEXT NOT NULL,
               delivery_key TEXT NOT NULL UNIQUE,
               status TEXT NOT NULL DEFAULT 'pending',
               attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               delivered_at TEXT NOT NULL DEFAULT ''
           )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_deliveries"):
        ensure_vip_health_check_schema(conn)


def test_canonical_schema_rejects_null_text_primary_keys(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(
            """INSERT INTO vip_health_check_cases
               (case_id,user_id,benefit_key,first_vip_activation_id,
                activation_event_key,window_started_at,window_ends_at,
                status,valid_day_count,created_at,updated_at)
               VALUES (NULL,'null-case-user','first_vip_baseline_check','activation-null',
                       'event-null','now','later','collecting',0,'now','now')"""
        )
    conn.execute(
        """INSERT INTO vip_health_check_cases
           (case_id,user_id,benefit_key,first_vip_activation_id,
            activation_event_key,window_started_at,window_ends_at,
            status,valid_day_count,created_at,updated_at)
           VALUES ('case-valid','valid-user','first_vip_baseline_check','activation-valid',
                   'event-valid','now','later','collecting',0,'now','now')"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(
            """INSERT INTO vip_health_check_reviews
               (review_id,case_id,review_version,status,source_manifest_hash,
                created_at,updated_at)
               VALUES (NULL,'case-valid',1,'approved','hash','now','now')"""
        )
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,
            created_at,updated_at)
           VALUES ('review-valid','case-valid',1,'approved','hash','now','now')"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,review_id,report_kind,report_version,
                report_json,source_manifest_hash,published_by,published_at)
               VALUES (NULL,'case-valid','review-valid','baseline_3day',1,
                       '{}','hash','dietitian','now')"""
        )
    conn.execute(
        """INSERT INTO vip_health_check_reports
           (report_id,case_id,review_id,report_kind,report_version,
            report_json,source_manifest_hash,published_by,published_at)
           VALUES ('report-valid','case-valid','review-valid','baseline_3day',1,
                   '{}','hash','dietitian','now')"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(
            """INSERT INTO vip_health_check_deliveries
               (delivery_id,report_id,user_id,delivery_key,status,created_at)
               VALUES (NULL,'report-valid','valid-user','delivery-null-id',
                       'pending','now')"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(
            """INSERT INTO dietitian_coaching_orders
               (order_id,user_id,case_id,product_type,operation_key,status,
                requested_at,updated_at)
               VALUES (NULL,'valid-user','case-valid','dietitian_coaching_4w',
                       'coaching-null-id','payment_pending','now','now')"""
        )


@pytest.mark.parametrize(
    "delivery_key_definition, extra_column, foreign_key_suffix",
    [
        ("TEXT NOT NULL UNIQUE", ", poison TEXT NOT NULL", ""),
        ("TEXT NOT NULL COLLATE NOCASE UNIQUE", "", ""),
        ("TEXT NOT NULL UNIQUE ON CONFLICT IGNORE", "", ""),
        ("TEXT NOT NULL UNIQUE", "", " MATCH FULL"),
    ],
)
def test_schema_rejects_extra_column_unique_collation_and_fk_match(
    conn, delivery_key_definition, extra_column, foreign_key_suffix
):
    from vip_health_check import ensure_vip_health_check_schema

    conn.execute(
        f"""CREATE TABLE vip_health_check_deliveries (
                delivery_id TEXT PRIMARY KEY NOT NULL,
                report_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                delivery_key {delivery_key_definition},
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','failed','delivered')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT ''
                {extra_column},
                FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
                    {foreign_key_suffix}
            )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_deliveries"):
        ensure_vip_health_check_schema(conn)


def test_schema_rejects_decoy_check_that_allows_other_invalid_statuses(conn):
    from vip_health_check import ensure_vip_health_check_schema

    conn.execute(
        """CREATE TABLE vip_health_check_deliveries (
               delivery_id TEXT PRIMARY KEY NOT NULL,
               report_id TEXT NOT NULL,
               user_id TEXT NOT NULL,
               delivery_key TEXT NOT NULL UNIQUE,
               status TEXT NOT NULL DEFAULT 'pending' CHECK(status <> 'invalid'),
               attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               delivered_at TEXT NOT NULL DEFAULT '',
               FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
           )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_deliveries"):
        ensure_vip_health_check_schema(conn)


def test_schema_rejects_noncanonical_foreign_key_actions(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute("DROP TABLE vip_health_check_valid_days")
    conn.execute(
        """CREATE TABLE vip_health_check_valid_days (
               case_id TEXT NOT NULL,
               local_date TEXT NOT NULL,
               rule_version TEXT NOT NULL,
               qualifying_meal_count INTEGER NOT NULL DEFAULT 0,
               completeness_status TEXT NOT NULL,
               evaluated_at TEXT NOT NULL,
               PRIMARY KEY(case_id, local_date),
               FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
                   ON DELETE CASCADE
           )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_valid_days"):
        ensure_vip_health_check_schema(conn)


def test_schema_rejects_deliveries_without_unique_key_and_status_check(conn):
    from vip_health_check import ensure_vip_health_check_schema

    conn.execute(
        """CREATE TABLE vip_health_check_deliveries (
               delivery_id TEXT PRIMARY KEY,
               report_id TEXT NOT NULL,
               user_id TEXT NOT NULL,
               delivery_key TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'pending',
               attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               delivered_at TEXT NOT NULL DEFAULT '',
               FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
           )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_deliveries"):
        ensure_vip_health_check_schema(conn)


def test_schema_rejects_nullable_delivery_key_and_status(conn):
    from vip_health_check import ensure_vip_health_check_schema

    conn.execute(
        """CREATE TABLE vip_health_check_deliveries (
               delivery_id TEXT PRIMARY KEY,
               report_id TEXT NOT NULL,
               user_id TEXT NOT NULL,
               delivery_key TEXT UNIQUE,
               status TEXT DEFAULT 'pending'
                   CHECK(status IN ('pending','failed','delivered')),
               attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               delivered_at TEXT NOT NULL DEFAULT '',
               FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
           )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_deliveries"):
        ensure_vip_health_check_schema(conn)


def test_schema_rejects_noop_immutable_report_trigger(conn):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute("DROP TRIGGER vip_health_check_reports_no_update")
    conn.execute(
        """CREATE TRIGGER vip_health_check_reports_no_update
           BEFORE UPDATE ON vip_health_check_reports WHEN 0
           BEGIN
               SELECT RAISE(ABORT, 'published health-check reports are immutable');
           END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="trigger"):
        ensure_vip_health_check_schema(conn)
    trigger_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='vip_health_check_reports_no_update'"
    ).fetchone()[0]
    assert "WHEN 0" in trigger_sql


def test_schema_rejects_comment_spoof_partial_unique_and_wrong_parent_index(conn):
    from vip_health_check import ensure_vip_health_check_schema

    conn.executescript(
        """
        CREATE TABLE hidden_delivery_index_parent (
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_vip_health_check_deliveries_status
            ON hidden_delivery_index_parent(status, created_at);
        CREATE TABLE vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                /* CHECK(status IN ('pending','failed','delivered')) */,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        );
        CREATE UNIQUE INDEX partial_delivery_key_spoof
            ON vip_health_check_deliveries(delivery_key) WHERE 0;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_deliveries"):
        ensure_vip_health_check_schema(conn)


@pytest.mark.parametrize(
    "replacement_sql",
    [
        """CREATE INDEX idx_vip_health_check_deliveries_status
           ON vip_health_check_deliveries(status DESC, created_at)""",
        """CREATE INDEX idx_vip_health_check_deliveries_status
           ON vip_health_check_deliveries(status COLLATE NOCASE, created_at)""",
    ],
)
def test_schema_rejects_noncanonical_index_direction_or_collation(
    conn, replacement_sql
):
    from vip_health_check import ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute("DROP INDEX idx_vip_health_check_deliveries_status")
    conn.execute(replacement_sql)

    with pytest.raises(sqlite3.IntegrityError, match="index"):
        ensure_vip_health_check_schema(conn)


def test_schema_rejects_independent_foreign_keys_instead_of_report_composite_fk(conn):
    from vip_health_check import ensure_vip_health_check_schema

    conn.execute(
        """CREATE TABLE vip_health_check_reports (
               report_id TEXT PRIMARY KEY,
               case_id TEXT NOT NULL,
               review_id TEXT NOT NULL UNIQUE,
               report_kind TEXT NOT NULL DEFAULT 'baseline_3day'
                   CHECK(report_kind='baseline_3day'),
               report_version INTEGER NOT NULL,
               report_json TEXT NOT NULL,
               source_manifest_hash TEXT NOT NULL,
               published_by TEXT NOT NULL,
               published_at TEXT NOT NULL,
               UNIQUE(case_id, report_kind, report_version),
               FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
               FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id),
               FOREIGN KEY(case_id) REFERENCES vip_health_check_reviews(case_id)
           )"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="vip_health_check_reports"):
        ensure_vip_health_check_schema(conn)


def _create_minimal_food_ledger(conn):
    conn.executescript(
        """
        CREATE TABLE food_logs (
            log_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            consumed_at TEXT NOT NULL,
            meal_slot TEXT DEFAULT '',
            nutrition_snapshot_json TEXT NOT NULL,
            confirmation_status TEXT NOT NULL DEFAULT 'confirmed',
            version INTEGER NOT NULL DEFAULT 1,
            deleted_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE planned_meal_checks (
            user_id TEXT,
            meal_date TEXT,
            meal_slot TEXT,
            meal_name TEXT,
            cal INTEGER,
            pro INTEGER,
            checked_at TEXT,
            PRIMARY KEY(user_id,meal_date,meal_slot)
        );
        """
    )


def _insert_log(conn, log_id, consumed_at, meal_slot, *, status="confirmed", deleted_at=""):
    conn.execute(
        """INSERT INTO food_logs
           (log_id,user_id,consumed_at,meal_slot,nutrition_snapshot_json,
            confirmation_status,version,deleted_at)
           VALUES (?,?,?,?,?,?,1,?)""",
        (log_id, "U1", consumed_at, meal_slot, '{"calories_kcal":500}', status, deleted_at),
    )


def test_manifest_accepts_only_integrity_valid_user_confirmed_ai_estimates(conn):
    from nutrition_system import ensure_nutrition_schema, insert_user_confirmed_meal_photo_log
    from meal_photo_system import save_meal_photo_draft
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_case_source_manifest,
    )

    ensure_nutrition_schema(conn)
    conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    conn.execute("""CREATE TABLE health_profile (
        user_id TEXT PRIMARY KEY,name TEXT,tdee REAL,protein REAL,goal TEXT,
        restrictions TEXT,active_days TEXT)""")
    ensure_vip_health_check_schema(conn)
    case = create_first_vip_health_check_case(
        conn, user_id="U1", first_vip_activation_id="activation-ai",
        activation_event_key="event-ai",
        activated_at=datetime(2026, 9, 3, 7, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    estimate = {
        "calories_kcal": None, "protein_g": None, "fat_g": None,
        "carbohydrate_g": None,
        "protein_total_exchange": {"min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"},
        "starch_exchange": {"min": 0.0, "max": 0.0, "basis": "user_confirmed_none"},
        "vegetable_exchange": {"min": 1.0, "max": 2.0, "basis": "bowl_range_v1"},
        "cooking_oil_confirmation": "unknown", "sauce_confirmation": "unknown",
        "formal_status": "pending_review_not_counted", "rule_version": "hand-portion-range-v1",
    }
    log_ids = []
    for suffix, slot, hour in (("a", "早餐", 8), ("b", "午餐", 12)):
        source_message_id = f"message-{suffix}"
        confirmation_event_id = f"confirm-{suffix}"
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id=source_message_id,
            payload={
                "status": "success", "image_type": "food_photo",
                "visible_items": [{"name": "雞肉", "category": "protein", "confidence": 0.9}],
                "uncertain_items": [], "starch_visibility": "not_visible",
                "oil_sauce_status": "unknown",
            },
        )
        result = insert_user_confirmed_meal_photo_log(
            conn, token=token, user_id="U1", source_message_id=source_message_id,
            confirmation_event_id=confirmation_event_id,
            consumed_at=f"2026-09-03T{hour:02d}:00:00+08:00", meal_slot=slot,
            source_image_ref=f"nutrition-image:{suffix}.jpg",
            observed_payload={"visible_items": [{"name": "雞肉"}]},
            answers={}, estimate=estimate,
        )
        log_ids.append(result["log_id"])
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET status='user_confirmed',version=2,confirmed_log_id=?,confirmed_by='U1',
                   original_confirmation_event_id=?
               WHERE token=? AND user_id='U1'""",
            (result["log_id"], confirmation_event_id, token),
        )
        request = {
            "user_id": "U1", "token": token, "expected_version": 1,
            "action": "confirm_estimate", "field": "", "value": "",
        }
        request_hash = hashlib.sha256(json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        event_result = {"kind": "recorded", "version": 2, "log_id": result["log_id"]}
        conn.execute(
            """INSERT INTO meal_photo_events
               (event_id,user_id,token,action,request_payload_hash,result_json,created_at)
               VALUES(?, 'U1', ?, 'confirm_estimate', ?, ?, ?)""",
            (confirmation_event_id, token, request_hash,
             json.dumps(event_result, ensure_ascii=False, sort_keys=True),
             "2026-09-03T13:00:00+08:00"),
        )
    conn.commit()

    first = refresh_case_source_manifest(
        conn, case_id=case["case_id"],
        evaluated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    assert first["source_count"] == 2
    from dietitian_health_check_api import load_health_check_detail
    detail = load_health_check_detail(conn, case_id=str(case["case_id"]))
    assert detail is not None
    assert {item["trust_label"] for item in detail["source_logs"]} == {"顧客確認・AI估算"}
    assert all(item["nutrition_snapshot"] == {} for item in detail["source_logs"])
    assert all(item["estimate"]["calories_kcal"] is None for item in detail["source_logs"])
    conn.execute("UPDATE food_logs SET trust_type='' WHERE log_id=?", (log_ids[1],))
    second = refresh_case_source_manifest(
        conn, case_id=case["case_id"],
        evaluated_at=datetime(2026, 9, 3, 13, 1, tzinfo=timezone(timedelta(hours=8))),
    )
    assert second["source_count"] == 1
    assert second["valid_day_count"] == 0
    assert [row[0] for row in conn.execute(
        "SELECT food_log_id FROM vip_health_check_source_refs"
    )] == [log_ids[0]]


def test_manifest_uses_canonical_logs_keeps_same_day_meals_and_ignores_planned_projection(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_case_source_manifest,
    )

    _create_minimal_food_ledger(conn)
    ensure_vip_health_check_schema(conn)
    started_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-1",
        activation_event_key="event-1",
        activated_at=started_at,
    )
    # Day 1: two canonical meals; both source rows must remain.
    _insert_log(conn, "log-breakfast", "2026-09-03T09:30:00+08:00", "早餐")
    _insert_log(conn, "log-lunch", "2026-09-03T12:30:00+08:00", "午餐")
    # 23:30 UTC is 07:30 next day in Taipei.
    _insert_log(conn, "log-day2-one", "2026-09-03T23:30:00Z", "早餐")
    # Day 3: two canonical meals.
    _insert_log(conn, "log-day3-a", "2026-09-05T08:00:00+08:00", "早餐")
    _insert_log(conn, "log-day3-b", "2026-09-05T18:00:00+08:00", "晚餐")
    # Pending/deleted records never qualify.
    _insert_log(conn, "log-pending", "2026-09-04T12:00:00+08:00", "午餐", status="pending")
    _insert_log(conn, "log-deleted", "2026-09-04T18:00:00+08:00", "晚餐", deleted_at="2026-09-04T19:00:00+08:00")
    # A checked planned meal is a projection only and must not become another source.
    conn.execute(
        "INSERT INTO planned_meal_checks VALUES (?,?,?,?,?,?,?)",
        ("U1", "2026-09-04", "午餐", "一日樂食便當", 500, 30, "2026-09-04T12:00:00+08:00"),
    )
    original_snapshots = conn.execute(
        "SELECT log_id,nutrition_snapshot_json FROM food_logs ORDER BY log_id"
    ).fetchall()

    result = refresh_case_source_manifest(
        conn,
        case_id=case["case_id"],
        evaluated_at=datetime(2026, 9, 6, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        minimum_meals_per_day=2,
    )
    replay = refresh_case_source_manifest(
        conn,
        case_id=case["case_id"],
        evaluated_at=datetime(2026, 9, 6, 9, 1, tzinfo=timezone(timedelta(hours=8))),
        minimum_meals_per_day=2,
    )

    refs = [tuple(row) for row in conn.execute(
        "SELECT food_log_id,local_date FROM vip_health_check_source_refs ORDER BY food_log_id"
    ).fetchall()]
    days = [tuple(row) for row in conn.execute(
        """SELECT local_date,qualifying_meal_count,completeness_status
           FROM vip_health_check_valid_days ORDER BY local_date"""
    ).fetchall()]
    assert refs == [
        ("log-breakfast", "2026-09-03"),
        ("log-day2-one", "2026-09-04"),
        ("log-day3-a", "2026-09-05"),
        ("log-day3-b", "2026-09-05"),
        ("log-lunch", "2026-09-03"),
    ]
    assert days == [
        ("2026-09-03", 2, "qualified"),
        ("2026-09-04", 1, "incomplete"),
        ("2026-09-05", 2, "qualified"),
    ]
    assert result["valid_day_count"] == 2
    assert replay["source_count"] == 5
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_source_refs").fetchone()[0] == 5
    assert conn.execute(
        "SELECT log_id,nutrition_snapshot_json FROM food_logs ORDER BY log_id"
    ).fetchall() == original_snapshots

    from dietitian_health_check_api import load_health_check_detail

    conn.execute(
        """CREATE TABLE health_profile (
               user_id TEXT PRIMARY KEY,name TEXT,tdee INTEGER,protein REAL,
               goal TEXT,restrictions TEXT,active_days TEXT
           )"""
    )
    detail = load_health_check_detail(conn, case_id=str(case["case_id"]))
    assert detail is not None
    projected = {item["log_id"]: item for item in detail["source_logs"]}
    assert projected["log-breakfast"]["local_date"] == "2026-09-03"
    assert projected["log-breakfast"]["normalized_meal_slot"] == "早餐"
    assert projected["log-lunch"]["local_date"] == "2026-09-03"
    assert projected["log-lunch"]["normalized_meal_slot"] == "午餐"


def test_third_qualified_day_moves_case_to_ready_without_copying_planned_meal(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_case_source_manifest,
    )

    _create_minimal_food_ledger(conn)
    ensure_vip_health_check_schema(conn)
    started_at = datetime(2026, 9, 3, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-1",
        activation_event_key="event-1",
        activated_at=started_at,
    )
    for log_id, timestamp, slot in [
        ("d1-a", "2026-09-03T08:00:00+08:00", "早餐"),
        ("d1-b", "2026-09-03T12:00:00+08:00", "午餐"),
        ("d2-a", "2026-09-04T08:00:00+08:00", "早餐"),
        ("d2-b", "2026-09-04T12:00:00+08:00", "午餐"),
        ("d3-a", "2026-09-05T08:00:00+08:00", "早餐"),
        ("d3-b", "2026-09-05T12:00:00+08:00", "午餐"),
    ]:
        _insert_log(conn, log_id, timestamp, slot)

    result = refresh_case_source_manifest(
        conn,
        case_id=case["case_id"],
        evaluated_at=datetime(2026, 9, 5, 13, 0, tzinfo=timezone(timedelta(hours=8))),
        minimum_meals_per_day=2,
    )

    assert result["valid_day_count"] == 3
    assert result["status"] == "ready_for_review"
    assert tuple(conn.execute(
        "SELECT status,valid_day_count FROM vip_health_check_cases WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()) == ("ready_for_review", 3)


def _ready_case(conn):
    from vip_health_check import create_first_vip_health_check_case, ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-1",
        activation_event_key="event-1",
        activated_at=now,
    )
    conn.execute(
        """UPDATE vip_health_check_cases
           SET status='ready_for_review',valid_day_count=3,source_manifest_hash='manifest-v1'
           WHERE case_id=?""",
        (case["case_id"],),
    )
    return case, now


def test_approval_creates_one_immutable_report_and_delivery_intent_atomically(conn):
    from vip_health_check import (
        approve_health_check_review,
        save_health_check_review,
    )

    case, now = _ready_case(conn)
    draft = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={"patterns": ["早餐穩定"]},
        review={"good": "蛋白質來源穩定", "priority": "蔬菜份量"},
        suggested_values={"vegetable_servings": 2},
        limitations="僅依三個有效紀錄日",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    with pytest.raises(ValueError, match="版本"):
        approve_health_check_review(
            conn,
            case_id=case["case_id"],
            review_id=draft["review_id"],
            expected_version=2,
            approved_by="dietitian-1",
            approved_at=now + timedelta(hours=1),
            report={
                "good": "蛋白質來源穩定",
                "priority": "蔬菜份量",
                "next_7_days": "每天午晚餐各一份蔬菜",
                "limitations": "僅依三個有效紀錄日",
            },
        )

    approved = approve_health_check_review(
        conn,
        case_id=case["case_id"],
        review_id=draft["review_id"],
        expected_version=1,
        approved_by="dietitian-1",
        approved_at=now + timedelta(hours=1),
        report={
            "good": "蛋白質來源穩定",
            "priority": "蔬菜份量",
            "next_7_days": "每天午晚餐各一份蔬菜",
            "limitations": "僅依三個有效紀錄日",
        },
    )
    replay = approve_health_check_review(
        conn,
        case_id=case["case_id"],
        review_id=draft["review_id"],
        expected_version=1,
        approved_by="dietitian-1",
        approved_at=now + timedelta(hours=2),
        report={
            "good": "這次重送不應覆蓋",
            "priority": "不應改變",
            "next_7_days": "不應改變",
            "limitations": "不應改變",
        },
    )

    assert approved["created"] is True
    assert replay["created"] is False
    assert replay["report_id"] == approved["report_id"]
    assert replay["delivery_key"] == approved["delivery_key"]
    stored = conn.execute(
        "SELECT report_json FROM vip_health_check_reports WHERE report_id=?",
        (approved["report_id"],),
    ).fetchone()[0]
    assert json.loads(stored)["good"] == "蛋白質來源穩定"
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_deliveries").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE vip_health_check_reports SET report_json='{}' WHERE report_id=?",
            (approved["report_id"],),
        )


def test_older_review_cannot_be_approved_after_a_newer_draft_exists(conn):
    from vip_health_check import approve_health_check_review, save_health_check_review

    case, now = _ready_case(conn)
    old = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "舊稿"},
        suggested_values={},
        limitations="舊稿",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    conn.execute(
        "UPDATE vip_health_check_cases SET source_manifest_hash='manifest-v2' WHERE case_id=?",
        (case["case_id"],),
    )
    newer = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "新稿"},
        suggested_values={},
        limitations="新稿",
        source_manifest_hash="manifest-v2",
        saved_at=now + timedelta(minutes=5),
    )

    assert newer["review_version"] == 2
    with pytest.raises(ValueError, match="最新版本"):
        approve_health_check_review(
            conn,
            case_id=case["case_id"],
            review_id=old["review_id"],
            expected_version=1,
            approved_by="dietitian-1",
            approved_at=now + timedelta(hours=1),
            report={
                "good": "舊稿",
                "priority": "舊稿",
                "next_7_days": "舊稿",
                "limitations": "舊稿",
            },
        )
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 0


def test_review_cannot_be_approved_after_source_manifest_changes(conn):
    from vip_health_check import approve_health_check_review, save_health_check_review

    case, now = _ready_case(conn)
    draft = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "草稿"},
        suggested_values={},
        limitations="依三日紀錄",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    conn.execute(
        "UPDATE vip_health_check_cases SET source_manifest_hash='manifest-v2' WHERE case_id=?",
        (case["case_id"],),
    )

    with pytest.raises(ValueError, match="來源資料已更新"):
        approve_health_check_review(
            conn,
            case_id=case["case_id"],
            review_id=draft["review_id"],
            expected_version=1,
            approved_by="dietitian-1",
            approved_at=now + timedelta(hours=1),
            report={
                "good": "草稿",
                "priority": "補水",
                "next_7_days": "執行",
                "limitations": "依三日紀錄",
            },
        )
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 0


def test_delivery_failure_and_retry_reuse_exact_report_then_customer_can_read_it(conn):
    from vip_health_check import (
        approve_health_check_review,
        get_customer_health_check_state,
        record_health_check_delivery_attempt,
        save_health_check_review,
    )

    case, now = _ready_case(conn)
    draft = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "穩定記錄"},
        suggested_values={},
        limitations="三日資料",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    approved = approve_health_check_review(
        conn,
        case_id=case["case_id"],
        review_id=draft["review_id"],
        expected_version=1,
        approved_by="dietitian-1",
        approved_at=now + timedelta(hours=1),
        report={
            "good": "穩定記錄",
            "priority": "補水",
            "next_7_days": "每日補水",
            "limitations": "三日資料",
        },
    )
    before_delivery = get_customer_health_check_state(conn, user_id="U1")
    assert before_delivery["status"] == "approved_pending_delivery"
    assert before_delivery["report"] is None
    assert get_customer_health_check_state(conn, user_id="OTHER") is None

    failed = record_health_check_delivery_attempt(
        conn,
        delivery_key=approved["delivery_key"],
        succeeded=False,
        error="LINE 500",
        attempted_at=now + timedelta(hours=2),
    )
    delivered = record_health_check_delivery_attempt(
        conn,
        delivery_key=approved["delivery_key"],
        succeeded=True,
        error="",
        attempted_at=now + timedelta(hours=3),
    )

    assert failed["status"] == "failed"
    assert delivered["status"] == "delivered"
    assert failed["report_id"] == delivered["report_id"] == approved["report_id"]
    assert delivered["attempts"] == 2
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_deliveries").fetchone()[0] == 1
    customer = get_customer_health_check_state(conn, user_id="U1")
    assert customer["status"] == "delivered"
    assert customer["report"]["good"] == "穩定記錄"

    timestamp = (now + timedelta(hours=4)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO vip_health_check_reviews (
               review_id,case_id,review_version,status,ai_observations_json,
               review_json,suggested_values_json,limitations,source_manifest_hash,
               approved_by,approved_at,created_at,updated_at
           ) VALUES ('review-v2',?,2,'approved','{}','{}','{}','',
                     'manifest-v1','dietitian-2',?,?,?)""",
        (case["case_id"], timestamp, timestamp, timestamp),
    )
    conn.execute(
        """INSERT INTO vip_health_check_reports (
               report_id,case_id,review_id,report_kind,report_version,report_json,
               source_manifest_hash,published_by,published_at
           ) VALUES ('report-v2',?,'review-v2','baseline_3day',2,?,
                     'manifest-v1','dietitian-2',?)""",
        (
            case["case_id"],
            json.dumps(
                {
                    "good": "NOT_DELIVERED",
                    "priority": "不可曝光",
                    "next_7_days": "不可曝光",
                    "limitations": "pending",
                }
            ),
            timestamp,
        ),
    )
    conn.execute(
        """INSERT INTO vip_health_check_deliveries (
               delivery_id,report_id,user_id,delivery_key,status,attempts,
               last_error,created_at,delivered_at
           ) VALUES ('delivery-v2','report-v2','U1','delivery-v2-key',
                     'pending',0,'',?,'')""",
        (timestamp,),
    )

    customer_after_pending_v2 = get_customer_health_check_state(conn, user_id="U1")
    assert customer_after_pending_v2 is not None
    assert isinstance(customer_after_pending_v2["report"], dict)
    assert customer_after_pending_v2["report"]["good"] == "穩定記錄"

    conn.execute(
        """INSERT INTO vip_health_check_reviews (
               review_id,case_id,review_version,status,ai_observations_json,
               review_json,suggested_values_json,limitations,source_manifest_hash,
               approved_by,approved_at,created_at,updated_at
           ) VALUES ('review-v3',?,3,'approved','{}','{}','{}','',
                     'manifest-v1','dietitian-3',?,?,?)""",
        (case["case_id"], timestamp, timestamp, timestamp),
    )
    conn.execute("PRAGMA ignore_check_constraints=ON")
    try:
        conn.execute(
            """INSERT INTO vip_health_check_reports (
                   report_id,case_id,review_id,report_kind,report_version,report_json,
                   source_manifest_hash,published_by,published_at
               ) VALUES ('coaching-v3',?,'review-v3','coaching_summary',3,?,
                         'manifest-v1','dietitian-3',?)""",
            (case["case_id"], json.dumps({"good": "WRONG_KIND"}), timestamp),
        )
    finally:
        conn.execute("PRAGMA ignore_check_constraints=OFF")
    conn.execute(
        """INSERT INTO vip_health_check_deliveries (
               delivery_id,report_id,user_id,delivery_key,status,attempts,
               last_error,created_at,delivered_at
           ) VALUES ('delivery-v3','coaching-v3','U1','delivery-v3-key',
                     'delivered',1,'',?,?)""",
        (timestamp, timestamp),
    )

    customer_after_other_kind = get_customer_health_check_state(conn, user_id="U1")
    assert customer_after_other_kind is not None
    assert isinstance(customer_after_other_kind["report"], dict)
    assert customer_after_other_kind["report"]["good"] == "穩定記錄"


def test_manual_coaching_payment_activates_same_user_without_vip_code(conn):
    from vip_health_check import (
        activate_dietitian_coaching,
        mark_coaching_payment_reported,
        request_dietitian_coaching,
    )

    case, now = _ready_case(conn)
    conn.execute(
        "UPDATE vip_health_check_cases SET status='delivered',report_published_at=? WHERE case_id=?",
        (now.isoformat(timespec="seconds"), case["case_id"]),
    )
    conn.execute(
        "CREATE TABLE vips(code TEXT PRIMARY KEY,meals INTEGER,duration_days INTEGER,chat_limit INTEGER,is_used INTEGER)"
    )

    order = request_dietitian_coaching(
        conn,
        user_id="U1",
        case_id=case["case_id"],
        operation_key="coaching-request-1",
        requested_at=now + timedelta(days=1),
    )
    replay = request_dietitian_coaching(
        conn,
        user_id="U1",
        case_id=case["case_id"],
        operation_key="coaching-request-1",
        requested_at=now + timedelta(days=1, minutes=1),
    )
    assert replay["order_id"] == order["order_id"]
    assert replay["created"] is False

    mark_coaching_payment_reported(
        conn,
        order_id=order["order_id"],
        user_id="U1",
        reported_at=now + timedelta(days=2),
    )
    active = activate_dietitian_coaching(
        conn,
        order_id=order["order_id"],
        confirmed_by="admin-1",
        confirmed_at=now + timedelta(days=3),
        duration_days=28,
    )

    assert active["status"] == "coaching_active"
    assert active["user_id"] == "U1"
    assert active["ends_at"] == (now + timedelta(days=31)).isoformat(timespec="seconds")
    assert conn.execute("SELECT COUNT(*) FROM vips").fetchone()[0] == 0


def test_server_customer_state_service_is_feature_gated_and_user_scoped(monkeypatch, tmp_path):
    import server
    from vip_health_check import create_first_vip_health_check_case

    db_path = _prepare_server_db(server, monkeypatch, tmp_path)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    with sqlite3.connect(db_path) as db:
        from vip_health_check import configure_vip_health_check_connection

        configure_vip_health_check_connection(db)
        create_first_vip_health_check_case(
            db,
            user_id="U1",
            first_vip_activation_id="activation-1",
            activation_event_key="event-1",
            activated_at=now,
        )

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    assert server.get_vip_health_check_state_for_user("U1") is None

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    state = server.get_vip_health_check_state_for_user("U1")
    assert state["status"] == "collecting"
    assert state["valid_day_count"] == 0
    assert server.get_vip_health_check_state_for_user("OTHER") is None


def test_server_customer_liff_registration_uses_feature_flag_and_user_scoped_loader(
    monkeypatch,
):
    import server
    from fastapi import FastAPI

    captured = {}
    target_app = FastAPI()

    def fake_attach(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)
        return True

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(server, "attach_customer_health_check_routes", fake_attach)

    assert server.register_customer_health_check_liff(target_app) is True
    assert captured["app"] is target_app
    assert captured["enabled"] is True
    assert captured["environ"] is server.os.environ
    assert captured["state_loader"] is server.get_vip_health_check_state_for_user


def test_redeem_health_check_failure_rolls_back_closes_and_unlocks_db(monkeypatch, tmp_path):
    import server

    db_path = _prepare_server_db(server, monkeypatch, tmp_path)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO vips VALUES ('#VIP-LOCK',24,31,20,0)")

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)

    def fail_case(*args, **kwargs):
        raise sqlite3.OperationalError("simulated case failure")

    monkeypatch.setattr(server, "create_first_vip_health_check_case", fail_case)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        server.redeem_code("U1", "#VIP-LOCK")

    with sqlite3.connect(db_path, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        assert db.execute("SELECT is_used FROM vips WHERE code='#VIP-LOCK'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM usage WHERE user_id='U1'").fetchone()[0] == 0
        db.rollback()


def test_same_meal_slot_rows_do_not_count_as_two_meals(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_case_source_manifest,
    )

    ensure_vip_health_check_schema(conn)
    _create_minimal_food_ledger(conn)
    start = datetime(2026, 9, 3, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="meal-dedupe",
        activation_event_key="meal-dedupe",
        activated_at=start,
    )
    for log_id in ("food-a", "food-b"):
        _insert_log(
            conn,
            log_id,
            "2026-09-03T08:00:00+08:00",
            "早餐",
        )
    result = refresh_case_source_manifest(
        conn,
        case_id=case["case_id"],
        evaluated_at=start + timedelta(days=1),
        minimum_meals_per_day=2,
    )
    assert result["source_count"] == 2
    assert result["valid_day_count"] == 0
    assert tuple(conn.execute(
        "SELECT qualifying_meal_count,completeness_status FROM vip_health_check_valid_days"
    ).fetchone()) == (1, "incomplete")


def test_same_manifest_review_save_is_idempotent(conn):
    from vip_health_check import save_health_check_review

    case, now = _ready_case(conn)
    kwargs = dict(
        case_id=case["case_id"],
        ai_observations={"pattern": "same"},
        review={"good": "same"},
        suggested_values={},
        limitations="same",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    first = save_health_check_review(conn, **kwargs)
    replay = save_health_check_review(conn, **kwargs)
    assert first["review_id"] == replay["review_id"]
    assert replay["created"] is False
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0] == 1


class _OneRowCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _StaleDeliveryConnection:
    def __init__(self, conn, stale_row):
        self._conn = conn
        self._stale_row = stale_row
        self._served = False

    def execute(self, sql, parameters=()):
        if not self._served and "FROM vip_health_check_deliveries d" in sql:
            self._served = True
            return _OneRowCursor(self._stale_row)
        return self._conn.execute(sql, parameters)


def test_stale_delivery_failure_cannot_regress_delivered_state(conn):
    from vip_health_check import (
        approve_health_check_review,
        record_health_check_delivery_attempt,
        save_health_check_review,
    )

    case, now = _ready_case(conn)
    draft = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "ok"},
        suggested_values={},
        limitations="ok",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    approved = approve_health_check_review(
        conn,
        case_id=case["case_id"],
        review_id=draft["review_id"],
        expected_version=1,
        approved_by="dietitian",
        approved_at=now,
        report={"good": "a", "priority": "b", "next_7_days": "c", "limitations": "d"},
    )
    row = conn.execute(
        """SELECT d.delivery_id,d.report_id,d.status,d.attempts,r.case_id,r.report_json
           FROM vip_health_check_deliveries d JOIN vip_health_check_reports r ON r.report_id=d.report_id
           WHERE d.delivery_key=?""",
        (approved["delivery_key"],),
    ).fetchone()
    stale_row = (row[0], row[1], "pending", 0, row[4], row[5])
    conn.execute(
        "UPDATE vip_health_check_deliveries SET status='delivered',attempts=1,delivered_at=?",
        (now.isoformat(timespec="seconds"),),
    )
    conn.execute(
        "UPDATE vip_health_check_cases SET status='delivered',report_published_at=? WHERE case_id=?",
        (now.isoformat(timespec="seconds"), case["case_id"]),
    )

    result = record_health_check_delivery_attempt(
        _StaleDeliveryConnection(conn, stale_row),
        delivery_key=approved["delivery_key"],
        succeeded=False,
        error="late failure",
        attempted_at=now + timedelta(minutes=1),
    )
    assert result["status"] == "delivered"
    assert tuple(conn.execute(
        "SELECT status,attempts FROM vip_health_check_deliveries"
    ).fetchone()) == ("delivered", 2)
    assert conn.execute("SELECT status FROM vip_health_check_cases").fetchone()[0] == "delivered"


def test_coaching_request_and_audit_are_atomic(conn):
    from vip_health_check import request_dietitian_coaching

    case, now = _ready_case(conn)
    conn.execute("UPDATE vip_health_check_cases SET status='delivered' WHERE case_id=?", (case["case_id"],))
    conn.execute(
        """CREATE TRIGGER reject_coaching_audit BEFORE INSERT ON vip_health_check_audit_log
           WHEN NEW.to_status='payment_pending'
           BEGIN SELECT RAISE(ABORT,'audit rejected'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit rejected"):
        request_dietitian_coaching(
            conn,
            user_id="U1",
            case_id=case["case_id"],
            operation_key="atomic-order",
            requested_at=now,
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM dietitian_coaching_orders").fetchone()[0] == 0


def test_coaching_payment_and_activation_audits_are_atomic(conn):
    from vip_health_check import (
        activate_dietitian_coaching,
        mark_coaching_payment_reported,
        request_dietitian_coaching,
    )

    case, now = _ready_case(conn)
    conn.execute("UPDATE vip_health_check_cases SET status='delivered' WHERE case_id=?", (case["case_id"],))
    order = request_dietitian_coaching(
        conn,
        user_id="U1",
        case_id=case["case_id"],
        operation_key="atomic-transitions",
        requested_at=now,
    )
    conn.execute(
        """CREATE TRIGGER reject_payment_audit BEFORE INSERT ON vip_health_check_audit_log
           WHEN NEW.to_status='payment_reported'
           BEGIN SELECT RAISE(ABORT,'payment audit rejected'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="payment audit rejected"):
        mark_coaching_payment_reported(
            conn,
            order_id=order["order_id"],
            user_id="U1",
            reported_at=now + timedelta(minutes=1),
        )
    conn.commit()
    assert conn.execute(
        "SELECT status FROM dietitian_coaching_orders WHERE order_id=?", (order["order_id"],)
    ).fetchone()[0] == "payment_pending"

    conn.execute("DROP TRIGGER reject_payment_audit")
    mark_coaching_payment_reported(
        conn,
        order_id=order["order_id"],
        user_id="U1",
        reported_at=now + timedelta(minutes=2),
    )
    conn.execute(
        """CREATE TRIGGER reject_activation_audit BEFORE INSERT ON vip_health_check_audit_log
           WHEN NEW.to_status='coaching_active'
           BEGIN SELECT RAISE(ABORT,'activation audit rejected'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="activation audit rejected"):
        activate_dietitian_coaching(
            conn,
            order_id=order["order_id"],
            confirmed_by="admin",
            confirmed_at=now + timedelta(minutes=3),
        )
    conn.commit()
    row = conn.execute(
        "SELECT status,confirmed_by,starts_at,ends_at FROM dietitian_coaching_orders WHERE order_id=?",
        (order["order_id"],),
    ).fetchone()
    assert tuple(row) == ("payment_reported", "", "", "")


def test_legacy_approved_report_migration_backfills_review_for_idempotent_replay(conn):
    from vip_health_check import approve_health_check_review, ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.executescript(
        """
        INSERT INTO vip_health_check_cases
          (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
           window_started_at,window_ends_at,status,valid_day_count,source_manifest_hash,
           created_at,updated_at)
        VALUES
          ('legacy-case','legacy-user','first_vip_baseline_check','legacy-act','legacy-event',
           '2026-09-01T09:00:00+08:00','2026-09-08T09:00:00+08:00',
           'approved_pending_delivery',3,'legacy-manifest',
           '2026-09-01T09:00:00+08:00','2026-09-03T09:00:00+08:00');
        INSERT INTO vip_health_check_reviews
          (review_id,case_id,review_version,status,ai_observations_json,review_json,
           suggested_values_json,limitations,source_manifest_hash,approved_by,
           approved_at,created_at,updated_at)
        VALUES
          ('legacy-review','legacy-case',1,'approved','{}','{}','{}','legacy',
           'legacy-manifest','dietitian','2026-09-03T09:00:00+08:00',
           '2026-09-03T08:00:00+08:00','2026-09-03T09:00:00+08:00');
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
          report_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
          report_kind TEXT NOT NULL DEFAULT 'baseline_3day', report_version INTEGER NOT NULL,
          report_json TEXT NOT NULL, source_manifest_hash TEXT NOT NULL,
          published_by TEXT NOT NULL, published_at TEXT NOT NULL,
          UNIQUE(case_id,report_kind,report_version)
        );
        INSERT INTO vip_health_check_reports
          (report_id,case_id,report_kind,report_version,report_json,
           source_manifest_hash,published_by,published_at)
        VALUES
          ('legacy-report','legacy-case','baseline_3day',1,
           '{"good":"legacy"}','legacy-manifest','dietitian','2026-09-03T09:00:00+08:00');
        INSERT INTO vip_health_check_deliveries
          (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
           created_at,delivered_at)
        VALUES
          ('legacy-delivery','legacy-report','legacy-user','legacy-key','pending',0,'',
           '2026-09-03T09:00:00+08:00','');
        """
    )

    ensure_vip_health_check_schema(conn)

    assert conn.execute(
        "SELECT review_id FROM vip_health_check_reports WHERE report_id='legacy-report'"
    ).fetchone()[0] == "legacy-review"
    replay = approve_health_check_review(
        conn,
        case_id="legacy-case",
        review_id="legacy-review",
        expected_version=1,
        approved_by="dietitian",
        approved_at=datetime(2026, 9, 3, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        report={"good": "x", "priority": "x", "next_7_days": "x", "limitations": "x"},
    )
    assert replay["created"] is False
    assert replay["report_id"] == "legacy-report"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE vip_health_check_reports SET report_json='{}' WHERE report_id='legacy-report'"
        )


def test_case_creation_audit_failure_propagates_and_rolls_back_case(conn):
    from vip_health_check import create_first_vip_health_check_case, ensure_vip_health_check_schema

    ensure_vip_health_check_schema(conn)
    conn.execute(
        """CREATE TRIGGER reject_case_audit BEFORE INSERT ON vip_health_check_audit_log
           WHEN NEW.to_status='collecting'
           BEGIN SELECT RAISE(ABORT,'case audit rejected'); END"""
    )
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(sqlite3.IntegrityError, match="case audit rejected"):
        create_first_vip_health_check_case(
            conn,
            user_id="U1",
            first_vip_activation_id="audit-fail",
            activation_event_key="audit-fail",
            activated_at=now,
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_cases").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_audit_log").fetchone()[0] == 0


def test_manifest_refresh_failure_rolls_back_to_previous_state(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_case_source_manifest,
    )

    ensure_vip_health_check_schema(conn)
    _create_minimal_food_ledger(conn)
    start = datetime(2026, 9, 3, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="refresh-fail",
        activation_event_key="refresh-fail",
        activated_at=start,
    )
    _insert_log(conn, "log-ok", "2026-09-03T08:00:00+08:00", "早餐")
    _insert_log(conn, "log-ok2", "2026-09-03T12:00:00+08:00", "午餐")
    refresh_case_source_manifest(
        conn,
        case_id=case["case_id"],
        evaluated_at=start + timedelta(days=1),
        minimum_meals_per_day=2,
    )
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_source_refs").fetchone()[0] == 2
    assert conn.execute(
        "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()[0] != ""

    conn.execute(
        """CREATE TRIGGER reject_source_ref BEFORE INSERT ON vip_health_check_source_refs
           WHEN NEW.food_log_id='log-new'
           BEGIN SELECT RAISE(ABORT,'source ref rejected'); END"""
    )
    _insert_log(conn, "log-new", "2026-09-04T08:00:00+08:00", "早餐")
    _insert_log(conn, "log-new2", "2026-09-04T12:00:00+08:00", "午餐")
    with pytest.raises(sqlite3.IntegrityError, match="source ref rejected"):
        refresh_case_source_manifest(
            conn,
            case_id=case["case_id"],
            evaluated_at=start + timedelta(days=2),
            minimum_meals_per_day=2,
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_source_refs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_valid_days").fetchone()[0] == 1


def test_approve_rejects_when_manifest_changed_between_validation_and_savepoint(conn):
    from vip_health_check import (
        approve_health_check_review,
        save_health_check_review,
    )

    case, now = _ready_case(conn)
    draft = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "ok"},
        suggested_values={},
        limitations="ok",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    conn.commit()

    class _ManifestChangingConnection:
        def __init__(self, real_conn, new_hash):
            self._conn = real_conn
            self._new_hash = new_hash
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT approve_health_check_report" in sql:
                self._conn.execute(
                    "UPDATE vip_health_check_cases SET source_manifest_hash=? WHERE case_id=?",
                    (self._new_hash, case["case_id"]),
                )
                self._injected = True
            return self._conn.execute(sql, parameters)

    with pytest.raises(ValueError, match="來源資料已更新"):
        approve_health_check_review(
            _ManifestChangingConnection(conn, "manifest-v2"),
            case_id=case["case_id"],
            review_id=draft["review_id"],
            expected_version=1,
            approved_by="dietitian",
            approved_at=now + timedelta(hours=1),
            report={"good": "a", "priority": "b", "next_7_days": "c", "limitations": "d"},
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 0


def test_approve_rejects_when_newer_review_appears_before_savepoint(conn):
    from vip_health_check import approve_health_check_review, save_health_check_review

    case, now = _ready_case(conn)
    draft = save_health_check_review(
        conn,
        case_id=case["case_id"],
        ai_observations={},
        review={"good": "v1"},
        suggested_values={},
        limitations="v1",
        source_manifest_hash="manifest-v1",
        saved_at=now,
    )
    conn.commit()

    class _NewerReviewConnection:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT approve_health_check_report" in sql:
                self._conn.execute(
                    """INSERT INTO vip_health_check_reviews
                       (review_id,case_id,review_version,status,ai_observations_json,
                        review_json,suggested_values_json,limitations,source_manifest_hash,
                        approved_by,approved_at,created_at,updated_at)
                       VALUES ('newer-review',?,2,'draft','{}','{}','{}','newer',
                               'manifest-v1','','',?,?)""",
                    (case["case_id"], now.isoformat(), now.isoformat()),
                )
                self._injected = True
            return self._conn.execute(sql, parameters)

    with pytest.raises(ValueError, match="只能核准最新版本"):
        approve_health_check_review(
            _NewerReviewConnection(conn),
            case_id=case["case_id"],
            review_id=draft["review_id"],
            expected_version=1,
            approved_by="dietitian",
            approved_at=now + timedelta(hours=1),
            report={"good": "a", "priority": "b", "next_7_days": "c", "limitations": "d"},
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 0


def test_redeem_activation_identity_does_not_collide_when_sqlite_rowid_is_reused(
    monkeypatch, tmp_path
):
    import server

    db_path = tmp_path / "rowid-reuse.db"
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    server.init_db()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO vips(code,meals,duration_days,chat_limit,is_used) VALUES ('#ROW-A',3,7,5,0)"
        )
        first_rowid = db.execute("SELECT rowid FROM vips WHERE code='#ROW-A'").fetchone()[0]
        db.commit()
    assert server.redeem_code("U-row-a", "#ROW-A")[0] is not None

    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM vips WHERE code='#ROW-A'")
        db.execute(
            "INSERT INTO vips(code,meals,duration_days,chat_limit,is_used) VALUES ('#ROW-B',3,7,5,0)"
        )
        second_rowid = db.execute("SELECT rowid FROM vips WHERE code='#ROW-B'").fetchone()[0]
        db.commit()
    assert second_rowid == first_rowid
    assert server.redeem_code("U-row-b", "#ROW-B")[0] is not None
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT user_id,activation_event_key FROM vip_health_check_cases ORDER BY user_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] != rows[1][1]


def test_refresh_user_case_is_noop_without_an_open_case(conn):
    from vip_health_check import (
        ensure_vip_health_check_schema,
        refresh_user_health_check_case,
    )

    _create_minimal_food_ledger(conn)
    ensure_vip_health_check_schema(conn)

    result = refresh_user_health_check_case(
        conn,
        user_id="U1",
        evaluated_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert result is None
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_source_refs").fetchone()[0] == 0


def test_refresh_user_case_projects_new_confirmed_meals(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_user_health_check_case,
    )

    _create_minimal_food_ledger(conn)
    ensure_vip_health_check_schema(conn)
    started_at = datetime(2026, 9, 3, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-live-1",
        activation_event_key="event-live-1",
        activated_at=started_at,
    )
    _insert_log(conn, "live-breakfast", "2026-09-03T08:00:00+08:00", "早餐")
    _insert_log(conn, "live-lunch", "2026-09-03T12:00:00+08:00", "午餐")

    result = refresh_user_health_check_case(
        conn,
        user_id="U1",
        evaluated_at=datetime(2026, 9, 3, 12, 1, tzinfo=timezone(timedelta(hours=8))),
    )

    assert result is not None
    assert result["case_id"] == case["case_id"]
    assert result["source_count"] == 2
    assert result["valid_day_count"] == 1
    assert tuple(conn.execute(
        "SELECT status,valid_day_count FROM vip_health_check_cases WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()) == ("collecting", 1)


def test_refresh_rejects_unregistered_or_mismatched_day_rule_before_db_access(conn):
    from vip_health_check import refresh_case_source_manifest

    evaluated_at = datetime(2026, 9, 6, tzinfo=timezone.utc)
    for minimum, rule, expected_error in (
        (1, "custom-minimum-one-v1", "不支援"),
        (1, "draft-confirmed-meals-v1", "不匹配"),
    ):
        with pytest.raises(ValueError, match=expected_error):
            refresh_case_source_manifest(
                conn,
                case_id="case-never-read",
                evaluated_at=evaluated_at,
                minimum_meals_per_day=minimum,
                rule_version=rule,
            )


def test_refresh_cannot_regress_case_approved_before_savepoint(conn):
    from vip_health_check import refresh_case_source_manifest

    case, now = _ready_case(conn)
    _create_minimal_food_ledger(conn)

    class _ApprovedBeforeRefreshConnection:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT refresh_case_source_manifest" in sql:
                self._conn.execute(
                    "UPDATE vip_health_check_cases SET status='approved_pending_delivery' WHERE case_id=?",
                    (case["case_id"],),
                )
                self._injected = True
            return self._conn.execute(sql, parameters)

    with pytest.raises(ValueError, match="不可重建來源"):
        refresh_case_source_manifest(
            _ApprovedBeforeRefreshConnection(conn),
            case_id=case["case_id"],
            evaluated_at=now + timedelta(hours=1),
        )
    assert conn.execute(
        "SELECT status FROM vip_health_check_cases WHERE case_id=?", (case["case_id"],)
    ).fetchone()[0] == "approved_pending_delivery"


def test_save_review_rechecks_manifest_inside_savepoint(conn):
    from vip_health_check import save_health_check_review

    case, now = _ready_case(conn)

    class _ManifestChangedBeforeSaveConnection:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT save_health_check_review" in sql:
                self._conn.execute(
                    "UPDATE vip_health_check_cases SET source_manifest_hash='manifest-v2' WHERE case_id=?",
                    (case["case_id"],),
                )
                self._injected = True
            return self._conn.execute(sql, parameters)

    with pytest.raises(ValueError, match="來源資料已更新"):
        save_health_check_review(
            _ManifestChangedBeforeSaveConnection(conn),
            case_id=case["case_id"],
            ai_observations={},
            review={"good": "stale"},
            suggested_values={},
            limitations="stale",
            source_manifest_hash="manifest-v1",
            saved_at=now,
        )
    assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0] == 0


def test_coaching_activation_does_not_overwrite_competing_activation(conn):
    from vip_health_check import (
        activate_dietitian_coaching,
        mark_coaching_payment_reported,
        request_dietitian_coaching,
    )

    case, now = _ready_case(conn)
    conn.execute("UPDATE vip_health_check_cases SET status='delivered' WHERE case_id=?", (case["case_id"],))
    order = request_dietitian_coaching(
        conn,
        user_id="U1",
        case_id=case["case_id"],
        operation_key="competing-activation",
        requested_at=now,
    )
    mark_coaching_payment_reported(
        conn,
        order_id=order["order_id"],
        user_id="U1",
        reported_at=now + timedelta(minutes=1),
    )
    first_start = now + timedelta(hours=1)
    first_end = first_start + timedelta(days=28)

    class _CompetingActivationConnection:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT activate_dietitian_coaching" in sql:
                self._conn.execute(
                    """UPDATE dietitian_coaching_orders
                       SET status='coaching_active',confirmed_by='admin-first',confirmed_at=?,
                           starts_at=?,ends_at=?,updated_at=? WHERE order_id=?""",
                    (
                        first_start.isoformat(timespec="seconds"),
                        first_start.isoformat(timespec="seconds"),
                        first_end.isoformat(timespec="seconds"),
                        first_start.isoformat(timespec="seconds"),
                        order["order_id"],
                    ),
                )
                self._injected = True
            return self._conn.execute(sql, parameters)

    result = activate_dietitian_coaching(
        _CompetingActivationConnection(conn),
        order_id=order["order_id"],
        confirmed_by="admin-second",
        confirmed_at=now + timedelta(hours=2),
    )
    row = conn.execute(
        "SELECT confirmed_by,starts_at,ends_at FROM dietitian_coaching_orders WHERE order_id=?",
        (order["order_id"],),
    ).fetchone()
    assert tuple(row) == (
        "admin-first",
        first_start.isoformat(timespec="seconds"),
        first_end.isoformat(timespec="seconds"),
    )
    assert result["starts_at"] == first_start.isoformat(timespec="seconds")


def test_payment_report_does_not_overwrite_competing_report_time(conn):
    from vip_health_check import mark_coaching_payment_reported, request_dietitian_coaching

    case, now = _ready_case(conn)
    conn.execute("UPDATE vip_health_check_cases SET status='delivered' WHERE case_id=?", (case["case_id"],))
    order = request_dietitian_coaching(
        conn,
        user_id="U1",
        case_id=case["case_id"],
        operation_key="competing-payment",
        requested_at=now,
    )
    first_reported = now + timedelta(minutes=1)

    class _CompetingPaymentConnection:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT mark_coaching_payment_reported" in sql:
                self._conn.execute(
                    """UPDATE dietitian_coaching_orders
                       SET status='payment_reported',payment_reported_at=?,updated_at=?
                       WHERE order_id=?""",
                    (
                        first_reported.isoformat(timespec="seconds"),
                        first_reported.isoformat(timespec="seconds"),
                        order["order_id"],
                    ),
                )
                self._injected = True
            return self._conn.execute(sql, parameters)

    result = mark_coaching_payment_reported(
        _CompetingPaymentConnection(conn),
        order_id=order["order_id"],
        user_id="U1",
        reported_at=now + timedelta(minutes=2),
    )
    assert result["status"] == "payment_reported"
    assert conn.execute(
        "SELECT payment_reported_at FROM dietitian_coaching_orders WHERE order_id=?",
        (order["order_id"],),
    ).fetchone()[0] == first_reported.isoformat(timespec="seconds")


def test_refresh_reads_ledger_inside_savepoint(conn):
    from vip_health_check import (
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_case_source_manifest,
    )

    _create_minimal_food_ledger(conn)
    ensure_vip_health_check_schema(conn)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    case = create_first_vip_health_check_case(
        conn,
        user_id="U1",
        first_vip_activation_id="activation-ledger-race",
        activation_event_key="event-ledger-race",
        activated_at=now,
    )
    _insert_log(conn, "before-savepoint", "2026-09-03T10:00:00+08:00", "早餐")

    class _LedgerChangedBeforeSavepoint:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._injected = False

        def execute(self, sql, parameters=()):
            if not self._injected and "SAVEPOINT refresh_case_source_manifest" in sql:
                _insert_log(self._conn, "before-savepoint-2", "2026-09-03T12:00:00+08:00", "午餐")
                self._injected = True
            return self._conn.execute(sql, parameters)

    result = refresh_case_source_manifest(
        _LedgerChangedBeforeSavepoint(conn),
        case_id=case["case_id"],
        evaluated_at=now + timedelta(days=1),
    )
    assert result["source_count"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?", (case["case_id"],)
    ).fetchone()[0] == 2


def test_failed_legacy_delivery_cannot_regress_delivered_case(conn):
    from vip_health_check import record_health_check_delivery_attempt

    case, now = _ready_case(conn)
    published = now.isoformat(timespec="seconds")
    conn.execute(
        "UPDATE vip_health_check_cases SET status='delivered',report_published_at=? WHERE case_id=?",
        (published, case["case_id"]),
    )
    conn.executemany(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES (?,?,?,'approved','manifest-v1','dietitian',?,?,?)""",
        [
            ("review-one", case["case_id"], 1, published, published, published),
            ("review-two", case["case_id"], 2, published, published, published),
        ],
    )
    for suffix, status in (("one", "delivered"), ("two", "pending")):
        conn.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,report_kind,report_version,review_id,report_json,
                source_manifest_hash,published_by,published_at)
               VALUES (?,?, 'baseline_3day', ?, ?, '{}','manifest-v1','dietitian',?)""",
            (f"report-{suffix}", case["case_id"], 1 if suffix == "one" else 2, f"review-{suffix}", published),
        )
        conn.execute(
            """INSERT INTO vip_health_check_deliveries
               (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at)
               VALUES (?,?,?,?,?,0,'',?,?)""",
            (f"delivery-{suffix}", f"report-{suffix}", "U1", f"key-{suffix}", status, published, published if status == "delivered" else ""),
        )

    record_health_check_delivery_attempt(
        conn,
        delivery_key="key-two",
        succeeded=False,
        error="legacy failure",
        attempted_at=now + timedelta(minutes=1),
    )
    assert tuple(conn.execute(
        "SELECT status,report_published_at FROM vip_health_check_cases WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()) == ("delivered", published)


def test_schema_restores_strict_report_immutability_after_backfill(conn):
    from vip_health_check import ensure_vip_health_check_schema

    case, now = _ready_case(conn)
    published = now.isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO vip_health_check_reviews
           (review_id,case_id,review_version,status,source_manifest_hash,approved_by,
            approved_at,created_at,updated_at)
           VALUES ('review-99',?,99,'approved','manifest-v1','dietitian',?,?,?)""",
        (case["case_id"], published, published, published),
    )
    conn.executescript(
        """
        DROP TABLE vip_health_check_deliveries;
        DROP TRIGGER vip_health_check_reports_no_update;
        DROP TRIGGER vip_health_check_reports_no_delete;
        DROP TABLE vip_health_check_reports;
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day',
            report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version)
        );
        """
    )
    conn.execute(
        """INSERT INTO vip_health_check_reports
           (report_id,case_id,report_kind,report_version,report_json,
            source_manifest_hash,published_by,published_at)
           VALUES ('legacy-empty-review',?,'baseline_3day',99,'{}','manifest-v1','dietitian',?)""",
        (case["case_id"], published),
    )
    ensure_vip_health_check_schema(conn)
    assert conn.execute(
        "SELECT review_id FROM vip_health_check_reports WHERE report_id='legacy-empty-review'"
    ).fetchone()[0] == "review-99"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE vip_health_check_reports SET report_json='arbitrary-mutation' WHERE report_id='legacy-empty-review'"
        )


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_named_environment_init_db_fails_closed_on_vip_schema_error(
    monkeypatch, tmp_path, app_env
):
    import server

    db_path = tmp_path / f"{app_env}-schema-failure.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "APP_ENV", app_env)

    def fail_schema(_conn):
        raise sqlite3.IntegrityError("forced VIP schema failure")

    monkeypatch.setattr(server, "ensure_vip_health_check_schema", fail_schema)

    with pytest.raises(sqlite3.IntegrityError, match="forced VIP schema failure"):
        server.init_db()

    # Failure handling must close the connection and release the write lock.
    with sqlite3.connect(db_path, timeout=0.1) as check:
        check.execute("BEGIN IMMEDIATE")
        check.rollback()


def test_legacy_init_db_keeps_graceful_schema_failure_compatibility(monkeypatch, tmp_path):
    import server

    db_path = tmp_path / "legacy-schema-failure.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "APP_ENV", "legacy")

    def fail_schema(_conn):
        raise sqlite3.IntegrityError("forced VIP schema failure")

    monkeypatch.setattr(server, "ensure_vip_health_check_schema", fail_schema)

    assert server.init_db() is None
    with sqlite3.connect(db_path, timeout=0.1) as check:
        check.execute("BEGIN IMMEDIATE")
        check.rollback()
