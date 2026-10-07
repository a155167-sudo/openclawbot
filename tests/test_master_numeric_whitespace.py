"""Numeric text must be transport-safe before any registered service write."""
import copy
import json
import pytest
from test_master_numeric_admission import (
    normal_db, sheet_fixture, master_row, SOURCE, TARGET, OWNER, ADMIN, NOW,
    verify_customer_reschedule_context, submit_customer_pair_reschedule_pending,
    approve_customer_pair_reschedule, verify_admin_context, normal_pair_policy,
    RescheduleRequestConflict,
)

@pytest.mark.parametrize('index,value', [(2, ' 1800 '), (6, ' 0 '), (20, '\t1\n')])
@pytest.mark.parametrize('existing', [False, True])
def test_whitespace_numeric_rejected_without_sheet_write(tmp_path, index, value, existing):
    conn = normal_db(tmp_path / 'numeric.sqlite3')
    sheet, book = sheet_fixture()
    row = master_row(TARGET)
    row[index] = value
    if existing:
        sheet._master.rows.append(row)
    else:
        conn.execute('UPDATE subscription_orders SET form_payload_json=? WHERE id=1',
                     (json.dumps({'master_api_rows': [master_row(SOURCE), row]}),))
        conn.commit()
    before = copy.deepcopy(sheet._master.rows)
    submit_customer_pair_reschedule_pending(conn,
        context=verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1),
        source_date=SOURCE, target_date=TARGET, request_id='RS_whitespace', now=NOW, feature_enabled=True)
    with pytest.raises(RescheduleRequestConflict):
        approve_customer_pair_reschedule(conn, sheet, request_id='RS_whitespace',
            admin_context=verify_admin_context(conn, ADMIN), now=NOW,
            feature_enabled=True, policy_validator=normal_pair_policy)
    assert book.batch_calls == []
    assert sheet._master.rows == before
