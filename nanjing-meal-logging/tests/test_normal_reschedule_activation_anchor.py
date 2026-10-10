"""New order anchor capture after one-time startup initialization."""

import pytest

from normal_reschedule_policy import ensure_normal_reschedule_anchors, normal_order_expiry
from test_normal_reschedule_policy import OWNER, OTHER, db


@pytest.mark.parametrize('entitlement_first', [False, True])
@pytest.mark.parametrize('entitlement_write', ['insert', 'transfer'])
def test_new_order_captures_original_expiry_in_both_write_orders(
    entitlement_first, entitlement_write,
):
    conn = db()
    ensure_normal_reschedule_anchors(conn)
    owner = OTHER if entitlement_write == 'insert' else OWNER
    conn.execute(
        "INSERT INTO subscription_orders VALUES(2,?,'pending',20,'')", (owner,)
    )
    if entitlement_write == 'insert':
        conn.execute(
            "INSERT INTO usage VALUES(?,20,'2026-10-20','2026-11-26','vip')",
            (owner,),
        )

    def activate():
        conn.execute(
            "UPDATE subscription_orders SET status='activated',"
            "formalized_at='2026-10-20T12:00:00+08:00' WHERE id=2"
        )

    def entitlement():
        if entitlement_write == 'insert':
            conn.execute(
                "INSERT INTO subscription_menu_entitlements VALUES(?,2,'active','2026-11-26')",
                (owner,),
            )
        else:
            conn.execute(
                "UPDATE subscription_menu_entitlements SET order_id=2,expires_on='2026-11-26' "
                "WHERE user_id=?", (owner,),
            )
            conn.execute(
                "UPDATE usage SET expiry_date='2026-11-26' WHERE user_id=?", (owner,)
            )

    if entitlement_first:
        entitlement()
        assert conn.execute(
            'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
        ).fetchone() is None
        activate()
    else:
        activate()
        assert conn.execute(
            'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
        ).fetchone() is None
        entitlement()
    conn.commit()

    assert normal_order_expiry(conn, 2, owner) == '2026-11-26'
    conn.execute(
        "UPDATE subscription_menu_entitlements SET expires_on='2027-01-01' "
        "WHERE user_id=?", (owner,),
    )
    conn.execute(
        "UPDATE usage SET expiry_date='2027-01-01' WHERE user_id=?", (owner,)
    )
    assert normal_order_expiry(conn, 2, owner) == '2026-11-26'
    assert conn.execute(
        'SELECT original_expiry_date FROM normal_reschedule_expiry_anchors WHERE order_id=1'
    ).fetchone()[0] == '2026-10-26'


def test_ineligible_join_does_not_capture_until_all_fields_are_valid():
    conn = db()
    ensure_normal_reschedule_anchors(conn)
    conn.execute(
        "INSERT INTO subscription_orders VALUES(2,?,'pending',0,'')", (OTHER,)
    )
    # A mismatched owner must not acquire the order's anchor.
    conn.execute(
        "UPDATE subscription_menu_entitlements SET order_id=2,expires_on='2026-11-26' "
        "WHERE user_id=?", (OWNER,),
    )
    conn.execute(
        "INSERT INTO subscription_menu_entitlements VALUES(?,2,'inactive','2026-02-30')",
        (OTHER,),
    )
    conn.execute("UPDATE subscription_menu_entitlements SET status='active' WHERE user_id=?", (OTHER,))
    assert conn.execute(
        'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone() is None  # Pending, unformalized, and zero meals.
    conn.execute("UPDATE subscription_orders SET status='activated' WHERE id=2")
    conn.execute("UPDATE subscription_orders SET meal_count=20 WHERE id=2")
    assert conn.execute(
        'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone() is None
    conn.execute("UPDATE subscription_orders SET formalized_at='2026-10-20' WHERE id=2")
    assert conn.execute(
        'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone() is None  # Invalid calendar date.
    conn.execute(
        "UPDATE subscription_menu_entitlements SET status='inactive' WHERE user_id=?",
        (OTHER,),
    )
    conn.execute(
        "UPDATE subscription_menu_entitlements SET expires_on='2026-11-26' WHERE user_id=?",
        (OTHER,),
    )
    assert conn.execute(
        'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone() is None
    conn.execute(
        "UPDATE subscription_menu_entitlements SET status='active' WHERE user_id=?",
        (OTHER,),
    )
    assert conn.execute(
        'SELECT original_expiry_date FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone()[0] == '2026-11-26'


def test_anchor_rolls_back_with_entitlement_transaction():
    conn = db()
    ensure_normal_reschedule_anchors(conn)
    conn.execute(
        "INSERT INTO subscription_orders VALUES(2,?,'activated',20,'2026-10-20')",
        (OTHER,),
    )
    conn.commit()
    conn.execute(
        "INSERT INTO subscription_menu_entitlements VALUES(?,2,'active','2026-11-26')",
        (OTHER,),
    )
    assert conn.execute(
        'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone() is not None
    conn.rollback()
    assert conn.execute(
        'SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2'
    ).fetchone() is None
    assert conn.execute(
        'SELECT 1 FROM subscription_menu_entitlements WHERE user_id=?', (OTHER,)
    ).fetchone() is None
