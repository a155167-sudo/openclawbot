"""The registered readback commits only the original complete request row."""
import sqlite3

import pytest

from reschedule_service_integration import _LEGACY_REQUEST_DDL
from test_readback_atomic_authority import _assert_unknown, _case, _post


def _use_legacy_request_schema(path):
    with sqlite3.connect(path) as conn:
        original = conn.execute(
            "SELECT * FROM customer_pair_reschedule_requests"
        ).fetchone()
        conn.execute("DROP TABLE customer_pair_reschedule_requests")
        conn.execute(_LEGACY_REQUEST_DDL)
        conn.execute(
            "INSERT INTO customer_pair_reschedule_requests VALUES (?,?,?,?,?,?,?,?,?)",
            original[:9],
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_registered_full_request_success_and_replay(tmp_path, legacy):
    path, _sheet, book, app = _case(tmp_path)
    if legacy:
        _use_legacy_request_schema(path)
    with sqlite3.connect(path) as conn:
        original = conn.execute("SELECT * FROM customer_pair_reschedule_requests").fetchone()
    response = _post(app)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    with sqlite3.connect(path) as conn:
        final = conn.execute("SELECT * FROM customer_pair_reschedule_requests").fetchone()
        expected = list(original)
        expected[5] = "confirmed"
        assert final == tuple(expected)
        committed = tuple(conn.iterdump())
    replay = _post(app)
    assert replay.status_code == 200, replay.text
    with sqlite3.connect(path) as conn:
        assert tuple(conn.iterdump()) == committed
    assert len(book.batch_calls) == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("stage", ["confirmation", "projection"])
@pytest.mark.parametrize("field,value", [
    ("expires_at", "2099-01-01T00:00:00+08:00"),
    ("created_at", "2000-01-01T00:00:00+08:00"),
    ("admin_notification_status", "delivered"),
    ("admin_notification_last_error", "mutated by trigger"),
    ("admin_notification_attempted_at", "2099-01-01T00:00:00+08:00"),
])
def test_registered_trigger_mutation_rolls_back_full_request(
    tmp_path, legacy, stage, field, value,
):
    if legacy and field.startswith("admin_notification_"):
        pytest.skip("legacy request schema has no notification columns")
    path, _sheet, book, app = _case(tmp_path)
    if legacy:
        _use_legacy_request_schema(path)
    if stage == "confirmation":
        trigger_table = "reschedule_dispatch_operations"
        target = "NEW.request_id"
    else:
        trigger_table = "customer_pair_reschedule_requests"
        target = "NEW.request_id"
    with sqlite3.connect(path) as conn:
        conn.execute(f"""CREATE TRIGGER mutate_frozen_request
            AFTER UPDATE OF status ON {trigger_table}
            WHEN NEW.status='confirmed'
            BEGIN UPDATE customer_pair_reschedule_requests
                 SET {field}=? WHERE request_id={target}; END""".replace("?", "'" + value.replace("'", "''") + "'"))
        original_request = conn.execute(
            "SELECT * FROM customer_pair_reschedule_requests"
        ).fetchone()
        original_lease = conn.execute("SELECT * FROM workbook_write_leases").fetchone()
        original_confirmations = conn.execute(
            "SELECT * FROM reschedule_dispatch_confirmations"
        ).fetchall()
        before = tuple(conn.iterdump())
    batches = len(book.batch_calls)
    response = _post(app)
    assert response.status_code != 200, response.text
    with sqlite3.connect(path) as conn:
        assert tuple(conn.iterdump()) == before
        assert conn.execute("SELECT * FROM customer_pair_reschedule_requests").fetchone() == original_request
        assert conn.execute("SELECT * FROM workbook_write_leases").fetchone() == original_lease
        assert conn.execute("SELECT * FROM reschedule_dispatch_confirmations").fetchall() == original_confirmations
    _assert_unknown(path)
    assert len(book.batch_calls) == batches
