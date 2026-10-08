import sqlite3
from line_webhook_inbox import LineWebhookInbox


def test_existing_staging_not_null_error_column_completes(tmp_path):
 db=tmp_path/'old-schema.db'
 with sqlite3.connect(db) as conn:
  conn.execute('''CREATE TABLE line_webhook_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_key TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL, signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',received_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '')''')
 inbox=LineWebhookInbox(str(db),lambda *_:None)
 key=inbox.receive(b'{"events":[]}','synthetic')
 inbox.activate_and_start(key)
 assert inbox.wait_idle()
 with sqlite3.connect(db) as conn:
  assert conn.execute('SELECT status,error FROM line_webhook_inbox').fetchone()==('finished','')
