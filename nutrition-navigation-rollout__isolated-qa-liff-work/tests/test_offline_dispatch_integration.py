import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from subscription_dispatch_contract import (
    ORIGINAL_HEADERS,
    DispatchNotEligible,
    create_dispatch_router,
    ensure_dispatch_schema,
    publish_schedule,
    stage_schedule,
)

ANDROID_ROOT = Path(__file__).parents[1] / "android" / "android-nanjing-reviewed"
sys.path.insert(0, str(ANDROID_ROOT))
from nanjing_android_printer import (  # noqa: E402
    DeviceConfig,
    DispatchJob,
    Ledger,
    fetch_dispatch_contract,
    parse_contract,
    reconcile_contract,
)

UID = "U" + "1" * 32
TOKEN = "synthetic-device-token-" + "x" * 32
WORKBOOK_ID = "synthetic-nanjing-workbook"
TARGET = date(2026, 9, 16)
NOW = datetime.fromisoformat("2026-09-15T10:00:00+08:00")
TITLE = "離線整合客_1111_20260915"


class SharedWorksheet:
    def __init__(self):
        self.id = 9028
        self.title = TITLE
        self.rows = []
        self.updates = []

    def replace_schedule(self, tagged_rows):
        self.rows = [list(row) for row in tagged_rows]
        return self.rows

    def read_schedule(self):
        return [list(row) for row in self.rows]

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def batch_update(self, updates):
        self.updates.extend(updates)
        for update in updates:
            assert update["range"] == "N2"
            self.rows[1][13] = update["values"][0][0]


class FakeWorkbook:
    def __init__(self, worksheet):
        self.id = WORKBOOK_ID
        self.worksheet = worksheet

    def worksheets(self):
        return [self.worksheet]


class ClientResponseAdapter:
    def __init__(self, response):
        self.status = response.status_code
        self.body = response.content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class RegisteredAppOpener:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        parsed = urlsplit(request.full_url)
        headers = dict(request.header_items())
        response = self.client.get(parsed.path + "?" + parsed.query, headers=headers)
        return ClientResponseAdapter(response)


class MockTcpTransport:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return len(payload)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _schedule():
    # One complete ordinary-meal day: no workout text, no carb-cycle label.
    return [
        ORIGINAL_HEADERS,
        [
            "2026/09/16",
            "第1週-三",
            "南京香煎雞腿便當",
            "520",
            "35",
            "南京味噌鮭魚便當",
            "510",
            "34",
            "1030",
            "69",
            "",
            "$360",
            "",
            "待列印",
        ],
    ]


def test_registered_backend_to_android_offline_paid_order_prints_once(tmp_path):
    db_path = tmp_path / "producer.sqlite3"
    worksheet = SharedWorksheet()
    with _connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, formalized_at TEXT DEFAULT '')"
        )
        ensure_dispatch_schema(conn)
        conn.execute(
            "INSERT INTO subscription_orders(id,user_id,status) VALUES(1,?,'pending')",
            (UID,),
        )
        with pytest.raises(DispatchNotEligible):
            stage_schedule(
                conn,
                order_id=1,
                snapshot_uid=UID,
                workbook_id=WORKBOOK_ID,
                worksheet_title=TITLE,
                schedule_rows=_schedule(),
                now=NOW,
                id_factory=lambda: "must-not-stage-unpaid",
            )
        assert conn.execute("SELECT count(*) FROM subscription_dispatch_rows").fetchone()[0] == 0

        conn.execute("UPDATE subscription_orders SET status='activated' WHERE id=1")
        tagged = stage_schedule(
            conn,
            order_id=1,
            snapshot_uid=UID,
            workbook_id=WORKBOOK_ID,
            worksheet_title=TITLE,
            schedule_rows=_schedule(),
            now=NOW,
            id_factory=lambda: "dispatch-synthetic-paid-1",
        )
        publish_schedule(conn, order_id=1, sheet=worksheet, tagged_rows=tagged, now=NOW)
        conn.commit()

    app = FastAPI()
    app.include_router(
        create_dispatch_router(
            connection_factory=lambda: _connect(db_path),
            export_token=TOKEN,
            workbook_id=WORKBOOK_ID,
            now_factory=lambda: NOW,
        )
    )
    client = TestClient(app)
    assert any(route.path == "/internal/printer/v1/dispatch-contract" for route in app.routes)

    config = DeviceConfig.from_mapping(
        {
            "backend_origin": "https://openclawbot-production-production.up.railway.app",
            "device_bearer_token": TOKEN,
            "store_id": "nanjing",
            "workbook_id": WORKBOOK_ID,
            "http_timeout_seconds": 8,
        }
    )
    opener = RegisteredAppOpener(client)
    contract = fetch_dispatch_contract(config, TARGET, opener=opener)
    assert len(contract["rows"]) == 1
    exported = contract["rows"][0]
    assert exported["order_status"] == "activated"
    assert exported["source_columns"] == _schedule()[1]
    assert exported["lunch"] == "南京香煎雞腿便當"
    assert exported["dinner"] == "南京味噌鮭魚便當"
    assert exported["source_columns"][12] == ""

    records = parse_contract(contract, config, TARGET)
    reconciled = reconcile_contract(FakeWorkbook(worksheet), records, config, TARGET)
    transport = MockTcpTransport()
    ledger = Ledger(str(tmp_path / "android-ledger.sqlite3"))
    result = DispatchJob(reconciled.rows, ledger, transport).run(TARGET, allow_print=True)
    assert result.customer_dispatched == 1
    assert result.summary_dispatched == 1
    assert len(transport.payloads) == 2
    customer_ticket = transport.payloads[0].decode("cp950", errors="replace")
    assert "南京香煎雞腿便當" in customer_ticket
    assert "南京味噌鮭魚便當" in customer_ticket
    assert worksheet.rows[1][13] == "已送印"
    assert ledger.states() == [("complete", reconciled.rows[0].dispatch_key)]

    # A fresh Android fetch sees the same immutable backend receipt, but the fake
    # Sheet's mutable status causes exact matching to skip rather than print again.
    again_contract = fetch_dispatch_contract(config, TARGET, opener=opener)
    again = reconcile_contract(
        FakeWorkbook(worksheet), parse_contract(again_contract, config, TARGET), config, TARGET
    )
    assert again.rows == ()
    assert again.already_printed == 1
    replay_transport = MockTcpTransport()
    replay = DispatchJob(again.rows, ledger, replay_transport, already_printed=1).run(
        TARGET, allow_print=True
    )
    assert replay.customer_already_complete == 1
    assert replay_transport.payloads == []
    assert len(opener.calls) == 2
