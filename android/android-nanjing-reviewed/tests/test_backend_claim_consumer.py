import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from nanjing_android_printer import (
    AmbiguousSendError,
    BackendClaimClient,
    ClaimRejected,
    ContractError,
    DeviceConfig,
    Ledger,
    V2DispatchJob,
    contract_rows_to_schedule,
    parse_contract,
)
from test_backend_contract_binding import FakeTransport, TODAY, config, payload


class ScriptedHTTP:
    def __init__(self, claims=(), reports=()):
        self.claims = list(claims)
        self.reports = list(reports)
        self.calls = []

    def __call__(self, method, path, body, _config):
        self.calls.append((method, path, dict(body)))
        queue = self.claims if path.endswith("dispatch-claims") else self.reports
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def rows():
    return contract_rows_to_schedule(parse_contract(payload(), config(dispatch_mode="backend_v2"), TODAY))


def permitted(operation="ignored"):
    return (201, {
        "status": "send_permitted", "operation_id": operation,
        "dispatch_row_id": "dispatch-1", "service_date": TODAY.isoformat(),
        "send_permit": "permit-" + "x" * 32,
    })


def accepted(operation="ignored"):
    return (200, {"operation_id": operation, "status": "transport_accepted",
                  "recorded_at": "2026-09-16T10:00:00+08:00", "printed": False})


def make_job(tmp_path, http, transport=None):
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    client = BackendClaimClient(config(dispatch_mode="backend_v2"), request_sender=http)
    job = V2DispatchJob(rows(), ledger, transport or FakeTransport(), client)
    return job, ledger


def test_config_mode_defaults_legacy_and_v2_is_explicit():
    assert config().dispatch_mode == "legacy"
    assert config(dispatch_mode="backend_v2").dispatch_mode == "backend_v2"
    with pytest.raises(ContractError):
        config(dispatch_mode="typo")


def test_v2_claim_then_durable_local_claim_sends_once_and_reports_without_sheet(tmp_path):
    http = ScriptedHTTP(claims=[permitted()], reports=[accepted()])
    transport = FakeTransport()
    job, ledger = make_job(tmp_path, http, transport)
    operation = job.operation_id(rows()[0])
    http.claims[0][1]["operation_id"] = operation
    http.reports[0][1]["operation_id"] = operation

    result = job.run(TODAY, allow_print=True)

    assert result.customer_dispatched == 1
    assert len(transport.payloads) == 1
    assert [call[1] for call in http.calls] == [
        "/internal/printer/v1/dispatch-claims", "/internal/printer/v1/dispatch-results"
    ]
    assert ledger.v2_state(operation) == "complete"
    assert rows()[0].worksheet is None


def test_claim_replay_409_and_claim_timeout_never_touch_socket(tmp_path):
    for name, response in (
        ("replay", (409, {"detail": "already claimed"})),
        ("timeout", TimeoutError("claim timeout")),
    ):
        sub = tmp_path / name
        sub.mkdir()
        http = ScriptedHTTP(claims=[response])
        transport = FakeTransport()
        job, _ = make_job(sub, http, transport)
        with pytest.raises((ClaimRejected, ContractError)):
            job.run(TODAY, allow_print=True)
        assert transport.payloads == []


def test_sendall_error_is_durable_unknown_and_never_auto_resends(tmp_path):
    http = ScriptedHTTP(claims=[permitted()], reports=[(200, {
        "operation_id": "ignored", "status": "outcome_unknown",
        "recorded_at": "2026-09-16T10:00:00+08:00", "printed": False,
    })])
    transport = FakeTransport([AmbiguousSendError("unknown")])
    job, ledger = make_job(tmp_path, http, transport)
    operation = job.operation_id(rows()[0])
    http.claims[0][1]["operation_id"] = operation
    http.reports[0][1]["operation_id"] = operation

    first = job.run(TODAY, allow_print=True)
    second = job.run(TODAY, allow_print=True)

    assert first.ambiguous == 1
    assert len(transport.payloads) == 1
    assert ledger.v2_state(operation) == "complete"
    assert len([c for c in http.calls if c[1].endswith("dispatch-claims")]) == 1


def test_result_ack_timeout_keeps_pending_report_and_retries_report_only(tmp_path):
    http = ScriptedHTTP(
        claims=[permitted()],
        reports=[TimeoutError("ack lost"), accepted()],
    )
    transport = FakeTransport()
    job, ledger = make_job(tmp_path, http, transport)
    operation = job.operation_id(rows()[0])
    http.claims[0][1]["operation_id"] = operation
    http.reports[1][1]["operation_id"] = operation

    first = job.run(TODAY, allow_print=True)
    assert first.report_pending == 1
    assert ledger.v2_state(operation) == "pending_report"
    second = job.run(TODAY, allow_print=True)

    assert len(transport.payloads) == 1
    assert len([c for c in http.calls if c[1].endswith("dispatch-claims")]) == 1
    assert len([c for c in http.calls if c[1].endswith("dispatch-results")]) == 2
    assert second.report_recovered == 1
    assert ledger.v2_state(operation) == "complete"


def test_v2_never_calls_legacy_sheet_writeback_or_local_retry_fallback(tmp_path):
    row = rows()[0]
    assert row.worksheet is None
    http = ScriptedHTTP(claims=[(409, {"detail": "dispatch is stale or unavailable"})])
    transport = FakeTransport()
    job, _ = make_job(tmp_path, http, transport)
    with pytest.raises(ClaimRejected):
        job.run(TODAY, allow_print=True)
    assert transport.payloads == []
