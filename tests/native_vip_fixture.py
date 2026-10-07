"""Explicit, native temporary VIP fixtures; no authorization or debit mocks."""
import sqlite3


def install_vip(db, *, uid, today, quota=20):
    with sqlite3.connect(db) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS usage (
            user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
            remaining_meals INTEGER, last_date TEXT, status TEXT,
            expiry_date TEXT, daily_chat_limit INTEGER)''')
        conn.execute('''INSERT OR REPLACE INTO usage
            (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
            VALUES (?,?,?,?,?,?,?)''',
            (uid, quota, 10, today, 'vip', '2099-12-31', quota))


def spy_native_debit(monkeypatch, server, calls):
    original = server._charge_text_meal_estimate_quota
    def debit(conn, **kwargs):
        calls.append(kwargs['user_id'])
        return original(conn, **kwargs)
    monkeypatch.setattr(server, '_charge_text_meal_estimate_quota', debit)
