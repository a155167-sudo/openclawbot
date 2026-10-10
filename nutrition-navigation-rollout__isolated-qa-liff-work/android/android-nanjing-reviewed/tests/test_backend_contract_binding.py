import io
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from nanjing_android_printer import (
    AmbiguousSendError,
    ContractError,
    DeviceConfig,
    DispatchJob,
    Ledger,
    RedirectBlocked,
    SocketTransport,
    fetch_dispatch_contract,
    is_absent_meal,
    main,
    parse_contract,
    reconcile_contract,
    render_customer_ticket,
)

TODAY = date(2026, 9, 16)
ORIGIN = "https://openclawbot-production-production.up.railway.app"
UID = "U" + "a" * 32
HEADERS14 = [
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
]
HEADERS17 = HEADERS14 + ["Dispatch_Row_ID", "Order_ID", "Menu_Version"]
SOURCE = ["2026/09/16", "第1週-三", "無骨雞便當", "500", "30", "無", "0", "0", "500", "30", "", "$100", "", "待列印"]


def config(**overrides):
    raw = {
        "backend_origin": ORIGIN,
        "device_bearer_token": "device-test-" + "x" * 32,
        "store_id": "nanjing",
        "workbook_id": "runtime-sheet-id",
        "http_timeout_seconds": 8,
    }
    raw.update(overrides)
    return DeviceConfig.from_mapping(raw)


def payload(**row_overrides):
    row = {
        "dispatch_row_id": "dispatch-1",
        "worksheet_id": 77,
        "worksheet_title": "王小明_abcd_20260901",
        "order_id": 123,
        "order_status": "activated",
        "customer_uid": UID,
        "formalized_at": "2026-09-15T10:00:00+08:00",
        "menu_version": "order-123-v1",
        "service_date": "2026-09-16",
        "lunch": "無骨雞便當",
        "dinner": "無",
        "source_columns": list(SOURCE),
        "publication_print_status": "待列印",
        "print_status_policy": "external_mutable_not_publication_identity",
        "receipt_version": 1,
    }
    row.update(row_overrides)
    return {
        "contract_version": 1,
        "store_id": "nanjing",
        "workbook_id": "runtime-sheet-id",
        "generated_at": "2026-09-15T10:01:00+08:00",
        "rows": [row],
    }


class FakeWorksheet:
    def __init__(self, values=None, *, title="王小明_abcd_20260901", sheet_id=77, fail_updates=0):
        self.title, self.id = title, sheet_id
        self._values = values or [HEADERS17, SOURCE + ["dispatch-1", "123", "order-123-v1"]]
        self.fail_updates = fail_updates
        self.updates = []

    def get_all_values(self):
        return [list(row) for row in self._values]

    def batch_update(self, updates):
        if self.fail_updates:
            self.fail_updates -= 1
            raise RuntimeError("writeback failed")
        self.updates.append(updates)
        self._values[1][13] = updates[0]["values"][0][0]


class FakeWorkbook:
    def __init__(self, worksheets, workbook_id="runtime-sheet-id"):
        self.id = workbook_id
        self._worksheets = list(worksheets)

    def worksheets(self):
        return list(self._worksheets)


class FakeTransport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.payloads = []

    def send(self, data):
        self.payloads.append(data)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        return len(data)


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class CapturingOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.response


class BackendBindingTests(unittest.TestCase):
    def ledger(self, td):
        return Ledger(str(Path(td) / "ledger.sqlite3"))

    def bound_rows(self, ws=None, data=None):
        ws = ws or FakeWorksheet()
        contract_rows = parse_contract(data or payload(), config(), TODAY)
        return ws, reconcile_contract(FakeWorkbook([ws]), contract_rows, config(), TODAY)

    def test_fetch_uses_exact_readonly_query_bearer_and_timeout(self):
        opener = CapturingOpener(FakeResponse(json.dumps(payload()).encode()))
        result = fetch_dispatch_contract(config(), TODAY, opener=opener)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, ORIGIN + "/internal/printer/v1/dispatch-contract?store_id=nanjing&service_date=2026-09-16")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer " + config().device_bearer_token)
        self.assertEqual(timeout, 8)
        self.assertEqual(result["contract_version"], 1)

    def test_config_requires_https_exact_origin_and_real_missing_credential_fails(self):
        bad = [
            {"backend_origin": "http://example.test"},
            {"backend_origin": "https://evil.example"},
            {"backend_origin": "https://example.test/path"},
            {"backend_origin": "https://user:pass@example.test"},
            {"device_bearer_token": ""},
            {"device_bearer_token": "REPLACE_WITH_DEVICE_TOKEN"},
            {"workbook_id": ""},
        ]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(ContractError):
                config(**override)

    def test_cross_origin_redirect_is_blocked_without_forwarding_authorization(self):
        handler = RedirectBlocked(ORIGIN)
        request = mock.Mock(full_url=ORIGIN + "/internal/printer/v1/dispatch-contract")
        with self.assertRaises(ContractError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal")

    def test_auth_http_and_malformed_json_errors_are_sanitized(self):
        from urllib.error import HTTPError
        secret = config().device_bearer_token
        auth_error = HTTPError(ORIGIN, 403, secret, {}, io.BytesIO(secret.encode()))
        for response in (auth_error, FakeResponse(b"not-json")):
            opener = mock.Mock()
            opener.open.side_effect = response if isinstance(response, BaseException) else None
            if not isinstance(response, BaseException):
                opener.open.return_value = response
            with self.assertRaises(ContractError) as raised:
                fetch_dispatch_contract(config(), TODAY, opener=opener)
            self.assertNotIn(secret, str(raised.exception))

    def test_contract_positive_binds_exact_backend_identity_and_14_columns(self):
        rows = parse_contract(payload(), config(), TODAY)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.dispatch_row_id, "dispatch-1")
        self.assertEqual(row.customer_uid, UID)
        self.assertEqual(row.order_id, "123")
        self.assertEqual(row.worksheet_id, 77)
        self.assertEqual(row.source_columns, tuple(SOURCE))

    def test_contract_rejects_duplicates_nonactivated_unknown_receipt_and_bad_payload(self):
        cases = []
        duplicate = payload(); duplicate["rows"].append(dict(duplicate["rows"][0])); cases.append(duplicate)
        cases.append(payload(order_status="approved"))
        cases.append(payload(receipt_version=999))
        cases.append(payload(source_columns=SOURCE[:-1]))
        cases.append(payload(print_status_policy="unknown"))
        cases.append(payload(customer_uid="short"))
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ContractError):
                parse_contract(data, config(), TODAY)

    def test_contract_shape_is_exact_and_empty_export_cannot_hide_unknown_sheet_receipt(self):
        extra_top = payload(); extra_top["debug"] = "not in v1"
        extra_row = payload(); extra_row["rows"][0]["legacy_binding"] = True
        for data in (extra_top, extra_row):
            with self.assertRaises(ContractError):
                parse_contract(data, config(), TODAY)
        empty = payload(); empty["rows"] = []
        with self.assertRaisesRegex(ContractError, "unknown receipt"):
            reconcile_contract(FakeWorkbook([FakeWorksheet()]), parse_contract(empty, config(), TODAY), config(), TODAY)

    def test_wrong_workbook_or_service_date_fails_closed(self):
        for data in (payload(), payload(service_date="2026-09-17")):
            cfg = config(workbook_id="wrong") if data["rows"][0]["service_date"] == "2026-09-16" else config()
            with self.assertRaises(ContractError):
                parse_contract(data, cfg, TODAY)

    def test_reconcile_exact_positive_and_current_print_status_is_external(self):
        ws, reconciled = self.bound_rows()
        self.assertEqual(len(reconciled.rows), 1)
        self.assertEqual(reconciled.already_printed, 0)
        ws._values[1][13] = "已送印"
        result = reconcile_contract(FakeWorkbook([ws]), parse_contract(payload(), config(), TODAY), config(), TODAY)
        self.assertEqual(result.rows, ())
        self.assertEqual(result.already_printed, 1)

    def test_missing_row_duplicate_unknown_row_tamper_and_sheet_identity_all_block(self):
        base = SOURCE + ["dispatch-1", "123", "order-123-v1"]
        variants = [
            FakeWorksheet(values=[HEADERS17]),
            FakeWorksheet(values=[HEADERS17, base, list(base)]),
            FakeWorksheet(values=[HEADERS17, base, SOURCE + ["unknown", "999", "v"]]),
            FakeWorksheet(values=[HEADERS17, base + ["unexpected-extra"]]),
            FakeWorksheet(values=[HEADERS17, [*SOURCE[:2], "被改餐", *SOURCE[3:], "dispatch-1", "123", "order-123-v1"]]),
            FakeWorksheet(sheet_id=88),
        ]
        rows = parse_contract(payload(), config(), TODAY)
        for ws in variants:
            with self.subTest(values=ws._values), self.assertRaises(ContractError):
                reconcile_contract(FakeWorkbook([ws]), rows, config(), TODAY)

    def test_header_must_be_exact_and_unique(self):
        rows = parse_contract(payload(), config(), TODAY)
        for values in ([HEADERS14], [HEADERS17, HEADERS17, SOURCE + ["dispatch-1", "123", "order-123-v1"]]):
            with self.assertRaises(ContractError):
                reconcile_contract(FakeWorkbook([FakeWorksheet(values=values)]), rows, config(), TODAY)

    def test_default_dry_run_has_no_sheet_write_socket_or_ledger_row(self):
        ws, reconciled = self.bound_rows()
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            result = DispatchJob(reconciled.rows, ledger, transport).run(TODAY)
            self.assertEqual(result.dry_run_candidates, 1)
            self.assertEqual(transport.payloads, [])
            self.assertEqual(ws.updates, [])
            self.assertEqual(ledger.states(), [])

    def test_positive_render_fake_socket_ledger_writeback_failure_then_no_reprint(self):
        ws = FakeWorksheet(fail_updates=1)
        _, reconciled = self.bound_rows(ws)
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            first = DispatchJob(reconciled.rows, ledger, transport).run(TODAY, allow_print=True)
            self.assertEqual(first.customer_dispatched, 1)
            self.assertEqual(first.writeback_failed, 1)
            _, second_rows = self.bound_rows(ws)
            second = DispatchJob(second_rows.rows, ledger, transport).run(TODAY, allow_print=True)
            self.assertEqual(second.writeback_recovered, 1)
            self.assertEqual(len(transport.payloads), 2)  # customer + summary, no second customer
            self.assertEqual(ws._values[1][13], "已送印")

    def test_connect_failure_retryable_and_send_failure_ambiguous(self):
        ws, reconciled = self.bound_rows()
        class Sock:
            def settimeout(self, _): pass
            def connect(self, _): raise OSError("offline")
            def close(self): pass
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            DispatchJob(reconciled.rows, ledger, SocketTransport("127.0.0.1", 9100, socket_factory=Sock)).run(TODAY, allow_print=True)
            self.assertEqual(ledger.states()[0][0], "retryable")
        ws, reconciled = self.bound_rows()
        transport = FakeTransport([AmbiguousSendError("drop")])
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            first = DispatchJob(reconciled.rows, ledger, transport).run(TODAY, allow_print=True)
            second = DispatchJob(reconciled.rows, ledger, transport).run(TODAY, allow_print=True)
            self.assertEqual((first.ambiguous, second.blocked_manual, len(transport.payloads)), (1, 1, 1))

    def test_two_real_sqlite_connections_claim_one_retry_only(self):
        """Two Android processes sharing a ledger must not both enter socket send."""
        _, reconciled = self.bound_rows()
        row = reconciled.rows[0]
        barrier = threading.Barrier(2)

        class BarrierConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if ("SELECT state" in sql and "FROM customer_dispatch WHERE dispatch_key" in sql
                        and not self.in_transaction):
                    # Reproduce the old SELECT-before-write race.  The fixed path
                    # already owns BEGIN IMMEDIATE here and therefore bypasses it.
                    barrier.wait(timeout=2)
                return cursor

        class RacingLedger(Ledger):
            def _connect(self):
                conn = sqlite3.connect(self.path, timeout=2, factory=BarrierConnection)
                conn.row_factory = sqlite3.Row
                return conn

        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            seed = ledger.claim_customer_send(row)
            self.assertEqual(seed.action, "send")
            ledger.mark_customer(row.dispatch_key, "retryable", "connect failed", claim_token=seed.token)
            racers = [RacingLedger(ledger.path), RacingLedger(ledger.path)]
            actions = []
            errors = []

            def claim(candidate):
                try:
                    actions.append(candidate.begin_customer_send(row))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=claim, args=(candidate,)) for candidate in racers]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(actions), ["manual", "send"])

    def test_writer_lock_is_released_before_socket_and_parallel_job_refuses(self):
        _, reconciled = self.bound_rows()
        row = reconciled.rows[0]

        class DelayedTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def send(self, data):
                self.payloads.append(data)
                if len(self.payloads) == 1:
                    self.entered.set()
                    if not self.release.wait(timeout=3):
                        raise AssertionError("test did not release fake socket")
                return len(data)

        with tempfile.TemporaryDirectory() as td:
            first_ledger = self.ledger(td)
            second_ledger = Ledger(first_ledger.path)
            transport = DelayedTransport()
            results = []
            worker = threading.Thread(target=lambda: results.append(
                DispatchJob([row], first_ledger, transport).run(TODAY, allow_print=True)))
            worker.start()
            self.assertTrue(transport.entered.wait(timeout=2))

            # The sender is blocked in fake socket I/O, yet another writer can
            # commit: no SQLite write transaction spans printer I/O.
            with sqlite3.connect(first_ledger.path, timeout=0.2) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("CREATE TABLE lock_release_probe(value INTEGER)")
                probe.execute("INSERT INTO lock_release_probe VALUES(1)")
                probe.commit()

            refused = DispatchJob([row], second_ledger, FakeTransport()).run(TODAY, allow_print=True)
            self.assertEqual(refused.blocked_manual, 1)
            transport.release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0].customer_dispatched, 1)
            self.assertEqual(len(transport.payloads), 2)  # one customer + one summary

    def test_busy_ledger_fails_closed_before_socket(self):
        _, reconciled = self.bound_rows()
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            transport = FakeTransport()
            blocker = sqlite3.connect(ledger.path)
            blocker.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaisesRegex(ContractError, "ledger busy"):
                    DispatchJob(reconciled.rows, ledger, transport).run(TODAY, allow_print=True)
            finally:
                blocker.rollback()
                blocker.close()
            self.assertEqual(transport.payloads, [])

    def test_stale_claim_token_cannot_complete_new_retry_generation(self):
        _, reconciled = self.bound_rows()
        row = reconciled.rows[0]
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            old = ledger.claim_customer_send(row)
            ledger.mark_customer(row.dispatch_key, "retryable", claim_token=old.token)
            current = ledger.claim_customer_send(row)
            self.assertNotEqual(old.token, current.token)
            with self.assertRaisesRegex(ContractError, "stale customer claim"):
                ledger.mark_customer(row.dispatch_key, "ambiguous", claim_token=old.token)
            self.assertEqual(ledger.states()[0][0], "sending")

    def test_invalid_calendar_date_in_sheet_fails_closed(self):
        bad = list(SOURCE)
        bad[0] = "2026/02/30"
        ws = FakeWorksheet(values=[HEADERS17, bad + ["dispatch-1", "123", "order-123-v1"]])
        with self.assertRaisesRegex(ContractError, "日期無效"):
            reconcile_contract(FakeWorkbook([ws]), parse_contract(payload(), config(), TODAY), config(), TODAY)

    def test_default_date_uses_asia_taipei_across_midnight(self):
        target = date(2026, 9, 17)
        source = list(SOURCE)
        source[0] = "2026/09/17"
        data = payload(service_date="2026-09-17", source_columns=source)
        ws = FakeWorksheet(values=[HEADERS17, source + ["dispatch-1", "123", "order-123-v1"]])
        seen = []
        fake_datetime = mock.Mock(wraps=datetime)
        fake_datetime.now.return_value = datetime(2026, 9, 17, 0, 1, tzinfo=ZoneInfo("Asia/Taipei"))
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("nanjing_android_printer.datetime", fake_datetime), \
             mock.patch("nanjing_android_printer.load_device_config", return_value=config()), \
             mock.patch("nanjing_android_printer.fetch_dispatch_contract", side_effect=lambda _cfg, day: seen.append(day) or data), \
             mock.patch("nanjing_android_printer.find_key_path", return_value="fake-key"), \
             mock.patch("nanjing_android_printer.connect_workbook", return_value=FakeWorkbook([ws])):
            code = main(["--config", "private.json", "--ledger", str(Path(td) / "ledger.db")])
        self.assertEqual((code, seen), (0, [target]))
        fake_datetime.now.assert_any_call(ZoneInfo("Asia/Taipei"))

    def test_cross_day_and_incremental_summary_members_are_counted_once(self):
        _, reconciled = self.bound_rows()
        first = reconciled.rows[0]
        with tempfile.TemporaryDirectory() as td:
            ledger = self.ledger(td)
            first_transport = FakeTransport()
            DispatchJob([first], ledger, first_transport).run(TODAY, allow_print=True)

            salmon_source = list(first.source_columns)
            salmon_source[2] = "鮭魚便當"
            second_ws = FakeWorksheet(title="李小美_eeee_20260901", sheet_id=88)
            second = replace(first, worksheet=second_ws, dispatch_row_id="dispatch-2",
                             worksheet_title=second_ws.title, worksheet_id=88,
                             customer_uid="U" + "b" * 32, order_id="456",
                             schedule_version="order-456-v1", lunch="鮭魚便當",
                             source_columns=tuple(salmon_source))
            incremental_transport = FakeTransport()
            DispatchJob([second], ledger, incremental_transport).run(TODAY, allow_print=True)
            summary = incremental_transport.payloads[1].decode("cp950", errors="replace")
            self.assertIn("鮭魚便當", summary)
            self.assertNotIn("無骨雞便當", summary)

            replay_transport = FakeTransport()
            replay = DispatchJob([first, second], ledger, replay_transport).run(TODAY, allow_print=True)
            self.assertEqual(replay.customer_already_complete, 2)
            self.assertEqual(replay_transport.payloads, [])

            next_source = list(first.source_columns)
            next_source[0] = "2026/09/17"
            next_source[2] = "牛肉便當"
            next_ws = FakeWorksheet(title="隔日客戶_ffff_20260901", sheet_id=99)
            next_row = replace(first, worksheet=next_ws, dispatch_row_id="dispatch-next",
                               worksheet_title=next_ws.title, worksheet_id=99,
                               customer_uid="U" + "c" * 32, order_id="789",
                               schedule_version="order-789-v1", service_date="2026/09/17",
                               lunch="牛肉便當", source_columns=tuple(next_source))
            next_transport = FakeTransport()
            DispatchJob([next_row], ledger, next_transport).run(date(2026, 9, 17), allow_print=True)
            next_summary = next_transport.payloads[1].decode("cp950", errors="replace")
            self.assertIn("日期：2026/09/17", next_summary)
            self.assertIn("牛肉便當", next_summary)
            self.assertNotIn("鮭魚便當", next_summary)
            self.assertNotIn("無骨雞便當", next_summary)

    def test_old_ledger_migration_preserves_ambiguous_and_manual_states(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "old.sqlite3")
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                CREATE TABLE customer_dispatch(
                  dispatch_key TEXT PRIMARY KEY, semantic_identity TEXT NOT NULL UNIQUE,
                  state TEXT NOT NULL, worksheet_title TEXT NOT NULL, worksheet_id INTEGER NOT NULL,
                  row_number INTEGER NOT NULL, customer_uid TEXT NOT NULL, customer_name TEXT NOT NULL,
                  order_id TEXT NOT NULL, schedule_version TEXT NOT NULL, service_date TEXT NOT NULL,
                  lunch TEXT NOT NULL, dinner TEXT NOT NULL, status_column INTEGER NOT NULL,
                  last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE TABLE summary_dispatch(
                  summary_key TEXT PRIMARY KEY, service_date TEXT NOT NULL, state TEXT NOT NULL,
                  member_keys_json TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE TABLE summary_members(
                  dispatch_key TEXT PRIMARY KEY, summary_key TEXT NOT NULL, summarized_at TEXT NOT NULL);
                INSERT INTO customer_dispatch VALUES(
                  'old-key','old-id','ambiguous','sheet',1,2,'Uaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                  'name','1','v1','2026/09/16','meal','無',14,'uncertain','2026-09-16T00:00:00+08:00');
                INSERT INTO summary_dispatch VALUES(
                  'old-summary','2026/09/16','manual','["old-key"]','review','2026-09-16T00:00:00+08:00');
                """)
            ledger = Ledger(path)
            self.assertEqual(ledger.states(), [("ambiguous", "old-key")])
            with sqlite3.connect(path) as conn:
                customer = conn.execute("SELECT state,claim_token FROM customer_dispatch").fetchone()
                summary = conn.execute("SELECT state,claim_token FROM summary_dispatch").fetchone()
            self.assertEqual(customer, ("ambiguous", ""))
            self.assertEqual(summary, ("manual", ""))

    def test_absence_cp950_and_result_wording(self):
        self.assertFalse(is_absent_meal("無骨雞便當"))
        ticket = render_customer_ticket("王小明", "2026/09/16", "無骨雞🥗", "無")
        self.assertIn("🥗", ticket.unsupported_characters)
        self.assertIn("無骨雞", ticket.payload.decode("cp950", errors="replace"))

    def test_cli_missing_private_config_never_opens_sheet_or_socket(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("nanjing_android_printer.connect_workbook", side_effect=AssertionError("sheet")), \
             mock.patch("nanjing_android_printer.socket.socket", side_effect=AssertionError("socket")):
            code = main(["--config", str(Path(td) / "missing.json"), "--date", "2026/09/16"])
        self.assertEqual(code, 2)

    def test_cli_offline_full_positive_default_dry_run(self):
        ws = FakeWorksheet()
        cfg = config()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("nanjing_android_printer.load_device_config", return_value=cfg), \
             mock.patch("nanjing_android_printer.fetch_dispatch_contract", return_value=payload()), \
             mock.patch("nanjing_android_printer.find_key_path", return_value="fake-key"), \
             mock.patch("nanjing_android_printer.connect_workbook", return_value=FakeWorkbook([ws])), \
             mock.patch("nanjing_android_printer.socket.socket", side_effect=AssertionError("socket")):
            code = main(["--config", "private.json", "--date", "2026/09/16", "--ledger", str(Path(td) / "ledger.db")])
        self.assertEqual(code, 0)
        self.assertEqual(ws.updates, [])


if __name__ == "__main__":
    unittest.main()
