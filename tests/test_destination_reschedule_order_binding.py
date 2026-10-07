import ast
from pathlib import Path
import sqlite3
import server


def test_destination_loader_reuses_same_current_order_as_message_entry(tmp_path):
    db=tmp_path/'destination.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE subscription_menu_entitlements(order_id INTEGER,user_id TEXT,status TEXT)')
        conn.executemany('INSERT INTO subscription_menu_entitlements VALUES(?,?,?)',[(1,'OWNER','active'),(2,'OWNER','active'),(3,'OTHER','active')])
    tree=ast.parse(Path(server.__file__).read_text())
    nodes=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_load_customer_sheet_reschedule_context']
    assert len(nodes)==1
    called=[]
    def canonical_order(uid):
        called.append(uid)
        return 2 if uid=='OWNER' else None
    def menu_context(conn,*,order_id,owner_user_id,today):
        assert owner_user_id=='OWNER'
        return {'order_id':order_id,'authoritative':True,'source_dates':[],'occupied_dates':[]}
    namespace=dict(server.__dict__,DB_PATH=str(db),get_active_subscription_order_id=canonical_order,normal_order_menu_context=menu_context)
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(server.__file__),'exec'),namespace)
    loader=namespace['_load_customer_sheet_reschedule_context']
    result=loader('OWNER')
    assert result['order_id']==2
    assert called==['OWNER']
    assert loader('NO-ACTIVE-OWNER') is None
    assert called==['OWNER','NO-ACTIVE-OWNER']
