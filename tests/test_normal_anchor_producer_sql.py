"""Exercise the exact entitlement UPSERT SQL used by server redemption."""
import ast
from pathlib import Path
import pytest
from normal_reschedule_policy import ensure_normal_reschedule_anchors
from test_normal_reschedule_policy import OWNER, OTHER, db

@pytest.mark.parametrize('existing_owner', [False, True])
def test_real_redemption_upsert_preserves_anchor_on_retry_and_renewal(existing_owner):
    tree = ast.parse((Path(__file__).parents[1] / 'server.py').read_text())
    matches = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and 'INSERT INTO subscription_menu_entitlements' in n.value]
    assert len(matches) == 1
    sql = matches[0]
    conn = db()
    for name in ('vip_code', 'starts_on', 'created_at'):
        conn.execute(f'ALTER TABLE subscription_menu_entitlements ADD COLUMN {name} TEXT')
    ensure_normal_reschedule_anchors(conn)
    owner = OWNER if existing_owner else OTHER
    conn.execute("INSERT INTO subscription_orders VALUES(2,?,'activated',20,'2026-10-20')", (owner,))
    for expiry in ('2026-11-26', '2026-11-26', '2027-01-01'):
        conn.execute(sql, (owner, 2, 'synthetic-code', '2026-10-20', expiry, '2026-10-20 12:00:00'))
        assert conn.execute('SELECT original_expiry_date FROM normal_reschedule_expiry_anchors WHERE order_id=2').fetchone()[0] == '2026-11-26'
    conn.rollback()
    assert conn.execute('SELECT 1 FROM normal_reschedule_expiry_anchors WHERE order_id=2').fetchone() is None
