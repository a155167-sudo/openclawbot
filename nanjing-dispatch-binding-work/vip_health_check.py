"""VIP 首次三日健檢的隔離資料層與狀態機。

本模組不依賴 LINE、FastAPI 或全域 DB_PATH；所有寫入均由呼叫端提供
SQLite connection，因此可併入既有 VIP 兌換交易，亦可在測試中完全隔離。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo


BENEFIT_KEY = "first_vip_baseline_check"
FEATURE_FLAG = "VIP_HEALTH_CHECK_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def require_vip_health_check_connection(
    conn: sqlite3.Connection,
) -> sqlite3.Connection:
    """Fail closed unless SQLite is actively enforcing foreign keys."""
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()
    if not enabled or int(enabled[0]) != 1:
        raise sqlite3.IntegrityError(
            "VIP health-check connection requires PRAGMA foreign_keys=ON"
        )
    return conn


def configure_vip_health_check_connection(
    conn: sqlite3.Connection,
) -> sqlite3.Connection:
    """Enable and verify FK enforcement before the caller starts a transaction."""
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()
    if enabled and int(enabled[0]) == 1:
        return conn
    if conn.in_transaction:
        raise sqlite3.IntegrityError(
            "cannot enable PRAGMA foreign_keys inside an active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    return require_vip_health_check_connection(conn)


def is_vip_health_check_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """只有明確 truthy 值才啟用；未設定時一律關閉。"""
    source = os.environ if environment is None else environment
    return str(source.get(FEATURE_FLAG, "")).strip().lower() in _TRUTHY


def _execute_sql_script_without_implicit_commit(
    conn: sqlite3.Connection, script: str
) -> None:
    """逐句執行 SQL script，避免 sqlite3.executescript() 隱式提交交易。"""
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            conn.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise sqlite3.OperationalError("incomplete VIP health-check schema statement")


def _normalize_schema_sql(sql: str) -> str:
    """Remove SQL comments/spacing outside literals for controlled DDL comparison."""
    text = str(sql or "")
    pieces: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            closer = text.find("*/", index + 2)
            index = len(text) if closer < 0 else closer + 2
            continue

        opener = text[index]
        if opener in {"'", '"', "`", "["}:
            closer = "]" if opener == "[" else opener
            start = index
            index += 1
            while index < len(text):
                if text[index] != closer:
                    index += 1
                    continue
                if closer != "]" and index + 1 < len(text) and text[index + 1] == closer:
                    index += 2
                    continue
                index += 1
                break
            pieces.append(text[start:index])
            continue
        if not opener.isspace():
            pieces.append(opener.lower())
        index += 1
    return "".join(pieces)


def _extract_check_expressions(sql: str) -> tuple[str, ...]:
    """Extract canonical CHECK bodies while ignoring comments and quoted tokens."""
    text = str(sql or "")

    def skip_quoted(position: int) -> int:
        opener = text[position]
        closer = "]" if opener == "[" else opener
        position += 1
        while position < len(text):
            if text[position] != closer:
                position += 1
                continue
            if closer != "]" and position + 1 < len(text) and text[position + 1] == closer:
                position += 2
                continue
            return position + 1
        return position

    def skip_trivia(position: int) -> int:
        while position < len(text):
            if text[position].isspace():
                position += 1
            elif text.startswith("--", position):
                newline = text.find("\n", position + 2)
                position = len(text) if newline < 0 else newline + 1
            elif text.startswith("/*", position):
                closer = text.find("*/", position + 2)
                position = len(text) if closer < 0 else closer + 2
            else:
                break
        return position

    expressions: list[str] = []
    index = 0
    while index < len(text):
        index = skip_trivia(index)
        if index >= len(text):
            break
        if text[index] in {"'", '"', "`", "["}:
            index = skip_quoted(index)
            continue
        if text[index].isalpha() or text[index] == "_":
            start = index
            index += 1
            while index < len(text) and (text[index].isalnum() or text[index] == "_"):
                index += 1
            if text[start:index].casefold() != "check":
                continue
            opening = skip_trivia(index)
            if opening >= len(text) or text[opening] != "(":
                continue
            depth = 1
            cursor = opening + 1
            while cursor < len(text) and depth:
                if text.startswith("--", cursor):
                    newline = text.find("\n", cursor + 2)
                    cursor = len(text) if newline < 0 else newline + 1
                    continue
                if text.startswith("/*", cursor):
                    closer = text.find("*/", cursor + 2)
                    cursor = len(text) if closer < 0 else closer + 2
                    continue
                if text[cursor] in {"'", '"', "`", "["}:
                    cursor = skip_quoted(cursor)
                    continue
                if text[cursor] == "(":
                    depth += 1
                elif text[cursor] == ")":
                    depth -= 1
                    if depth == 0:
                        expressions.append(
                            _normalize_schema_sql(text[opening + 1:cursor])
                        )
                        cursor += 1
                        break
                cursor += 1
            index = cursor
            continue
        index += 1
    return tuple(sorted(expressions))


def _unquoted_schema_words(sql: str) -> tuple[str, ...]:
    """Return lower-case DDL words outside comments and quoted tokens."""
    text = str(sql or "")
    words: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            closer = text.find("*/", index + 2)
            index = len(text) if closer < 0 else closer + 2
            continue
        opener = text[index]
        if opener in {"'", '"', "`", "["}:
            closer = "]" if opener == "[" else opener
            index += 1
            while index < len(text):
                if text[index] != closer:
                    index += 1
                    continue
                if closer != "]" and index + 1 < len(text) and text[index + 1] == closer:
                    index += 2
                    continue
                index += 1
                break
            continue
        if opener.isalpha() or opener == "_":
            start = index
            index += 1
            while index < len(text) and (text[index].isalnum() or text[index] == "_"):
                index += 1
            words.append(text[start:index].casefold())
            continue
        index += 1
    return tuple(words)


def ensure_vip_health_check_schema(conn: sqlite3.Connection) -> None:
    """以不提交 caller transaction 的 SAVEPOINT 執行 failure-atomic migration。"""
    configure_vip_health_check_connection(conn)
    savepoint = "vip_health_check_schema_" + uuid.uuid4().hex
    previous_deferred = int(conn.execute("PRAGMA defer_foreign_keys").fetchone()[0])
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _ensure_vip_health_check_schema(conn)
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        conn.execute(f"PRAGMA defer_foreign_keys={previous_deferred}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    conn.execute(f"PRAGMA defer_foreign_keys={previous_deferred}")


def _rebuild_legacy_reports_table(
    conn: sqlite3.Connection, report_columns: set[str]
) -> None:
    """Rebuild legacy reports with real FKs without exposing partial schema/data."""
    def normalize_schema_sql(sql: str) -> str:
        return _normalize_schema_sql(sql)

    def allowed_schema_sql_variants(canonical: str) -> set[str]:
        variants = {canonical}
        for prefix in ("createtrigger", "createuniqueindex", "createindex"):
            if canonical.startswith(prefix):
                variants.add(prefix + "ifnotexists" + canonical[len(prefix) :])
                break
        return variants

    legacy_columns_without_review = (
        "report_id", "case_id", "report_kind", "report_version", "report_json",
        "source_manifest_hash", "published_by", "published_at",
    )
    legacy_columns_with_review = (
        "report_id", "case_id", "review_id", "report_kind", "report_version",
        "report_json", "source_manifest_hash", "published_by", "published_at",
    )
    actual_table_info = tuple(
        (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in conn.execute("PRAGMA table_info(vip_health_check_reports)")
    )
    expected_table_info_by_columns = {
        legacy_columns_without_review: (
            ("report_id", "TEXT", 0, None, 1),
            ("case_id", "TEXT", 1, None, 0),
            ("report_kind", "TEXT", 1, "'baseline_3day'", 0),
            ("report_version", "INTEGER", 1, None, 0),
            ("report_json", "TEXT", 1, None, 0),
            ("source_manifest_hash", "TEXT", 1, None, 0),
            ("published_by", "TEXT", 1, None, 0),
            ("published_at", "TEXT", 1, None, 0),
        ),
        legacy_columns_with_review: (
            ("report_id", "TEXT", 0, None, 1),
            ("case_id", "TEXT", 1, None, 0),
            ("review_id", "TEXT", 1, "''", 0),
            ("report_kind", "TEXT", 1, "'baseline_3day'", 0),
            ("report_version", "INTEGER", 1, None, 0),
            ("report_json", "TEXT", 1, None, 0),
            ("source_manifest_hash", "TEXT", 1, None, 0),
            ("published_by", "TEXT", 1, None, 0),
            ("published_at", "TEXT", 1, None, 0),
        ),
    }
    actual_column_order = tuple(item[0] for item in actual_table_info)
    expected_table_info = expected_table_info_by_columns.get(actual_column_order)
    unique_fingerprints = {
        tuple(
            (item[2], bool(item[3]), str(item[4]).upper(), bool(item[5]))
            for item in conn.execute(f"PRAGMA index_xinfo({index_row[1]})")
            if item[5]
        )
        for index_row in conn.execute("PRAGMA index_list(vip_health_check_reports)")
        if index_row[2] and index_row[3] == "u" and not index_row[4]
    }
    expected_unique = {
        (("case_id", False, "BINARY", True),
         ("report_kind", False, "BINARY", True),
         ("report_version", False, "BINARY", True))
    }
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vip_health_check_reports'"
    ).fetchone()
    table_sql = table_sql_row[0] if table_sql_row else ""
    unsupported_words = set(_unquoted_schema_words(table_sql)) & {
        "collate", "conflict", "deferrable", "initially", "match", "strict", "without",
    }
    if (
        set(report_columns) != set(actual_column_order)
        or expected_table_info is None
        or actual_table_info != expected_table_info
        or unique_fingerprints != expected_unique
        or conn.execute("PRAGMA foreign_key_list(vip_health_check_reports)").fetchall()
        or _extract_check_expressions(table_sql)
        or unsupported_words
    ):
        raise sqlite3.IntegrityError(
            "unsupported legacy vip_health_check_reports schema"
        )

    expected_schema_objects = {
        "vip_health_check_reports": {
            "vip_health_check_reports_no_update": (
                "trigger",
                normalize_schema_sql(
                    """CREATE TRIGGER vip_health_check_reports_no_update
                    BEFORE UPDATE ON vip_health_check_reports
                    BEGIN
                        SELECT RAISE(ABORT, 'published health-check reports are immutable');
                    END"""
                ),
            ),
            "vip_health_check_reports_no_delete": (
                "trigger",
                normalize_schema_sql(
                    """CREATE TRIGGER vip_health_check_reports_no_delete
                    BEFORE DELETE ON vip_health_check_reports
                    BEGIN
                        SELECT RAISE(ABORT, 'published health-check reports are immutable');
                    END"""
                ),
            ),
            "idx_vip_health_check_reports_review": (
                "index",
                normalize_schema_sql(
                    """CREATE UNIQUE INDEX idx_vip_health_check_reports_review
                    ON vip_health_check_reports(review_id) WHERE review_id<>''"""
                ),
            ),
        },
        "vip_health_check_deliveries": {
            "idx_vip_health_check_deliveries_status": (
                "index",
                normalize_schema_sql(
                    """CREATE INDEX idx_vip_health_check_deliveries_status
                    ON vip_health_check_deliveries(status, created_at)"""
                ),
            ),
        },
    }
    for table_name, expected_objects in expected_schema_objects.items():
        objects = conn.execute(
            """SELECT type,name,sql FROM sqlite_master
               WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL""",
            (table_name,),
        ).fetchall()
        unknown = [
            (object_type, name)
            for object_type, name, sql in objects
            if name not in expected_objects
            or object_type != expected_objects[name][0]
            or normalize_schema_sql(sql)
            not in allowed_schema_sql_variants(expected_objects[name][1])
        ]
        if unknown:
            raise sqlite3.IntegrityError(
                f"unsupported custom schema objects on {table_name}: {unknown!r}"
            )

    if "review_id" in report_columns:
        review_join = """ON review.status='approved' AND (
                 (report.review_id<>''
                  AND review.review_id=report.review_id
                  AND review.case_id=report.case_id
                  AND review.review_version=report.report_version)
                 OR
                 (report.review_id=''
                  AND review.case_id=report.case_id
                  AND review.review_version=report.report_version)
               )"""
    else:
        review_join = """ON review.case_id=report.case_id
              AND review.review_version=report.report_version
              AND review.status='approved'"""

    unresolved = conn.execute(
        f"""SELECT report.report_id, COUNT(review.review_id)
           FROM vip_health_check_reports AS report
           LEFT JOIN vip_health_check_reviews AS review
             {review_join}
           GROUP BY report.report_id
           HAVING COUNT(review.review_id)<>1
           LIMIT 1"""
    ).fetchone()
    if unresolved:
        raise sqlite3.IntegrityError(
            "legacy VIP health-check report has no unique approved review: "
            f"{unresolved[0]}"
        )

    suffix = uuid.uuid4().hex
    replacement = "vip_health_check_reports_rebuild_" + suffix
    delivery_backup = "vip_health_check_deliveries_rebuild_" + suffix
    conn.execute(
        f"CREATE TABLE {delivery_backup} AS SELECT * FROM vip_health_check_deliveries"
    )
    conn.execute("DROP TABLE vip_health_check_deliveries")
    conn.execute(
        f"""CREATE TABLE {replacement} (
            report_id TEXT PRIMARY KEY NOT NULL,
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
            FOREIGN KEY(case_id, review_id)
                REFERENCES vip_health_check_reviews(case_id, review_id)
        )"""
    )
    conn.execute(
        f"""INSERT INTO {replacement}
               (report_id,case_id,review_id,report_kind,report_version,report_json,
                source_manifest_hash,published_by,published_at)
           SELECT report.report_id,report.case_id,review.review_id,report.report_kind,
                  report.report_version,report.report_json,report.source_manifest_hash,
                  report.published_by,report.published_at
           FROM vip_health_check_reports AS report
           JOIN vip_health_check_reviews AS review
             {review_join}"""
    )
    conn.execute("DROP TABLE vip_health_check_reports")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO vip_health_check_reports")
    _execute_sql_script_without_implicit_commit(
        conn,
        """
        CREATE TRIGGER vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN
            SELECT RAISE(ABORT, 'published health-check reports are immutable');
        END;
        CREATE TRIGGER vip_health_check_reports_no_delete
        BEFORE DELETE ON vip_health_check_reports
        BEGIN
            SELECT RAISE(ABORT, 'published health-check reports are immutable');
        END;
        """,
    )
    conn.execute(
        """CREATE TABLE vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY NOT NULL,
            report_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','failed','delivered')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        )"""
    )
    conn.execute(
        f"""INSERT INTO vip_health_check_deliveries
               (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
                created_at,delivered_at)
           SELECT delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
                  created_at,delivered_at
           FROM {delivery_backup}"""
    )
    conn.execute(f"DROP TABLE {delivery_backup}")
    conn.execute(
        """CREATE INDEX idx_vip_health_check_deliveries_status
           ON vip_health_check_deliveries(status, created_at)"""
    )


def _verify_vip_health_check_foreign_keys(conn: sqlite3.Connection) -> None:
    """Reject historical VIP orphan rows even if they were written with FK checks off."""
    tables = (
        "vip_health_check_valid_days",
        "vip_health_check_source_refs",
        "vip_health_check_reviews",
        "vip_health_check_reports",
        "vip_health_check_deliveries",
        "vip_health_check_audit_log",
        "dietitian_coaching_orders",
    )
    violations = []
    for table_name in tables:
        violations.extend(
            (table_name, *row)
            for row in conn.execute(f"PRAGMA foreign_key_check({table_name})").fetchall()
        )
    if violations:
        raise sqlite3.IntegrityError(
            f"VIP health-check foreign key violations: {violations!r}"
        )


def _verify_vip_health_check_check_constraints(conn: sqlite3.Connection) -> None:
    """Exercise every CHECK against an isolated clone of the installed table DDL."""
    table_names = (
        "vip_health_check_activation_events",
        "vip_health_check_cases",
        "vip_health_check_reviews",
        "vip_health_check_reports",
        "vip_health_check_deliveries",
        "dietitian_coaching_orders",
    )
    shadow = sqlite3.connect(":memory:")
    try:
        shadow.execute("PRAGMA foreign_keys=ON")
        for table_name in table_names:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if not row or not row[0]:
                raise sqlite3.IntegrityError(f"missing table DDL: {table_name}")
            shadow.execute(row[0])

        shadow.execute(
            """INSERT INTO vip_health_check_cases
               (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
                window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
               VALUES ('case-valid','user-valid','first_vip_baseline_check','activation-valid',
                       'event-valid','start','end','collecting',0,'now','now')"""
        )
        shadow.execute(
            """INSERT INTO vip_health_check_reviews
               (review_id,case_id,review_version,status,source_manifest_hash,created_at,updated_at)
               VALUES ('review-valid','case-valid',1,'approved','hash','now','now'),
                      ('review-kind-probe','case-valid',2,'approved','hash','now','now')"""
        )
        shadow.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,review_id,report_kind,report_version,report_json,
                source_manifest_hash,published_by,published_at)
               VALUES ('report-valid','case-valid','review-valid','baseline_3day',1,
                       '{}','hash','dietitian','now')"""
        )

        probes = (
            (
                "activation_events.activation_type",
                """INSERT INTO vip_health_check_activation_events
                   (activation_event_key,user_id,activation_type,occurred_at)
                   VALUES ('event-invalid','user-invalid','invalid','now')""",
            ),
            (
                "cases.case_id_not_null",
                """INSERT INTO vip_health_check_cases
                   (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
                    window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
                   VALUES (NULL,'user-null-case','first_vip_baseline_check','activation-null-case',
                           'event-null-case','start','end','collecting',0,'now','now')""",
            ),
            (
                "reviews.review_id_not_null",
                """INSERT INTO vip_health_check_reviews
                   (review_id,case_id,review_version,status,source_manifest_hash,created_at,updated_at)
                   VALUES (NULL,'case-valid',4,'approved','hash','now','now')""",
            ),
            (
                "reports.report_id_not_null",
                """INSERT INTO vip_health_check_reports
                   (report_id,case_id,review_id,report_kind,report_version,report_json,
                    source_manifest_hash,published_by,published_at)
                   VALUES (NULL,'case-valid','review-kind-probe','baseline_3day',2,
                           '{}','hash','dietitian','now')""",
            ),
            (
                "deliveries.delivery_id_not_null",
                """INSERT INTO vip_health_check_deliveries
                   (delivery_id,report_id,user_id,delivery_key,status,created_at)
                   VALUES (NULL,'report-valid','user-valid','delivery-null-id',
                           'pending','now')""",
            ),
            (
                "coaching.order_id_not_null",
                """INSERT INTO dietitian_coaching_orders
                   (order_id,user_id,case_id,product_type,operation_key,status,
                    requested_at,updated_at)
                   VALUES (NULL,'user-valid','case-valid','dietitian_coaching_4w',
                           'operation-null-id','payment_pending','now','now')""",
            ),
            (
                "cases.benefit_key",
                """INSERT INTO vip_health_check_cases
                   (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
                    window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
                   VALUES ('bad-benefit','user-bad-benefit','other','activation-bad-benefit',
                           'event-bad-benefit','start','end','collecting',0,'now','now')""",
            ),
            (
                "cases.status",
                """INSERT INTO vip_health_check_cases
                   (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
                    window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
                   VALUES ('bad-status','user-bad-status','first_vip_baseline_check',
                           'activation-bad-status','event-bad-status','start','end','invalid',0,
                           'now','now')""",
            ),
            (
                "cases.valid_day_count",
                """INSERT INTO vip_health_check_cases
                   (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
                    window_started_at,window_ends_at,status,valid_day_count,created_at,updated_at)
                   VALUES ('bad-count','user-bad-count','first_vip_baseline_check',
                           'activation-bad-count','event-bad-count','start','end','collecting',8,
                           'now','now')""",
            ),
            (
                "reviews.status",
                """INSERT INTO vip_health_check_reviews
                   (review_id,case_id,review_version,status,source_manifest_hash,created_at,updated_at)
                   VALUES ('review-bad-status','case-valid',3,'invalid','hash','now','now')""",
            ),
            (
                "reports.report_kind",
                """INSERT INTO vip_health_check_reports
                   (report_id,case_id,review_id,report_kind,report_version,report_json,
                    source_manifest_hash,published_by,published_at)
                   VALUES ('report-bad-kind','case-valid','review-kind-probe','other',2,
                           '{}','hash','dietitian','now')""",
            ),
            (
                "deliveries.delivery_key_not_null",
                """INSERT INTO vip_health_check_deliveries
                   (delivery_id,report_id,user_id,delivery_key,status,created_at)
                   VALUES ('delivery-null-key','report-valid','user-valid',
                           NULL,'pending','now')""",
            ),
            (
                "deliveries.status_not_null",
                """INSERT INTO vip_health_check_deliveries
                   (delivery_id,report_id,user_id,delivery_key,status,created_at)
                   VALUES ('delivery-null-status','report-valid','user-valid',
                           'delivery-key-null-status',NULL,'now')""",
            ),
            (
                "deliveries.status",
                """INSERT INTO vip_health_check_deliveries
                   (delivery_id,report_id,user_id,delivery_key,status,created_at)
                   VALUES ('delivery-bad-status','report-valid','user-valid',
                           'delivery-key-bad-status','invalid','now')""",
            ),
            (
                "coaching.product_type",
                """INSERT INTO dietitian_coaching_orders
                   (order_id,user_id,case_id,product_type,operation_key,status,requested_at,updated_at)
                   VALUES ('order-bad-product','user-valid','case-valid','other',
                           'operation-bad-product','payment_pending','now','now')""",
            ),
            (
                "coaching.status",
                """INSERT INTO dietitian_coaching_orders
                   (order_id,user_id,case_id,product_type,operation_key,status,requested_at,updated_at)
                   VALUES ('order-bad-status','user-valid','case-valid',
                           'dietitian_coaching_4w','operation-bad-status','invalid','now','now')""",
            ),
        )
        for label, sql in probes:
            try:
                shadow.execute(sql)
            except sqlite3.IntegrityError:
                continue
            raise sqlite3.IntegrityError(
                f"unsupported VIP health-check constraint probe: {label}"
            )
    finally:
        shadow.close()


def _verify_vip_health_check_schema_shape(conn: sqlite3.Connection) -> None:
    """拒絕缺欄位、缺 FK、缺索引或缺 immutable trigger 的既有 schema。"""
    required_columns = {
        "vip_health_check_activation_events": {
            "activation_event_key", "user_id", "activation_type",
            "prior_usage_status", "prior_expiry_date", "occurred_at",
        },
        "vip_health_check_cases": {
            "case_id", "user_id", "benefit_key", "first_vip_activation_id",
            "activation_event_key", "window_started_at", "window_ends_at", "status",
            "valid_day_count", "source_manifest_hash", "submitted_at",
            "report_published_at", "created_at", "updated_at",
        },
        "vip_health_check_valid_days": {
            "case_id", "local_date", "rule_version", "qualifying_meal_count",
            "completeness_status", "evaluated_at",
        },
        "vip_health_check_source_refs": {
            "case_id", "food_log_id", "food_log_version", "local_date",
            "included_reason", "source_hash", "created_at",
        },
        "vip_health_check_reviews": {
            "review_id", "case_id", "review_version", "status",
            "ai_observations_json", "review_json", "suggested_values_json",
            "limitations", "source_manifest_hash", "approved_by", "approved_at",
            "created_at", "updated_at",
        },
        "vip_health_check_reports": {
            "report_id", "case_id", "review_id", "report_kind", "report_version",
            "report_json", "source_manifest_hash", "published_by", "published_at",
        },
        "vip_health_check_deliveries": {
            "delivery_id", "report_id", "user_id", "delivery_key", "status",
            "attempts", "last_error", "created_at", "delivered_at",
        },
        "vip_health_check_audit_log": {
            "audit_id", "case_id", "actor_type", "actor_id", "from_status",
            "to_status", "reason", "created_at",
        },
        "dietitian_coaching_orders": {
            "order_id", "user_id", "case_id", "product_type", "operation_key",
            "status", "quoted_amount", "requested_at", "payment_reported_at",
            "confirmed_by", "confirmed_at", "starts_at", "ends_at", "updated_at",
        },
    }
    def canonical_foreign_key(
        child_columns: tuple[str, ...],
        parent_table: str,
        parent_columns: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str, tuple[str, ...], str, str, str]:
        return (
            child_columns, parent_table, parent_columns,
            "NO ACTION", "NO ACTION", "NONE",
        )

    required_foreign_keys = {
        "vip_health_check_valid_days": {
            canonical_foreign_key(("case_id",), "vip_health_check_cases", ("case_id",)),
        },
        "vip_health_check_source_refs": {
            canonical_foreign_key(("case_id",), "vip_health_check_cases", ("case_id",)),
        },
        "vip_health_check_reviews": {
            canonical_foreign_key(("case_id",), "vip_health_check_cases", ("case_id",)),
        },
        "vip_health_check_reports": {
            canonical_foreign_key(("case_id",), "vip_health_check_cases", ("case_id",)),
            canonical_foreign_key(("review_id",), "vip_health_check_reviews", ("review_id",)),
            canonical_foreign_key(
                ("case_id", "review_id"),
                "vip_health_check_reviews",
                ("case_id", "review_id"),
            ),
        },
        "vip_health_check_deliveries": {
            canonical_foreign_key(("report_id",), "vip_health_check_reports", ("report_id",)),
        },
        "vip_health_check_audit_log": {
            canonical_foreign_key(("case_id",), "vip_health_check_cases", ("case_id",)),
        },
        "dietitian_coaching_orders": {
            canonical_foreign_key(("case_id",), "vip_health_check_cases", ("case_id",)),
        },
    }
    for table_name, expected_columns in required_columns.items():
        actual_columns = {
            row[1] for row in conn.execute(f"PRAGMA table_xinfo({table_name})")
        }
        missing_columns = expected_columns - actual_columns
        unexpected_columns = actual_columns - expected_columns
        foreign_key_groups: dict[
            int, tuple[str, str, str, str, list[tuple[int, str, str]]]
        ] = {}
        for row in conn.execute(f"PRAGMA foreign_key_list({table_name})"):
            group = foreign_key_groups.setdefault(
                row[0], (row[2], row[5], row[6], row[7], [])
            )
            group[4].append((row[1], row[3], row[4]))
        actual_foreign_keys = {
            (
                tuple(item[1] for item in sorted(columns)),
                referenced_table,
                tuple(item[2] for item in sorted(columns)),
                on_update,
                on_delete,
                match,
            )
            for referenced_table, on_update, on_delete, match, columns
            in foreign_key_groups.values()
        }
        expected_foreign_keys = required_foreign_keys.get(table_name, set())
        if (
            missing_columns
            or unexpected_columns
            or actual_foreign_keys != expected_foreign_keys
        ):
            raise sqlite3.IntegrityError(
                f"unsupported {table_name} schema: "
                f"missing_columns={sorted(missing_columns)!r}, "
                f"unexpected_columns={sorted(unexpected_columns)!r}, "
                f"foreign_keys={sorted(actual_foreign_keys)!r}"
            )

    required_primary_keys = {
        "vip_health_check_activation_events": ("activation_event_key",),
        "vip_health_check_cases": ("case_id",),
        "vip_health_check_valid_days": ("case_id", "local_date"),
        "vip_health_check_source_refs": ("case_id", "food_log_id"),
        "vip_health_check_reviews": ("review_id",),
        "vip_health_check_reports": ("report_id",),
        "vip_health_check_deliveries": ("delivery_id",),
        "vip_health_check_audit_log": ("audit_id",),
        "dietitian_coaching_orders": ("order_id",),
    }
    required_unique_sets = {
        "vip_health_check_cases": {
            ("user_id", "benefit_key"), ("activation_event_key",),
        },
        "vip_health_check_reviews": {
            ("case_id", "review_version"), ("case_id", "review_id"),
        },
        "vip_health_check_reports": {
            ("review_id",), ("case_id", "report_kind", "report_version"),
        },
        "vip_health_check_deliveries": {("delivery_key",)},
        "dietitian_coaching_orders": {("operation_key",)},
    }
    required_check_expressions = {
        "vip_health_check_activation_events": (
            _normalize_schema_sql(
                "activation_type IN ('lifetime_first','historical_existing','renewal')"
            ),
        ),
        "vip_health_check_cases": tuple(sorted((
            _normalize_schema_sql("benefit_key='first_vip_baseline_check'"),
            _normalize_schema_sql(
                """status IN ('collecting','ready_for_review','needs_more_info',
                    'approved_pending_delivery','delivery_failed','delivered',
                    'expired','cancelled')"""
            ),
            _normalize_schema_sql("valid_day_count BETWEEN 0 AND 7"),
        ))),
        "vip_health_check_valid_days": (),
        "vip_health_check_source_refs": (),
        "vip_health_check_reviews": (
            _normalize_schema_sql("status IN ('draft','approved','superseded')"),
        ),
        "vip_health_check_reports": (
            _normalize_schema_sql("report_kind='baseline_3day'"),
        ),
        "vip_health_check_deliveries": (
            _normalize_schema_sql("status IN ('pending','failed','delivered')"),
        ),
        "vip_health_check_audit_log": (),
        "dietitian_coaching_orders": tuple(sorted((
            _normalize_schema_sql("product_type='dietitian_coaching_4w'"),
            _normalize_schema_sql(
                """status IN (
                    'payment_pending','payment_reported','coaching_active','coaching_paused',
                    'coaching_completed','coaching_refunded','coaching_cancelled',
                    'payment_rejected')"""
            ),
        ))),
    }
    integer_columns = {
        ("vip_health_check_cases", "valid_day_count"),
        ("vip_health_check_valid_days", "qualifying_meal_count"),
        ("vip_health_check_source_refs", "food_log_version"),
        ("vip_health_check_reviews", "review_version"),
        ("vip_health_check_reports", "report_version"),
        ("vip_health_check_deliveries", "attempts"),
        ("vip_health_check_audit_log", "audit_id"),
        ("dietitian_coaching_orders", "quoted_amount"),
    }
    nullable_columns = {
        ("vip_health_check_audit_log", "audit_id"),
        ("dietitian_coaching_orders", "quoted_amount"),
    }
    expected_defaults = {
        ("vip_health_check_activation_events", "prior_usage_status"): "''",
        ("vip_health_check_activation_events", "prior_expiry_date"): "''",
        ("vip_health_check_cases", "status"): "'collecting'",
        ("vip_health_check_cases", "valid_day_count"): "0",
        ("vip_health_check_cases", "source_manifest_hash"): "''",
        ("vip_health_check_cases", "submitted_at"): "''",
        ("vip_health_check_cases", "report_published_at"): "''",
        ("vip_health_check_valid_days", "qualifying_meal_count"): "0",
        ("vip_health_check_source_refs", "included_reason"): "''",
        ("vip_health_check_reviews", "status"): "'draft'",
        ("vip_health_check_reviews", "ai_observations_json"): "'{}'",
        ("vip_health_check_reviews", "review_json"): "'{}'",
        ("vip_health_check_reviews", "suggested_values_json"): "'{}'",
        ("vip_health_check_reviews", "limitations"): "''",
        ("vip_health_check_reviews", "approved_by"): "''",
        ("vip_health_check_reviews", "approved_at"): "''",
        ("vip_health_check_reports", "report_kind"): "'baseline_3day'",
        ("vip_health_check_deliveries", "status"): "'pending'",
        ("vip_health_check_deliveries", "attempts"): "0",
        ("vip_health_check_deliveries", "last_error"): "''",
        ("vip_health_check_deliveries", "delivered_at"): "''",
        ("vip_health_check_audit_log", "actor_id"): "''",
        ("vip_health_check_audit_log", "from_status"): "''",
        ("vip_health_check_audit_log", "reason"): "''",
        ("dietitian_coaching_orders", "product_type"): "'dietitian_coaching_4w'",
        ("dietitian_coaching_orders", "status"): "'payment_pending'",
        ("dietitian_coaching_orders", "payment_reported_at"): "''",
        ("dietitian_coaching_orders", "confirmed_by"): "''",
        ("dietitian_coaching_orders", "confirmed_at"): "''",
        ("dietitian_coaching_orders", "starts_at"): "''",
        ("dietitian_coaching_orders", "ends_at"): "''",
    }
    for table_name, expected_primary_key in required_primary_keys.items():
        table_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        table_info_by_name = {row[1]: row for row in table_info}
        actual_primary_key = tuple(
            row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]
        )
        metadata_mismatches = []
        for column_name in required_columns[table_name]:
            row = table_info_by_name[column_name]
            key = (table_name, column_name)
            expected_type = "INTEGER" if key in integer_columns else "TEXT"
            expected_notnull = 0 if key in nullable_columns else 1
            expected_default = expected_defaults.get(key)
            expected_pk_ordinal = (
                expected_primary_key.index(column_name) + 1
                if column_name in expected_primary_key
                else 0
            )
            actual_metadata = (str(row[2]).upper(), row[3], row[4], row[5])
            expected_metadata = (
                expected_type, expected_notnull, expected_default, expected_pk_ordinal,
            )
            if actual_metadata != expected_metadata:
                metadata_mismatches.append(
                    (column_name, actual_metadata, expected_metadata)
                )
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        table_sql = table_sql_row[0] if table_sql_row else ""
        actual_check_expressions = _extract_check_expressions(table_sql)
        check_expressions_mismatch = (
            actual_check_expressions != required_check_expressions[table_name]
        )
        forbidden_schema_words = set(_unquoted_schema_words(table_sql)) & {
            "collate", "conflict", "deferrable", "initially", "match",
            "strict", "without",
        }
        unique_fingerprints = {
            tuple(
                (item[1], item[2], bool(item[3]), str(item[4]).upper(), bool(item[5]))
                for item in conn.execute(f"PRAGMA index_xinfo({index_row[1]})")
                if item[5]
            )
            for index_row in conn.execute(f"PRAGMA index_list({table_name})")
            if index_row[2] and index_row[3] == "u" and not index_row[4]
        }
        expected_unique_fingerprints = {
            tuple(
                (table_info_by_name[column][0], column, False, "BINARY", True)
                for column in columns
            )
            for columns in required_unique_sets.get(table_name, set())
        }
        unique_fingerprints_mismatch = (
            unique_fingerprints != expected_unique_fingerprints
        )
        if (
            actual_primary_key != expected_primary_key
            or metadata_mismatches
            or check_expressions_mismatch
            or forbidden_schema_words
            or unique_fingerprints_mismatch
        ):
            raise sqlite3.IntegrityError(
                f"unsupported {table_name} constraints: "
                f"primary_key={actual_primary_key!r}, "
                f"metadata_mismatches={metadata_mismatches!r}, "
                f"check_expressions={actual_check_expressions!r}, "
                f"forbidden_schema_words={sorted(forbidden_schema_words)!r}, "
                f"unique_fingerprints={sorted(unique_fingerprints)!r}"
            )

    required_indexes = {
        "idx_vip_health_check_activation_user_time": (
            "vip_health_check_activation_events", ("user_id", "occurred_at"), False,
        ),
        "idx_vip_health_check_cases_status": (
            "vip_health_check_cases", ("status", "updated_at"), False,
        ),
        "idx_vip_health_check_source_date": (
            "vip_health_check_source_refs", ("case_id", "local_date"), False,
        ),
        "idx_vip_health_check_reviews_case_review": (
            "vip_health_check_reviews", ("case_id", "review_id"), True,
        ),
        "idx_vip_health_check_deliveries_status": (
            "vip_health_check_deliveries", ("status", "created_at"), False,
        ),
    }
    for index_name, (
        expected_table,
        expected_columns,
        expected_unique,
    ) in required_indexes.items():
        master_row = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        index_row = next(
            (
                row
                for row in conn.execute(f"PRAGMA index_list({expected_table})")
                if row[1] == index_name
            ),
            None,
        )
        column_ids = {
            row[1]: row[0]
            for row in conn.execute(f"PRAGMA table_info({expected_table})")
        }
        actual_key_terms = tuple(
            (row[1], row[2], bool(row[3]), str(row[4]).upper(), bool(row[5]))
            for row in conn.execute(f"PRAGMA index_xinfo({index_name})")
            if row[5]
        )
        expected_key_terms = tuple(
            (column_ids[column], column, False, "BINARY", True)
            for column in expected_columns
        )
        if (
            not master_row
            or master_row[0] != expected_table
            or not index_row
            or bool(index_row[2]) is not expected_unique
            or index_row[3] != "c"
            or bool(index_row[4])
            or actual_key_terms != expected_key_terms
        ):
            raise sqlite3.IntegrityError(
                f"unsupported VIP health-check index: {index_name}"
            )

    required_triggers = {
        "vip_health_check_reports_no_update": _normalize_schema_sql(
            """CREATE TRIGGER vip_health_check_reports_no_update
               BEFORE UPDATE ON vip_health_check_reports
               BEGIN
                   SELECT RAISE(ABORT, 'published health-check reports are immutable');
               END"""
        ),
        "vip_health_check_reports_no_delete": _normalize_schema_sql(
            """CREATE TRIGGER vip_health_check_reports_no_delete
               BEFORE DELETE ON vip_health_check_reports
               BEGIN
                   SELECT RAISE(ABORT, 'published health-check reports are immutable');
               END"""
        ),
    }
    for trigger_name, expected_sql in required_triggers.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()
        normalized = _normalize_schema_sql(row[0] if row else "").rstrip(";")
        normalized = normalized.replace(
            "createtriggerifnotexists", "createtrigger", 1
        )
        if normalized != expected_sql:
            raise sqlite3.IntegrityError(
                f"unsupported VIP health-check trigger: {trigger_name}"
            )

    _verify_vip_health_check_check_constraints(conn)


def _ensure_vip_health_check_schema(conn: sqlite3.Connection) -> None:
    """建立 schema；必須由 ensure_vip_health_check_schema 的 SAVEPOINT 呼叫。"""
    _execute_sql_script_without_implicit_commit(
        conn,
        """
        CREATE TABLE IF NOT EXISTS vip_health_check_activation_events (
            activation_event_key TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            activation_type TEXT NOT NULL CHECK(activation_type IN (
                'lifetime_first','historical_existing','renewal'
            )),
            prior_usage_status TEXT NOT NULL DEFAULT '',
            prior_expiry_date TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vip_health_check_cases (
            case_id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            benefit_key TEXT NOT NULL CHECK (benefit_key='first_vip_baseline_check'),
            first_vip_activation_id TEXT NOT NULL,
            activation_event_key TEXT NOT NULL,
            window_started_at TEXT NOT NULL,
            window_ends_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'collecting' CHECK(status IN (
                'collecting','ready_for_review','needs_more_info',
                'approved_pending_delivery','delivery_failed','delivered',
                'expired','cancelled'
            )),
            valid_day_count INTEGER NOT NULL DEFAULT 0 CHECK (valid_day_count BETWEEN 0 AND 7),
            source_manifest_hash TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL DEFAULT '',
            report_published_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, benefit_key),
            UNIQUE(activation_event_key)
        );

        CREATE TABLE IF NOT EXISTS vip_health_check_valid_days (
            case_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            qualifying_meal_count INTEGER NOT NULL DEFAULT 0,
            completeness_status TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            PRIMARY KEY(case_id, local_date),
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
        );

        CREATE TABLE IF NOT EXISTS vip_health_check_source_refs (
            case_id TEXT NOT NULL,
            food_log_id TEXT NOT NULL,
            food_log_version INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            included_reason TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(case_id, food_log_id),
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
        );

        CREATE TABLE IF NOT EXISTS vip_health_check_reviews (
            review_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL,
            review_version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','superseded')),
            ai_observations_json TEXT NOT NULL DEFAULT '{}',
            review_json TEXT NOT NULL DEFAULT '{}',
            suggested_values_json TEXT NOT NULL DEFAULT '{}',
            limitations TEXT NOT NULL DEFAULT '',
            source_manifest_hash TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(case_id, review_version),
            UNIQUE(case_id, review_id),
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
        );

        CREATE TABLE IF NOT EXISTS vip_health_check_reports (
            report_id TEXT PRIMARY KEY NOT NULL,
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
            FOREIGN KEY(case_id, review_id)
                REFERENCES vip_health_check_reviews(case_id, review_id)
        );

        CREATE TRIGGER IF NOT EXISTS vip_health_check_reports_no_update
        BEFORE UPDATE ON vip_health_check_reports
        BEGIN
            SELECT RAISE(ABORT, 'published health-check reports are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS vip_health_check_reports_no_delete
        BEFORE DELETE ON vip_health_check_reports
        BEGIN
            SELECT RAISE(ABORT, 'published health-check reports are immutable');
        END;

        CREATE TABLE IF NOT EXISTS vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY NOT NULL,
            report_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','failed','delivered')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        );

        CREATE TABLE IF NOT EXISTS vip_health_check_audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
        );

        CREATE TABLE IF NOT EXISTS dietitian_coaching_orders (
            order_id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            product_type TEXT NOT NULL DEFAULT 'dietitian_coaching_4w'
                CHECK(product_type='dietitian_coaching_4w'),
            operation_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'payment_pending' CHECK(status IN (
                'payment_pending','payment_reported','coaching_active','coaching_paused',
                'coaching_completed','coaching_refunded','coaching_cancelled','payment_rejected'
            )),
            quoted_amount INTEGER,
            requested_at TEXT NOT NULL,
            payment_reported_at TEXT NOT NULL DEFAULT '',
            confirmed_by TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT NOT NULL DEFAULT '',
            starts_at TEXT NOT NULL DEFAULT '',
            ends_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
        );

        CREATE INDEX IF NOT EXISTS idx_vip_health_check_activation_user_time
            ON vip_health_check_activation_events(user_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_vip_health_check_cases_status
            ON vip_health_check_cases(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_vip_health_check_source_date
            ON vip_health_check_source_refs(case_id, local_date);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vip_health_check_reviews_case_review
            ON vip_health_check_reviews(case_id, review_id);
        CREATE INDEX IF NOT EXISTS idx_vip_health_check_deliveries_status
            ON vip_health_check_deliveries(status, created_at);
        """
    )
    report_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(vip_health_check_reports)")
    }
    report_schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vip_health_check_reports'"
    ).fetchone()
    normalized_report_schema = "".join(
        str(report_schema_row[0] or "").lower().split()
    )
    report_foreign_keys = {
        (row[3], row[2], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(vip_health_check_reports)")
    }
    required_report_foreign_keys = {
        ("case_id", "vip_health_check_cases", "case_id"),
        ("review_id", "vip_health_check_reviews", "review_id"),
        ("case_id", "vip_health_check_reviews", "case_id"),
    }
    if (
        "review_id" not in report_columns
        or not required_report_foreign_keys.issubset(report_foreign_keys)
        or "check(report_kind='baseline_3day')" not in normalized_report_schema
    ):
        _rebuild_legacy_reports_table(conn, report_columns)

    delivery_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(vip_health_check_deliveries)")
    }
    required_delivery_columns = {
        "delivery_id", "report_id", "user_id", "delivery_key", "status",
        "attempts", "last_error", "created_at", "delivered_at",
    }
    delivery_foreign_keys = {
        (row[3], row[2], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(vip_health_check_deliveries)")
    }
    if (
        not required_delivery_columns.issubset(delivery_columns)
        or ("report_id", "vip_health_check_reports", "report_id")
        not in delivery_foreign_keys
    ):
        raise sqlite3.IntegrityError(
            "vip_health_check_deliveries schema is incomplete or unsupported"
        )

    case_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(vip_health_check_cases)")
    }
    if "source_manifest_hash" not in case_columns:
        conn.execute(
            "ALTER TABLE vip_health_check_cases "
            "ADD COLUMN source_manifest_hash TEXT NOT NULL DEFAULT ''"
        )
    _verify_vip_health_check_schema_shape(conn)
    _verify_vip_health_check_foreign_keys(conn)


def _iso_seconds(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("activated_at 必須包含時區")
    return value.isoformat(timespec="seconds")


def record_vip_activation_event(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    activation_event_key: str,
    activated_at: datetime,
    prior_usage_exists: bool,
    prior_usage_status: str = "",
    prior_expiry_date: str = "",
) -> str:
    """Persist an auditable redemption classification before usage is overwritten."""
    require_vip_health_check_connection(conn)
    user_id = str(user_id or "").strip()
    activation_event_key = str(activation_event_key or "").strip()
    if not user_id or not activation_event_key:
        raise ValueError("VIP activation provenance 缺少必要識別碼")

    has_recorded_history = conn.execute(
        """SELECT 1 FROM vip_health_check_activation_events
           WHERE user_id=? LIMIT 1""",
        (user_id,),
    ).fetchone() is not None
    has_health_check_case = conn.execute(
        """SELECT 1 FROM vip_health_check_cases
           WHERE user_id=? LIMIT 1""",
        (user_id,),
    ).fetchone() is not None
    if has_recorded_history or has_health_check_case:
        activation_type = "renewal"
    elif prior_usage_exists:
        activation_type = "historical_existing"
    else:
        activation_type = "lifetime_first"

    conn.execute(
        """INSERT INTO vip_health_check_activation_events
           (activation_event_key,user_id,activation_type,prior_usage_status,
            prior_expiry_date,occurred_at)
           VALUES (?,?,?,?,?,?)""",
        (
            activation_event_key,
            user_id,
            activation_type,
            str(prior_usage_status or "").strip(),
            str(prior_expiry_date or "").strip(),
            _iso_seconds(activated_at),
        ),
    )
    return activation_type


def create_first_vip_health_check_case(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    first_vip_activation_id: str,
    activation_event_key: str,
    activated_at: datetime,
) -> dict[str, object]:
    """建立一次性首次 VIP 健檢；續期會回傳原案件且不重設窗口。"""
    require_vip_health_check_connection(conn)
    user_id = str(user_id or "").strip()
    first_vip_activation_id = str(first_vip_activation_id or "").strip()
    activation_event_key = str(activation_event_key or "").strip()
    if not user_id or not first_vip_activation_id or not activation_event_key:
        raise ValueError("首次 VIP 健檢缺少必要識別碼")

    started_at = _iso_seconds(activated_at)
    ends_at = _iso_seconds(activated_at + timedelta(days=7))
    existing = conn.execute(
        """SELECT case_id,window_started_at,window_ends_at,status
           FROM vip_health_check_cases WHERE user_id=? AND benefit_key=?""",
        (user_id, BENEFIT_KEY),
    ).fetchone()
    if existing:
        return {
            "case_id": existing[0],
            "window_started_at": existing[1],
            "window_ends_at": existing[2],
            "status": existing[3],
            "created": False,
        }

    case_id = "vhc_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT create_first_vip_health_check_case")
    try:
        conn.execute(
            """INSERT INTO vip_health_check_cases
               (case_id,user_id,benefit_key,first_vip_activation_id,activation_event_key,
                window_started_at,window_ends_at,status,valid_day_count,source_manifest_hash,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'collecting',0,'',?,?)""",
            (
                case_id,
                user_id,
                BENEFIT_KEY,
                first_vip_activation_id,
                activation_event_key,
                started_at,
                ends_at,
                started_at,
                started_at,
            ),
        )
        conn.execute(
            """INSERT INTO vip_health_check_audit_log
               (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
               VALUES (?,'system','vip_redemption','','collecting','first_vip_activation',?)""",
            (case_id, started_at),
        )
        conn.execute("RELEASE SAVEPOINT create_first_vip_health_check_case")
        created = True
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT create_first_vip_health_check_case")
        conn.execute("RELEASE SAVEPOINT create_first_vip_health_check_case")
        existing = conn.execute(
            """SELECT case_id,window_started_at,window_ends_at,status
               FROM vip_health_check_cases WHERE user_id=? AND benefit_key=?""",
            (user_id, BENEFIT_KEY),
        ).fetchone()
        if not existing:
            raise
        return {
            "case_id": existing[0],
            "window_started_at": existing[1],
            "window_ends_at": existing[2],
            "status": existing[3],
            "created": False,
        }

    return {
        "case_id": case_id,
        "window_started_at": started_at,
        "window_ends_at": ends_at,
        "status": "collecting",
        "created": created,
    }


_TAIPEI = ZoneInfo("Asia/Taipei")
_REFRESHABLE_CASE_STATUSES = {"collecting", "ready_for_review", "needs_more_info"}


def _parse_ledger_time(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"food_logs.consumed_at 格式錯誤: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_TAIPEI)
    return parsed.astimezone(_TAIPEI)


def _canonical_json_text(value: str) -> str:
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(value or "")


def refresh_case_source_manifest(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    evaluated_at: datetime,
    minimum_meals_per_day: int = 2,
    rule_version: str = "draft-confirmed-meals-v1",
) -> dict[str, object]:
    """依 canonical food_logs 重建收集期來源與每日資格。

    `minimum_meals_per_day` 是可替換的暫定規則；目前預設 2 餐，待營養師
    驗收後可更換 rule version。函式刻意不查 `planned_meal_checks`，避免已轉成
    food_log 的一日樂食餐點被投影表重複計算。
    """
    if not isinstance(minimum_meals_per_day, int) or not 1 <= minimum_meals_per_day <= 10:
        raise ValueError("minimum_meals_per_day 必須介於 1～10")
    rule_version = str(rule_version or "").strip()
    if not rule_version:
        raise ValueError("rule_version 不可空白")
    evaluated_text = _iso_seconds(evaluated_at)

    conn.execute("SAVEPOINT refresh_case_source_manifest")
    try:
        case = conn.execute(
            """SELECT user_id,window_started_at,window_ends_at,status
               FROM vip_health_check_cases WHERE case_id=?""",
            (str(case_id or "").strip(),),
        ).fetchone()
        if not case:
            raise ValueError("找不到健檢案件")
        user_id, window_start_text, window_end_text, current_status = case
        if current_status not in _REFRESHABLE_CASE_STATUSES:
            raise ValueError("案件已進入不可重建來源的狀態")

        window_start = _parse_ledger_time(window_start_text)
        window_end = _parse_ledger_time(window_end_text)
        rows = conn.execute(
            """SELECT log_id,consumed_at,meal_slot,nutrition_snapshot_json,version
               FROM food_logs
               WHERE user_id=? AND confirmation_status='confirmed'
                 AND COALESCE(deleted_at,'')=''""",
            (user_id,),
        ).fetchall()

        included: list[dict[str, object]] = []
        by_date: dict[str, list[dict[str, object]]] = defaultdict(list)
        for log_id, consumed_at, meal_slot, nutrition_snapshot_json, version in rows:
            local_time = _parse_ledger_time(consumed_at)
            if not window_start <= local_time < window_end:
                continue
            local_date = local_time.date().isoformat()
            version = int(version or 1)
            source_material = (
                f"{log_id}:{version}:"
                f"{_canonical_json_text(nutrition_snapshot_json)}"
            )
            item = {
                "food_log_id": log_id,
                "version": version,
                "local_date": local_date,
                "meal_slot": str(meal_slot or ""),
                "source_hash": hashlib.sha256(source_material.encode("utf-8")).hexdigest(),
            }
            included.append(item)
            by_date[local_date].append(item)

        conn.execute("DELETE FROM vip_health_check_source_refs WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM vip_health_check_valid_days WHERE case_id=?", (case_id,))
        for item in sorted(included, key=lambda row: str(row["food_log_id"])):
            conn.execute(
                """INSERT INTO vip_health_check_source_refs
                   (case_id,food_log_id,food_log_version,local_date,included_reason,
                    source_hash,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    case_id,
                    item["food_log_id"],
                    item["version"],
                    item["local_date"],
                    "confirmed_canonical_food_log_in_activation_window",
                    item["source_hash"],
                    evaluated_text,
                ),
            )

        valid_day_count = 0
        for local_date, items in sorted(by_date.items()):
            meal_count = len(
                {str(item["meal_slot"] or "unspecified").strip() or "unspecified" for item in items}
            )
            completeness = "qualified" if meal_count >= minimum_meals_per_day else "incomplete"
            valid_day_count += completeness == "qualified"
            conn.execute(
                """INSERT INTO vip_health_check_valid_days
                   (case_id,local_date,rule_version,qualifying_meal_count,
                    completeness_status,evaluated_at)
                   VALUES (?,?,?,?,?,?)""",
                (case_id, local_date, rule_version, meal_count, completeness, evaluated_text),
            )

        manifest_lines = [
            f"{item['food_log_id']}:{item['version']}:{item['source_hash']}"
            for item in sorted(included, key=lambda row: str(row["food_log_id"]))
        ]
        manifest_lines.extend(
            "day:"
            f"{local_date}:"
            f"{len({str(item['meal_slot'] or 'unspecified').strip() or 'unspecified' for item in items})}:"
            f"{rule_version}"
            for local_date, items in sorted(by_date.items())
        )
        manifest_hash = hashlib.sha256("\n".join(manifest_lines).encode("utf-8")).hexdigest()

        next_status = "ready_for_review" if valid_day_count >= 3 else "collecting"
        conn.execute(
            """UPDATE vip_health_check_cases
               SET status=?,valid_day_count=?,source_manifest_hash=?,updated_at=? WHERE case_id=?""",
            (next_status, valid_day_count, manifest_hash, evaluated_text, case_id),
        )
        if next_status != current_status:
            conn.execute(
                """INSERT INTO vip_health_check_audit_log
                   (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
                   VALUES (?,'system','ledger_refresh',?,?,?,?)""",
                (
                    case_id,
                    current_status,
                    next_status,
                    f"{valid_day_count}_qualified_days:{rule_version}",
                    evaluated_text,
                ),
            )
        conn.execute("RELEASE SAVEPOINT refresh_case_source_manifest")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT refresh_case_source_manifest")
        conn.execute("RELEASE SAVEPOINT refresh_case_source_manifest")
        raise

    return {
        "case_id": case_id,
        "status": next_status,
        "source_count": len(included),
        "valid_day_count": valid_day_count,
        "source_manifest_hash": manifest_hash,
        "rule_version": rule_version,
    }


def _json_object(value: Mapping[str, object], field: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} 必須是物件")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_health_check_review(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    ai_observations: Mapping[str, object],
    review: Mapping[str, object],
    suggested_values: Mapping[str, object],
    limitations: str,
    source_manifest_hash: str,
    saved_at: datetime,
) -> dict[str, object]:
    """儲存可編輯審核草稿；發布後不可再修改此案件來源。"""
    saved_text = _iso_seconds(saved_at)
    source_manifest_hash = str(source_manifest_hash or "").strip()
    if not source_manifest_hash:
        raise ValueError("source_manifest_hash 不可空白")
    case = conn.execute(
        "SELECT status,source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
        (case_id,),
    ).fetchone()
    if not case:
        raise ValueError("找不到健檢案件")
    if case[0] not in {"ready_for_review", "needs_more_info"}:
        raise ValueError("案件尚未進入可審核狀態")
    if source_manifest_hash != str(case[1] or ""):
        raise ValueError("來源資料已更新，請重新載入")
    ai_json = _json_object(ai_observations, "ai_observations")
    review_json = _json_object(review, "review")
    suggested_json = _json_object(suggested_values, "suggested_values")
    limitations_text = str(limitations or "").strip()
    conn.execute("SAVEPOINT save_health_check_review")
    try:
        fresh_case = conn.execute(
            "SELECT status,source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        if not fresh_case or fresh_case[0] not in {"ready_for_review", "needs_more_info"}:
            raise ValueError("案件尚未進入可審核狀態")
        if source_manifest_hash != str(fresh_case[1] or ""):
            raise ValueError("來源資料已更新，請重新載入")
        latest = conn.execute(
            """SELECT review_id,review_version,status,ai_observations_json,review_json,
                      suggested_values_json,limitations,source_manifest_hash
               FROM vip_health_check_reviews
               WHERE case_id=? ORDER BY review_version DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        if latest and latest[2] == "draft" and tuple(latest[3:]) == (
            ai_json,
            review_json,
            suggested_json,
            limitations_text,
            source_manifest_hash,
        ):
            conn.execute("RELEASE SAVEPOINT save_health_check_review")
            return {
                "review_id": latest[0],
                "review_version": int(latest[1]),
                "status": "draft",
                "created": False,
            }
        version = int(
            conn.execute(
                "SELECT COALESCE(MAX(review_version),0)+1 FROM vip_health_check_reviews WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]
        )
        review_id = "vhcr_" + uuid.uuid4().hex
        conn.execute(
            """INSERT INTO vip_health_check_reviews
               (review_id,case_id,review_version,status,ai_observations_json,review_json,
                suggested_values_json,limitations,source_manifest_hash,approved_by,
                approved_at,created_at,updated_at)
               VALUES (?,?,?,'draft',?,?,?,?,?,'','',?,?)""",
            (
                review_id,
                case_id,
                version,
                ai_json,
                review_json,
                suggested_json,
                limitations_text,
                source_manifest_hash,
                saved_text,
                saved_text,
            ),
        )
        conn.execute("RELEASE SAVEPOINT save_health_check_review")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT save_health_check_review")
        conn.execute("RELEASE SAVEPOINT save_health_check_review")
        raise
    return {
        "review_id": review_id,
        "review_version": version,
        "status": "draft",
        "created": True,
    }


def _existing_approved_report(
    conn: sqlite3.Connection, *, case_id: str, review_id: str
) -> dict[str, object] | None:
    row = conn.execute(
        """SELECT r.report_id,d.delivery_key,d.status
           FROM vip_health_check_reports r
           JOIN vip_health_check_deliveries d ON d.report_id=r.report_id
           JOIN vip_health_check_reviews v ON v.review_id=r.review_id
           WHERE r.case_id=? AND r.review_id=? AND v.status='approved'
           ORDER BY r.report_version DESC LIMIT 1""",
        (case_id, review_id),
    ).fetchone()
    if not row:
        return None
    return {
        "report_id": row[0],
        "delivery_key": row[1],
        "delivery_status": row[2],
        "created": False,
    }


def approve_health_check_review(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    review_id: str,
    expected_version: int,
    approved_by: str,
    approved_at: datetime,
    report: Mapping[str, object],
) -> dict[str, object]:
    """以樂觀鎖核准，並原子建立不可變報告及唯一投遞意圖。"""
    approved_by = str(approved_by or "").strip()
    if not approved_by:
        raise ValueError("approved_by 不可空白")
    approved_text = _iso_seconds(approved_at)
    report_values = dict(report) if isinstance(report, Mapping) else {}
    for field in ("good", "priority", "next_7_days", "limitations"):
        if not str(report_values.get(field) or "").strip():
            raise ValueError(f"報告缺少 {field}")
    report_json = _json_object(report_values, "report")

    existing = _existing_approved_report(conn, case_id=case_id, review_id=review_id)
    if existing:
        return existing
    review_row = conn.execute(
        """SELECT review_version,status,source_manifest_hash
           FROM vip_health_check_reviews WHERE review_id=? AND case_id=?""",
        (review_id, case_id),
    ).fetchone()
    if not review_row:
        raise ValueError("找不到審核草稿")
    latest_version = int(
        conn.execute(
            "SELECT COALESCE(MAX(review_version),0) FROM vip_health_check_reviews WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
    )
    if int(review_row[0]) != int(expected_version):
        raise ValueError("審核版本已更新，請重新載入")
    if int(expected_version) != latest_version:
        raise ValueError("只能核准最新版本，請重新載入")
    if review_row[1] != "draft":
        raise ValueError("審核草稿狀態不可核准")
    case_row = conn.execute(
        """SELECT user_id,status,source_manifest_hash
           FROM vip_health_check_cases WHERE case_id=?""",
        (case_id,),
    ).fetchone()
    if not case_row or case_row[1] not in {"ready_for_review", "needs_more_info"}:
        raise ValueError("案件狀態不可核准")
    if str(review_row[2] or "") != str(case_row[2] or ""):
        raise ValueError("來源資料已更新，請重新產生審核草稿")

    conn.execute("SAVEPOINT approve_health_check_report")
    try:
        fresh_case = conn.execute(
            """SELECT user_id,status,source_manifest_hash
               FROM vip_health_check_cases WHERE case_id=?""",
            (case_id,),
        ).fetchone()
        if not fresh_case or fresh_case[1] not in {"ready_for_review", "needs_more_info"}:
            raise ValueError("案件狀態不可核准")
        if str(review_row[2] or "") != str(fresh_case[2] or ""):
            raise ValueError("來源資料已更新，請重新產生審核草稿")
        fresh_latest_version = int(
            conn.execute(
                "SELECT COALESCE(MAX(review_version),0) FROM vip_health_check_reviews WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]
        )
        if int(expected_version) != fresh_latest_version:
            raise ValueError("只能核准最新版本，請重新載入")
        changed = conn.execute(
            """UPDATE vip_health_check_reviews
               SET status='approved',approved_by=?,approved_at=?,updated_at=?
               WHERE review_id=? AND case_id=? AND review_version=? AND status='draft'""",
            (approved_by, approved_text, approved_text, review_id, case_id, expected_version),
        )
        if changed.rowcount != 1:
            raise ValueError("審核版本已更新，請重新載入")
        report_version = int(
            conn.execute(
                """SELECT COALESCE(MAX(report_version),0)+1
                   FROM vip_health_check_reports WHERE case_id=? AND report_kind='baseline_3day'""",
                (case_id,),
            ).fetchone()[0]
        )
        report_id = "vhcp_" + uuid.uuid4().hex
        delivery_id = "vhcd_" + uuid.uuid4().hex
        delivery_key = f"vip-health-check:{case_id}:baseline_3day:v{report_version}"
        conn.execute(
            """INSERT INTO vip_health_check_reports
               (report_id,case_id,review_id,report_kind,report_version,report_json,
                source_manifest_hash,published_by,published_at)
               VALUES (?,?,?,'baseline_3day',?,?,?,?,?)""",
            (
                report_id,
                case_id,
                review_id,
                report_version,
                report_json,
                review_row[2],
                approved_by,
                approved_text,
            ),
        )
        conn.execute(
            """INSERT INTO vip_health_check_deliveries
               (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
                created_at,delivered_at)
               VALUES (?,?,?,?,'pending',0,'',?,'')""",
            (delivery_id, report_id, case_row[0], delivery_key, approved_text),
        )
        conn.execute(
            """UPDATE vip_health_check_cases
               SET status='approved_pending_delivery',updated_at=? WHERE case_id=?""",
            (approved_text, case_id),
        )
        conn.execute(
            """INSERT INTO vip_health_check_audit_log
               (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
               VALUES (?,'dietitian',? ,?,'approved_pending_delivery','review_approved',?)""",
            (case_id, approved_by, case_row[1], approved_text),
        )
        conn.execute("RELEASE SAVEPOINT approve_health_check_report")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT approve_health_check_report")
        conn.execute("RELEASE SAVEPOINT approve_health_check_report")
        raise
    return {
        "report_id": report_id,
        "delivery_key": delivery_key,
        "delivery_status": "pending",
        "created": True,
    }


def record_health_check_delivery_attempt(
    conn: sqlite3.Connection,
    *,
    delivery_key: str,
    succeeded: bool,
    error: str,
    attempted_at: datetime,
) -> dict[str, object]:
    """記錄同一 delivery key 的結果；失敗重送永遠沿用原報告。"""
    attempted_text = _iso_seconds(attempted_at)
    row = conn.execute(
        """SELECT d.delivery_id,d.report_id,d.status,d.attempts,r.case_id,r.report_json
           FROM vip_health_check_deliveries d
           JOIN vip_health_check_reports r ON r.report_id=d.report_id
           WHERE d.delivery_key=?""",
        (str(delivery_key or "").strip(),),
    ).fetchone()
    if not row:
        raise ValueError("找不到報告投遞意圖")
    delivery_id, report_id, old_status, attempts, case_id, report_json = row
    if old_status == "delivered":
        return {
            "delivery_key": delivery_key,
            "report_id": report_id,
            "status": "delivered",
            "attempts": int(attempts),
            "report": json.loads(report_json),
        }
    requested_status = "delivered" if succeeded else "failed"
    requested_error = "" if succeeded else str(error or "")[:1000]
    conn.execute("SAVEPOINT record_health_check_delivery")
    try:
        conn.execute(
            """UPDATE vip_health_check_deliveries
               SET status=CASE WHEN status='delivered' THEN status ELSE ? END,
                   attempts=attempts+1,
                   last_error=CASE WHEN status='delivered' THEN last_error ELSE ? END,
                   delivered_at=CASE
                       WHEN status='delivered' THEN delivered_at
                       WHEN ?='delivered' THEN ?
                       ELSE ''
                   END
               WHERE delivery_id=?""",
            (
                requested_status,
                requested_error,
                requested_status,
                attempted_text,
                delivery_id,
            ),
        )
        final_delivery = conn.execute(
            "SELECT status,attempts FROM vip_health_check_deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        if not final_delivery:
            raise ValueError("找不到報告投遞意圖")
        final_status, final_attempts = final_delivery
        case_status = "delivered" if final_status == "delivered" else "delivery_failed"
        case_row = conn.execute(
            "SELECT status FROM vip_health_check_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        conn.execute(
            """UPDATE vip_health_check_cases
               SET status=CASE WHEN status='delivered' THEN status ELSE ? END,
                   report_published_at=CASE
                       WHEN status='delivered' THEN report_published_at
                       WHEN ?='delivered' AND report_published_at='' THEN ?
                       WHEN ?<>'delivered' THEN ''
                       ELSE report_published_at
                   END,
                   updated_at=?
               WHERE case_id=?""",
            (
                case_status,
                case_status,
                attempted_text,
                case_status,
                attempted_text,
                case_id,
            ),
        )
        effective_case_status = (
            case_row[0] if case_row and case_row[0] == "delivered" else case_status
        )
        reason = "delivery_succeeded" if succeeded else "delivery_failed"
        if not succeeded and final_status == "delivered":
            reason = "late_delivery_failure_ignored"
        if not succeeded and case_row and case_row[0] == "delivered":
            reason = "case_already_delivered_failure_ignored"
        conn.execute(
            """INSERT INTO vip_health_check_audit_log
               (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
               VALUES (?,'system','line_delivery',?,?,?,?)""",
            (
                case_id,
                case_row[0] if case_row else "",
                effective_case_status,
                reason,
                attempted_text,
            ),
        )
        conn.execute("RELEASE SAVEPOINT record_health_check_delivery")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT record_health_check_delivery")
        conn.execute("RELEASE SAVEPOINT record_health_check_delivery")
        raise
    return {
        "delivery_key": delivery_key,
        "report_id": report_id,
        "status": final_status,
        "attempts": int(final_attempts),
        "report": json.loads(report_json),
    }


def get_customer_health_check_state(
    conn: sqlite3.Connection, *, user_id: str
) -> dict[str, object] | None:
    """顧客唯讀投影；只有已成功投遞的報告才回傳內容。"""
    row = conn.execute(
        """SELECT case_id,status,valid_day_count,window_started_at,window_ends_at,
                  report_published_at
           FROM vip_health_check_cases
           WHERE user_id=? AND benefit_key=?""",
        (str(user_id or "").strip(), BENEFIT_KEY),
    ).fetchone()
    if not row:
        return None
    report = None
    if row[1] == "delivered":
        report_row = conn.execute(
            """SELECT r.report_json
               FROM vip_health_check_reports AS r
               WHERE r.case_id=?
                 AND r.report_kind='baseline_3day'
                 AND EXISTS (
                     SELECT 1
                     FROM vip_health_check_deliveries AS d
                     WHERE d.report_id=r.report_id
                       AND d.user_id=?
                       AND d.status='delivered'
                 )
               ORDER BY r.report_version DESC
               LIMIT 1""",
            (row[0], str(user_id or "").strip()),
        ).fetchone()
        if report_row:
            report = json.loads(report_row[0])
    return {
        "case_id": row[0],
        "status": row[1],
        "valid_day_count": int(row[2]),
        "window_started_at": row[3],
        "window_ends_at": row[4],
        "report_published_at": row[5],
        "report": report,
    }


def request_dietitian_coaching(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    case_id: str,
    operation_key: str,
    requested_at: datetime,
) -> dict[str, object]:
    """為既有 LINE user 建立人工匯款加購，不產生任何 VIP code。"""
    user_id = str(user_id or "").strip()
    operation_key = str(operation_key or "").strip()
    if not user_id or not operation_key:
        raise ValueError("陪跑申請缺少必要識別碼")
    existing = conn.execute(
        """SELECT order_id,user_id,case_id,status FROM dietitian_coaching_orders
           WHERE operation_key=?""",
        (operation_key,),
    ).fetchone()
    if existing:
        if existing[1] != user_id or existing[2] != case_id:
            raise ValueError("operation_key 已由其他申請使用")
        return {"order_id": existing[0], "status": existing[3], "created": False}
    case = conn.execute(
        "SELECT user_id,status FROM vip_health_check_cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if not case or case[0] != user_id:
        raise ValueError("健檢案件不屬於目前使用者")
    if case[1] != "delivered":
        raise ValueError("需先完成基準報告投遞")
    requested_text = _iso_seconds(requested_at)
    order_id = "vhco_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT request_dietitian_coaching")
    try:
        conn.execute(
            """INSERT INTO dietitian_coaching_orders
               (order_id,user_id,case_id,product_type,operation_key,status,quoted_amount,
                requested_at,payment_reported_at,confirmed_by,confirmed_at,starts_at,
                ends_at,updated_at)
               VALUES (?,?,?,'dietitian_coaching_4w',?,'payment_pending',NULL,?,'','','','','',?)""",
            (order_id, user_id, case_id, operation_key, requested_text, requested_text),
        )
        conn.execute(
            """INSERT INTO vip_health_check_audit_log
               (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
               VALUES (?,'customer',?,'','payment_pending',?,?)""",
            (case_id, user_id, f"coaching_order:{order_id}", requested_text),
        )
        conn.execute("RELEASE SAVEPOINT request_dietitian_coaching")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT request_dietitian_coaching")
        conn.execute("RELEASE SAVEPOINT request_dietitian_coaching")
        raise
    return {"order_id": order_id, "status": "payment_pending", "created": True}


def mark_coaching_payment_reported(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    user_id: str,
    reported_at: datetime,
) -> dict[str, object]:
    reported_text = _iso_seconds(reported_at)
    row = conn.execute(
        "SELECT user_id,case_id,status FROM dietitian_coaching_orders WHERE order_id=?",
        (order_id,),
    ).fetchone()
    if not row or row[0] != str(user_id or "").strip():
        raise ValueError("找不到目前使用者的陪跑申請")
    if row[2] in {"payment_reported", "coaching_active"}:
        return {"order_id": order_id, "status": row[2]}
    if row[2] != "payment_pending":
        raise ValueError("目前狀態不可回報付款")
    conn.execute("SAVEPOINT mark_coaching_payment_reported")
    try:
        fresh_row = conn.execute(
            "SELECT user_id,case_id,status FROM dietitian_coaching_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if not fresh_row or fresh_row[0] != str(user_id or "").strip():
            raise ValueError("找不到目前使用者的陪跑申請")
        if fresh_row[2] in {"payment_reported", "coaching_active"}:
            conn.execute("RELEASE SAVEPOINT mark_coaching_payment_reported")
            return {"order_id": order_id, "status": fresh_row[2]}
        if fresh_row[2] != "payment_pending":
            raise ValueError("目前狀態不可回報付款")
        changed = conn.execute(
            """UPDATE dietitian_coaching_orders
               SET status='payment_reported',payment_reported_at=?,updated_at=?
               WHERE order_id=? AND status='payment_pending'""",
            (reported_text, reported_text, order_id),
        )
        if changed.rowcount != 1:
            raise ValueError("陪跑申請狀態已更新，請重新載入")
        conn.execute(
            """INSERT INTO vip_health_check_audit_log
               (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
               VALUES (?,'customer',?,'payment_pending','payment_reported',?,?)""",
            (fresh_row[1], fresh_row[0], f"coaching_order:{order_id}", reported_text),
        )
        conn.execute("RELEASE SAVEPOINT mark_coaching_payment_reported")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT mark_coaching_payment_reported")
        conn.execute("RELEASE SAVEPOINT mark_coaching_payment_reported")
        raise
    return {"order_id": order_id, "status": "payment_reported"}


def activate_dietitian_coaching(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    confirmed_by: str,
    confirmed_at: datetime,
    duration_days: int = 28,
) -> dict[str, object]:
    confirmed_by = str(confirmed_by or "").strip()
    if not confirmed_by:
        raise ValueError("confirmed_by 不可空白")
    if not isinstance(duration_days, int) or not 1 <= duration_days <= 365:
        raise ValueError("duration_days 必須介於 1～365")
    row = conn.execute(
        """SELECT user_id,case_id,status,starts_at,ends_at
           FROM dietitian_coaching_orders WHERE order_id=?""",
        (order_id,),
    ).fetchone()
    if not row:
        raise ValueError("找不到陪跑申請")
    if row[2] == "coaching_active":
        return {
            "order_id": order_id,
            "user_id": row[0],
            "status": row[2],
            "starts_at": row[3],
            "ends_at": row[4],
        }
    if row[2] != "payment_reported":
        raise ValueError("尚未完成付款回報，不能開通")
    starts_at = _iso_seconds(confirmed_at)
    ends_at = _iso_seconds(confirmed_at + timedelta(days=duration_days))
    conn.execute("SAVEPOINT activate_dietitian_coaching")
    try:
        fresh_row = conn.execute(
            """SELECT user_id,case_id,status,starts_at,ends_at
               FROM dietitian_coaching_orders WHERE order_id=?""",
            (order_id,),
        ).fetchone()
        if not fresh_row:
            raise ValueError("找不到陪跑申請")
        if fresh_row[2] == "coaching_active":
            conn.execute("RELEASE SAVEPOINT activate_dietitian_coaching")
            return {
                "order_id": order_id,
                "user_id": fresh_row[0],
                "status": fresh_row[2],
                "starts_at": fresh_row[3],
                "ends_at": fresh_row[4],
            }
        if fresh_row[2] != "payment_reported":
            raise ValueError("尚未完成付款回報，不能開通")
        changed = conn.execute(
            """UPDATE dietitian_coaching_orders
               SET status='coaching_active',confirmed_by=?,confirmed_at=?,starts_at=?,
                   ends_at=?,updated_at=? WHERE order_id=? AND status='payment_reported'""",
            (confirmed_by, starts_at, starts_at, ends_at, starts_at, order_id),
        )
        if changed.rowcount != 1:
            raise ValueError("陪跑申請狀態已更新，請重新載入")
        conn.execute(
            """INSERT INTO vip_health_check_audit_log
               (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
               VALUES (?,'admin',?,'payment_reported','coaching_active',?,?)""",
            (fresh_row[1], confirmed_by, f"coaching_order:{order_id}", starts_at),
        )
        conn.execute("RELEASE SAVEPOINT activate_dietitian_coaching")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT activate_dietitian_coaching")
        conn.execute("RELEASE SAVEPOINT activate_dietitian_coaching")
        raise
    return {
        "order_id": order_id,
        "user_id": row[0],
        "status": "coaching_active",
        "starts_at": starts_at,
        "ends_at": ends_at,
    }
