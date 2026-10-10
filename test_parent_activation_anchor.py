"""Lifecycle regression: a new order activates after server schema initialization."""
import pytest
from test_normal_reschedule_policy import db, OWNER
from normal_reschedule_policy import ensure_normal_reschedule_anchors, normal_order_expiry

@pytest.mark.parametrize('entitlement_first', [False, True])
def test_new_activation_after_startup_gets_fixed_order_anchor(entitlement_first):
    conn = db()
    ensure_normal_reschedule_anchors(conn)
    conn.execute("INSERT INTO subscription_orders VALUES(2,?,'pending',20,'')", (OWNER,))
    def activate_order():
        conn.execute("UPDATE subscription_orders SET status='activated',formalized_at='2026-10-20T12:00:00+08:00' WHERE id=2")
    def grant_entitlement():
        conn.execute("UPDATE subscription_menu_entitlements SET order_id=2,status='active',expires_on='2026-11-26' WHERE user_id=?", (OWNER,))
    if entitlement_first:
        grant_entitlement(); activate_order()
    else:
        activate_order(); grant_entitlement()
    conn.execute("UPDATE usage SET expiry_date='2026-11-26' WHERE user_id=?", (OWNER,))
    conn.commit()
    # No second startup initialization or lazy mutation on the read path.
    assert normal_order_expiry(conn, 2, OWNER) == '2026-11-26'
    conn.execute("UPDATE subscription_menu_entitlements SET expires_on='2027-01-01' WHERE user_id=?", (OWNER,))
    conn.execute("UPDATE usage SET expiry_date='2027-01-01' WHERE user_id=?", (OWNER,))
    assert normal_order_expiry(conn, 2, OWNER) == '2026-11-26'
    assert conn.execute('SELECT original_expiry_date FROM normal_reschedule_expiry_anchors WHERE order_id=1').fetchone()[0] == '2026-10-26'
