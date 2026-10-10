import sys
from pathlib import Path

import pytest

ANDROID = Path(__file__).parents[1] / "android" / "android-nanjing-reviewed"
if str(ANDROID) not in sys.path:
    sys.path.insert(0, str(ANDROID))

from nanjing_android_printer import (  # noqa: E402
    BackendClaimClient,
    ClaimRejected,
    ContractError,
    DeviceConfig,
    Ledger,
    V2DispatchJob,
    contract_rows_to_schedule,
    parse_contract,
)
from test_pair_reschedule_coordinator import (  # noqa: E402
    AtomicSheetFake,
    HTTP_TOKEN,
    NOW,
    SOURCE,
    STORE,
    TARGET,
    _http_client,
    call,
    old_source_row,
    open_db,
)


class FakePrinter:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return len(payload)


def device_config(token=HTTP_TOKEN):
    return DeviceConfig.from_mapping({
        "backend_origin": "https://openclawbot-production-production.up.railway.app",
        "device_bearer_token": token,
        "store_id": STORE,
        "workbook_id": "book-1",
        "http_timeout_seconds": 8,
        "dispatch_mode": "backend_v2",
    })


class ClientSender:
    def __init__(self, client, *, fail_first_result=False):
        self.client = client
        self.fail_first_result = fail_first_result
        self.calls = []

    def __call__(self, method, path, body, cfg):
        self.calls.append((method, path, dict(body)))
        if self.fail_first_result and path.endswith("dispatch-results"):
            self.fail_first_result = False
            response = self.client.request(
                method, path,
                headers={"Authorization": "Bearer " + cfg.device_bearer_token},
                json=body,
            )
            assert response.status_code == 200
            raise TimeoutError("synthetic result applied then acknowledgement lost")
        response = self.client.request(
            method, path,
            headers={"Authorization": "Bearer " + cfg.device_bearer_token},
            json=body,
        )
        return response.status_code, response.json()


def get_contract(client, service_date, cfg=None):
    cfg = cfg or device_config()
    response = client.get(
        "/internal/printer/v1/dispatch-contract",
        params={"store_id": STORE, "service_date": service_date},
        headers={"Authorization": "Bearer " + cfg.device_bearer_token},
    )
    assert response.status_code == 200
    return response.json()


def schedule(contract, cfg, day):
    return contract_rows_to_schedule(parse_contract(contract, cfg, day))


def test_actual_consumer_source_zero_target_one_cached_old_zero_new_exactly_one_and_replay_zero(tmp_path):
    db = tmp_path / "chain.sqlite3"
    conn, _ = open_db(db)
    client = _http_client(db, enabled=True)
    cfg = device_config()

    old_contract = get_contract(client, SOURCE, cfg)
    assert len(old_contract["rows"]) == 1
    old_rows = schedule(old_contract, cfg, NOW.date().replace(day=27))

    sheet = AtomicSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    assert call(conn, sheet).status == "confirmed"
    conn.close()

    assert get_contract(client, SOURCE, cfg)["rows"] == []
    target_contract = get_contract(client, TARGET, cfg)
    assert len(target_contract["rows"]) == 1
    target_rows = schedule(target_contract, cfg, NOW.date().replace(day=28))

    stale_printer = FakePrinter()
    stale_job = V2DispatchJob(
        old_rows, Ledger(str(tmp_path / "stale.sqlite3")), stale_printer,
        BackendClaimClient(cfg, request_sender=ClientSender(client)),
    )
    with pytest.raises(ClaimRejected):
        stale_job.run(NOW.date().replace(day=27), allow_print=True)
    assert stale_printer.payloads == []

    sender = ClientSender(client)
    new_printer = FakePrinter()
    new_job = V2DispatchJob(
        target_rows, Ledger(str(tmp_path / "new.sqlite3")), new_printer,
        BackendClaimClient(cfg, request_sender=sender),
    )
    result = new_job.run(NOW.date().replace(day=28), allow_print=True)
    assert result.customer_dispatched == 1
    assert len(new_printer.payloads) == 1
    assert get_contract(client, TARGET, cfg)["rows"] == []

    replay_printer = FakePrinter()
    replay = V2DispatchJob(
        target_rows, Ledger(str(tmp_path / "cache-cleared.sqlite3")), replay_printer,
        BackendClaimClient(cfg, request_sender=ClientSender(client)),
    )
    with pytest.raises(ClaimRejected):
        replay.run(NOW.date().replace(day=28), allow_print=True)
    assert replay_printer.payloads == []


def test_result_ack_timeout_restarts_as_report_only_and_wrong_bearer_never_sends(tmp_path):
    db = tmp_path / "ack.sqlite3"
    conn, _ = open_db(db)
    conn.close()
    client = _http_client(db, enabled=True)
    cfg = device_config()
    rows = schedule(get_contract(client, SOURCE, cfg), cfg, NOW.date().replace(day=27))

    sender = ClientSender(client, fail_first_result=True)
    printer = FakePrinter()
    ledger = Ledger(str(tmp_path / "ack-ledger.sqlite3"))
    job = V2DispatchJob(rows, ledger, printer, BackendClaimClient(cfg, request_sender=sender))
    first = job.run(NOW.date().replace(day=27), allow_print=True)
    assert first.report_pending == 1
    assert len(printer.payloads) == 1

    second = job.run(NOW.date().replace(day=27), allow_print=True)
    assert second.report_recovered == 1
    assert len(printer.payloads) == 1
    assert [path for _, path, _ in sender.calls].count("/internal/printer/v1/dispatch-claims") == 1
    assert [path for _, path, _ in sender.calls].count("/internal/printer/v1/dispatch-results") == 2

    wrong = device_config("wrong-device-credential-" + "x" * 32)
    wrong_printer = FakePrinter()
    wrong_job = V2DispatchJob(
        rows, Ledger(str(tmp_path / "wrong.sqlite3")), wrong_printer,
        BackendClaimClient(wrong, request_sender=ClientSender(client)),
    )
    with pytest.raises(ContractError):
        wrong_job.run(NOW.date().replace(day=27), allow_print=True)
    assert wrong_printer.payloads == []
