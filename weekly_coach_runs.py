"""每週課表：同一人同一週只呼叫一次 AI、只寫一次 Sheet。

問題（2026-11 整修前）：
- LINE 指令、排程 API、LINE webhook 重送，每次都會重新呼叫 AI 並重寫 Sheet。
- run_weekly_coach 成功時回傳 (訊息, dict)，排程 API 把它當 (success, 字串) 用，
  切片 dict 丟例外 → 回 500 → 排程重試 → 再跑一次 AI 與寫入。
- 函式內已推播，LINE 指令處理又推播一次，客人收到兩則。

做法：SQLite 表 weekly_coach_runs 以 (user_id, week_start) 為主鍵。
- claim()：搶到才可以跑；已完成回 done（附上次結果）；別人正在跑回 running。
- 跑超過 STALE_SECONDS 還沒完成視為當掉，可被重新搶走。
- force=True（「重排下週課表」）可覆蓋已完成的結果，但仍擋同時進行中的。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

STALE_SECONDS = 300
_TS = "%Y-%m-%d %H:%M:%S"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS weekly_coach_runs (
               user_id TEXT NOT NULL,
               week_start TEXT NOT NULL,
               status TEXT NOT NULL,
               started_at TEXT NOT NULL,
               finished_at TEXT DEFAULT '',
               line_message TEXT DEFAULT '',
               plan_json TEXT DEFAULT '',
               PRIMARY KEY (user_id, week_start)
           )"""
    )


def claim(db_path: str, user_id: str, week_start: str, now: datetime, *, force: bool = False) -> dict:
    """回傳 {"state": "claimed"|"done"|"running", "line_message", "daily_plan"}。"""
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status, started_at, line_message, plan_json FROM weekly_coach_runs "
                "WHERE user_id=? AND week_start=?",
                (user_id, week_start),
            ).fetchone()
            if row:
                status, started_at, line_message, plan_json = row
                if status == "done" and not force:
                    conn.execute("ROLLBACK")
                    try:
                        plan = json.loads(plan_json) if plan_json else {}
                    except ValueError:
                        plan = {}
                    return {"state": "done", "line_message": line_message or "", "daily_plan": plan}
                if status == "running":
                    try:
                        fresh = now - datetime.strptime(started_at, _TS) < timedelta(seconds=STALE_SECONDS)
                    except ValueError:
                        fresh = False
                    if fresh:
                        conn.execute("ROLLBACK")
                        return {"state": "running", "line_message": "", "daily_plan": {}}
            conn.execute(
                "INSERT INTO weekly_coach_runs (user_id, week_start, status, started_at) "
                "VALUES (?,?,'running',?) "
                "ON CONFLICT(user_id, week_start) DO UPDATE SET status='running', started_at=excluded.started_at, "
                "finished_at=''",
                (user_id, week_start, now.strftime(_TS)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"state": "claimed", "line_message": "", "daily_plan": {}}


def finish(db_path: str, user_id: str, week_start: str, now: datetime, line_message: str, daily_plan: dict) -> None:
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        ensure_schema(conn)
        with conn:
            conn.execute(
                "UPDATE weekly_coach_runs SET status='done', finished_at=?, line_message=?, plan_json=? "
                "WHERE user_id=? AND week_start=?",
                (now.strftime(_TS), line_message, json.dumps(daily_plan or {}, ensure_ascii=False),
                 user_id, week_start),
            )


def release(db_path: str, user_id: str, week_start: str) -> None:
    """失敗時放掉佔位，下次可以重跑。"""
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        ensure_schema(conn)
        with conn:
            conn.execute(
                "DELETE FROM weekly_coach_runs WHERE user_id=? AND week_start=? AND status='running'",
                (user_id, week_start),
            )
