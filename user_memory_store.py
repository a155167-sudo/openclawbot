"""對話記憶（user_memory）持久化：存 SQLite，重新部署不會忘記，並有大小上限。

原本是 `user_memory = {}`：每次部署／重啟就清空，而且沒有上限，會隨客人數一直長。
這個類別保留 dict 的用法（get／[]／in／del／pop），呼叫端不用改。

上限：
- 每人只留最近 MAX_MESSAGES 則（一問一答算兩則）。
- 每則最多 MAX_CHARS 字，超過截斷。
- 超過 TTL_DAYS 沒互動的記憶視為過期，讀不到並在寫入時順手清掉。
- 全部最多 MAX_USERS 人，超過刪最舊的。
寫入失敗不影響回覆（記憶是輔助，不是必要資料）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import closing

MAX_MESSAGES = 12
MAX_CHARS = 2000
TTL_DAYS = 7
MAX_USERS = 5000
_ROLES = ("user", "assistant", "system")


def _clean(history) -> list[dict]:
    out = []
    for item in list(history or [])[-MAX_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in _ROLES:
            continue
        out.append({"role": role, "content": str(item.get("content") or "")[:MAX_CHARS]})
    return out


class PersistentUserMemory(MutableMapping):
    def __init__(self, db_path: Callable[[], str], *, clock: Callable[[], float] = time.time):
        self._db_path = db_path
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), timeout=10)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_memory (
                   user_id TEXT PRIMARY KEY,
                   history_json TEXT NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        return conn

    def _cutoff(self) -> float:
        return self._clock() - TTL_DAYS * 86400

    def __getitem__(self, user_id) -> list[dict]:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT history_json FROM user_memory WHERE user_id=? AND updated_at>=?",
                    (str(user_id), self._cutoff()),
                ).fetchone()
        except sqlite3.Error as exc:
            print(f"⚠️ user_memory 讀取失敗: {type(exc).__name__}")
            raise KeyError(user_id) from exc
        if not row:
            raise KeyError(user_id)
        try:
            return _clean(json.loads(row[0]))
        except ValueError as exc:
            raise KeyError(user_id) from exc

    def __setitem__(self, user_id, history) -> None:
        payload = json.dumps(_clean(history), ensure_ascii=False)
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO user_memory (user_id, history_json, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET history_json=excluded.history_json, "
                    "updated_at=excluded.updated_at",
                    (str(user_id), payload, self._clock()),
                )
                conn.execute("DELETE FROM user_memory WHERE updated_at<?", (self._cutoff(),))
                conn.execute(
                    "DELETE FROM user_memory WHERE user_id IN ("
                    "SELECT user_id FROM user_memory ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
                    (MAX_USERS,),
                )
        except sqlite3.Error as exc:
            print(f"⚠️ user_memory 寫入失敗（不影響回覆）: {type(exc).__name__}")

    def __delitem__(self, user_id) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                cur = conn.execute("DELETE FROM user_memory WHERE user_id=?", (str(user_id),))
        except sqlite3.Error as exc:
            raise KeyError(user_id) from exc
        if cur.rowcount == 0:
            raise KeyError(user_id)

    def __iter__(self) -> Iterator[str]:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT user_id FROM user_memory WHERE updated_at>=?", (self._cutoff(),)
                ).fetchall()
        except sqlite3.Error:
            rows = []
        return iter([row[0] for row in rows])

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, user_id) -> bool:
        try:
            self[user_id]
        except KeyError:
            return False
        return True
