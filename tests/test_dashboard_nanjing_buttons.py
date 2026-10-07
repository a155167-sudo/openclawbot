"""Native registered-handler acceptance for the Nanjing three-button dashboard.

Uses the suite's isolated DATA_DIR and no-network conftest; no authorization mock.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import server


class FakeLineTransport:
    def __init__(self):
        self.replies = []

    def reply_message(self, token, message, **kwargs):
        self.replies.append((token, message, kwargs))


def _event(number, text, uid):
    return SimpleNamespace(
        message=SimpleNamespace(id=f"BUTTON-{number}", text=text),
        source=SimpleNamespace(user_id=uid),
        reply_token=f"reply-BUTTON-{number}",
    )


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


@pytest.fixture(params=[0,20])
def real_vip(tmp_path, monkeypatch, request):
    """Real fail-closed VIP lookup against an isolated DB; auth is not mocked."""
    assert Path(server.__file__).resolve().parent == Path(__file__).resolve().parents[1]
    uid = "U-BUTTON-NATIVE-HANDLER"
    db_dir = tmp_path / "isolated-data"
    db_path = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, request.param, 48, today, "vip", "2099-12-31", 20),
        )
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,
                today_food_items,today_date)
               VALUES (?,?,?,?,0,0,'',?)""",
            (uid, "三按鈕驗收會員", 1800, 100, today),
        )
        conn.commit()
    assert server.has_active_vip_access(uid) is True
    transport = FakeLineTransport()
    monkeypatch.setattr(server, "line_bot_api", transport)
    server.processed_messages.clear()
    return uid, db_path, transport


def test_three_rendered_home_buttons_reach_native_registered_handlers(real_vip, monkeypatch):
    uid, db_path, transport = real_vip
    def no_ai(*args, **kwargs):
        raise AssertionError('Dashboard navigation must not invoke AI')
    monkeypatch.setattr(server, 'get_ai_response_with_memory', no_ai)
    card=server.build_dashboard_flex(uid)
    assert card is not None
    payload=card.as_json_dict()
    rendered=[node['action'] for node in _walk(payload) if node.get('type')=='button']
    assert [a['label'] for a in rendered]==['記一餐','今日明細','功能選單']
    actions = {a['label']:a['text'] for a in rendered}
    assert actions == {
        "記一餐": "我要紀錄飲食",
        "今日明細": "我要修改飲食紀錄",
        "功能選單": "功能選單",
    }

    with sqlite3.connect(db_path) as conn:
        before = (
            conn.execute(
                "SELECT remaining_chat_quota,remaining_meals FROM usage WHERE user_id=?",
                (uid,),
            ).fetchone(),
            conn.execute("SELECT COUNT(*) FROM food_logs WHERE user_id=?", (uid,)).fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM daily_food_log_events WHERE user_id=?", (uid,)
            ).fetchone()[0],
        )

    for number, label in enumerate(("記一餐", "今日明細", "功能選單"), 1):
        server.handle_message(_event(number, actions[label], uid))

    assert len(transport.replies) == 3
    add_meal = transport.replies[0][1].as_json_dict()
    details = transport.replies[1][1].as_json_dict()
    menu = transport.replies[2][1].as_json_dict()
    assert add_meal["type"] == "text"
    assert "餐別、餐點和份量" in add_meal["text"]
    assert details["type"] == "flex"
    assert "今天的紀錄" in json.dumps(details, ensure_ascii=False)
    assert menu["type"] == "flex"
    assert menu["altText"] == "功能選單"

    with sqlite3.connect(db_path) as conn:
        after = (
            conn.execute(
                "SELECT remaining_chat_quota,remaining_meals FROM usage WHERE user_id=?",
                (uid,),
            ).fetchone(),
            conn.execute("SELECT COUNT(*) FROM food_logs WHERE user_id=?", (uid,)).fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM daily_food_log_events WHERE user_id=?", (uid,)
            ).fetchone()[0],
        )
    assert after == before
    transport.replies.clear()
    for number, text in enumerate(actions.values(), 101):
        server.handle_message(_event(number, text, 'U-NONVIP-BUTTON-NEGATIVE'))
    assert transport.replies == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM usage WHERE user_id=?', ('U-NONVIP-BUTTON-NEGATIVE',)).fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM food_logs WHERE user_id=?', ('U-NONVIP-BUTTON-NEGATIVE',)).fetchone()[0] == 0
