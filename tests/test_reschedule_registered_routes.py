import json
import sqlite3
from types import SimpleNamespace

import pytest

import server
from test_pair_reschedule_coordinator import ADMIN, NOW, OWNER, SOURCE, TARGET, open_db
from test_reschedule_service_integration import sheet_fixture
from test_gspread_pair_reschedule_adapter import master_row
from normal_reschedule_policy import ensure_normal_reschedule_anchors
from reschedule_service_integration import (
    submit_customer_pair_reschedule_pending, verify_customer_reschedule_context,
)


REQUEST_ID = "RS_7hQ2mK9p"


def _event(text, event_id, user_id):
    return SimpleNamespace(
        message=SimpleNamespace(id=event_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{event_id}",
        webhook_event_id=f"webhook-{event_id}",
    )


def _install_route_fixture(tmp_path, monkeypatch, *, enabled):
    db = tmp_path / "registered.sqlite3"
    conn, _ = open_db(db)
    conn.close()
    sheet, book = sheet_fixture()
    replies = []
    pushes = []
    factory_calls = []

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "PAIR_RESCHEDULE_ENABLED", enabled, raising=False)
    monkeypatch.setattr(server, "tw_now", lambda: NOW)
    monkeypatch.setattr(
        server,
        "RESCHEDULE_SHEET_ADAPTER_FACTORY",
        lambda conn, order_id: factory_calls.append(order_id) or sheet,
        raising=False,
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda token, message: replies.append((token, message.text)),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda target, message, **kwargs: pushes.append((target, message.text, kwargs)),
    )
    monkeypatch.setattr(
        server,
        "check_permission_and_quota",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("quota route reached")),
    )
    monkeypatch.setattr(
        server,
        "get_ai_response_with_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("AI route reached")),
    )
    server.processed_messages.clear()
    return db, book, replies, factory_calls, pushes


def test_pair_reschedule_setting_is_closed_enum_and_defaults_off():
    from app_config import load_settings

    assert load_settings({}).pair_reschedule_enabled is False
    assert load_settings({"PAIR_RESCHEDULE_ENABLED": "true"}).pair_reschedule_enabled is True
    with pytest.raises(ValueError, match="PAIR_RESCHEDULE_ENABLED"):
        load_settings({"PAIR_RESCHEDULE_ENABLED": "tru"})


def test_registered_commands_are_dark_before_db_sheet_quota_or_ai(tmp_path, monkeypatch):
    db, book, replies, factory_calls, pushes = _install_route_fixture(
        tmp_path, monkeypatch, enabled=False
    )
    before = db.read_bytes()
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        server.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled route touched SQLite")
        ),
    )

    server.handle_message(
        _event(
            f"#雙餐改期 1 {SOURCE} {TARGET} {REQUEST_ID}",
            "OFF-CUSTOMER",
            OWNER,
        )
    )
    server.handle_message(
        _event(f"#核准雙餐改期 {REQUEST_ID}", "OFF-ADMIN", ADMIN)
    )

    assert [text for _, text in replies] == [
        "雙餐改期功能目前未開放。",
        "雙餐改期功能目前未開放。",
    ]
    assert db.read_bytes() == before
    assert factory_calls == []
    assert pushes == []
    assert book.batch_calls == []
    with real_connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='customer_pair_reschedule_requests'"
        ).fetchone() is None


def test_registered_customer_to_admin_route_uses_real_sqlite_adapter_coordinator_and_replays_once(
    tmp_path, monkeypatch
):
    db, book, replies, factory_calls, pushes = _install_route_fixture(
        tmp_path, monkeypatch, enabled=True
    )
    customer_command = f"#雙餐改期 1 {SOURCE} {TARGET} {REQUEST_ID}"
    admin_command = f"#核准雙餐改期 {REQUEST_ID}"

    server.handle_message(_event(customer_command, "CUSTOMER-1", OWNER))
    server.handle_message(_event(customer_command, "CUSTOMER-REPLAY", OWNER))
    server.handle_message(_event(admin_command, "ADMIN-1", ADMIN))
    server.handle_message(_event(admin_command, "ADMIN-REPLAY", ADMIN))

    assert len(book.batch_calls) == 1
    assert factory_calls == [1]
    assert len(pushes) == 1
    assert pushes[0][0] == ADMIN
    assert pushes[0][2]["retry_key"]
    assert OWNER not in pushes[0][1]
    for needle in (
        "餐點改期待確認",
        f"申請編號：{REQUEST_ID}",
        f"日期：{SOURCE} → {TARGET}",
        "餐點：午餐A、晚餐B",
    ):
        assert needle in pushes[0][1]
    assert all(OWNER not in text and ADMIN not in text for _, text in replies)
    assert [text for _, text in replies] == [
        f"雙餐改期申請已建立，申請編號：{REQUEST_ID}。",
        f"雙餐改期申請已建立，申請編號：{REQUEST_ID}。",
        "雙餐改期已核准並完成。",
        "雙餐改期已核准並完成。",
    ]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM customer_pair_reschedule_requests"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()[0] == "confirmed"
        assert conn.execute(
            "SELECT admin_notification_status FROM customer_pair_reschedule_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()[0] == "delivered"
        assert conn.execute(
            "SELECT count(*) FROM pair_reschedule_snapshot_receipts"
        ).fetchone()[0] == 1


@pytest.mark.parametrize('original_expiry,accepted', [(TARGET, True), ('2026-10-26', False)])
def test_registered_admin_approval_with_normal_anchor_uses_shared_policy(tmp_path, monkeypatch, original_expiry, accepted):
    db, book, replies, factory_calls, _pushes = _install_route_fixture(
        tmp_path, monkeypatch, enabled=True
    )
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE subscription_service_calendar')
        conn.execute('''CREATE TABLE subscription_menu_entitlements(
            user_id TEXT PRIMARY KEY, order_id INTEGER, status TEXT, expires_on TEXT)''')
        conn.execute('INSERT INTO subscription_menu_entitlements VALUES(?,?,?,?)',
                     (OWNER, 1, 'active', original_expiry))
        conn.execute("UPDATE usage SET status='vip',remaining_meals=88,last_date='2026-09-25',expiry_date=?", (original_expiry,))
        conn.execute('UPDATE subscription_orders SET form_payload_json=? WHERE id=1',
                     (json.dumps({'master_api_rows': [master_row(SOURCE, lunch='午餐A', dinner='晚餐B')]}, ensure_ascii=False),))
        conn.commit()
        ensure_normal_reschedule_anchors(conn)
        context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
        submit_customer_pair_reschedule_pending(
            conn, context=context, source_date=SOURCE, target_date=TARGET,
            request_id=REQUEST_ID, now=NOW, feature_enabled=True)

    server.handle_message(_event(f'#核准雙餐改期 {REQUEST_ID}', 'NORMAL-ADMIN', ADMIN))

    assert replies[-1][1] == ('雙餐改期已核准並完成。' if accepted else '無法核准雙餐改期申請。')
    assert len(book.batch_calls) == int(accepted)
    if accepted:
        assert factory_calls == [1]
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM customer_pair_reschedule_requests WHERE request_id=?',
                            (REQUEST_ID,)).fetchone()[0] == ('confirmed' if accepted else 'pending_admin')


def test_registered_pair_only_inventory_rejects_before_any_sheet_read(
    tmp_path, monkeypatch
):
    db, _book, replies, factory_calls, _pushes = _install_route_fixture(
        tmp_path, monkeypatch, enabled=True
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workbook_writer_capabilities SET controlled_writers_json=?",
            ('["pair_reschedule"]',),
        )
        conn.commit()

    class NoSheetAccess:
        def __getattr__(self, name):
            raise AssertionError(f"Sheet access reached: {name}")

    monkeypatch.setattr(
        server, "RESCHEDULE_SHEET_ADAPTER_FACTORY",
        lambda conn, order_id: factory_calls.append(order_id) or NoSheetAccess(),
    )
    server.handle_message(_event(
        f"#雙餐改期 1 {SOURCE} {TARGET} {REQUEST_ID}", "PAIR-ONLY-CUSTOMER", OWNER,
    ))
    server.handle_message(_event(
        f"#核准雙餐改期 {REQUEST_ID}", "PAIR-ONLY-ADMIN", ADMIN,
    ))

    assert factory_calls == [1]
    assert replies[-1][1] == "無法核准雙餐改期申請。"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()[0] == "pending_admin"


def test_registered_routes_recheck_owner_and_admin_and_fail_closed_without_sheet(
    tmp_path, monkeypatch
):
    db, book, replies, factory_calls, _pushes = _install_route_fixture(
        tmp_path, monkeypatch, enabled=True
    )
    customer_command = f"#雙餐改期 1 {SOURCE} {TARGET} {REQUEST_ID}"

    server.handle_message(_event(customer_command, "WRONG-OWNER", "U" + "2" * 32))
    assert replies[-1][1] == "無法受理雙餐改期申請。"
    assert factory_calls == [] and book.batch_calls == []

    server.handle_message(_event(customer_command, "RIGHT-OWNER", OWNER))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE admin_settings SET value=? WHERE key='admin_id'", ("U" + "8" * 32,))
        conn.commit()
    server.handle_message(
        _event(f"#核准雙餐改期 {REQUEST_ID}", "REVOKED-ADMIN", ADMIN)
    )

    assert replies[-1][1] == "無法核准雙餐改期申請。"
    assert factory_calls == [] and book.batch_calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()[0] == "pending_admin"


@pytest.mark.parametrize(
    "text",
    [
        "#雙餐改期 1 09-27 2026-09-28 RS_7hQ2mK9p",
        "#雙餐改期 1 2026-09-27 2026-09-28 owner@example.com",
        "#雙餐改期 true 2026-09-27 2026-09-28 RS_7hQ2mK9p",
        "#核准雙餐改期",
        "#核准雙餐改期 RS bad",
    ],
)
def test_registered_parser_requires_explicit_year_integer_order_and_opaque_request_id(
    tmp_path, monkeypatch, text
):
    db, book, replies, factory_calls, pushes = _install_route_fixture(
        tmp_path, monkeypatch, enabled=True
    )
    before = db.read_bytes()

    server.handle_message(_event(text, "MALFORMED", OWNER))

    assert replies[-1][1] == "雙餐改期指令格式無效。"
    assert db.read_bytes() == before
    assert factory_calls == [] and book.batch_calls == []
    assert pushes == []
