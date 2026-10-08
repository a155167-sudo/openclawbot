import sqlite3
import server
from tests.test_nanjing_meal_logging_flow import _setup,_text_event,_estimate


def test_navigation_is_not_consumed_as_food_while_awaiting(tmp_path,monkeypatch):
    db,replies=_setup(tmp_path,monkeypatch)
    calls=[]
    monkeypatch.setattr(server,'estimate_text_meal_nutrition',lambda request:(calls.append(request) or _estimate('功能選單')))
    server._handle_message_impl(_text_event('nav-start','我要紀錄飲食'))
    server._handle_message_impl(_text_event('nav-menu','功能選單'))
    assert calls==[]
    state=server.get_text_meal_input('U-NANJING')
    assert state is not None
    assert state['status']=='cancelled'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM food_logs').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM pending_text_meal_estimates').fetchone()[0]==0
