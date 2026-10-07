"""Shared chat-budget reads/spends on the caller's real SQLite transaction.

Reservations never debit usage. Every actual consumer must respect outstanding
unknown general-AI claims; status lookups are read-only and show persisted balance.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class ChatBudget:
    authorized: bool
    balance: int
    effective: int
    meals: int
    reserved: int

    @property
    def available(self):
        return max(self.effective-self.reserved,0)


def outstanding_chat_reservations(conn,user_id):
    exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_consultation_operations'").fetchone()
    if not exists:
        return 0
    return int(conn.execute("SELECT count(*) FROM ai_consultation_operations WHERE user_id=? AND state='processing'",(user_id,)).fetchone()[0])


def read_chat_budget(conn,user_id,today):
    row=conn.execute("SELECT remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit FROM usage WHERE user_id=?",(user_id,)).fetchone()
    if row is None:
        return ChatBudget(False,0,0,0,0)
    q,meals,last_date,status,expiry,daily=row
    try:
        balance=int(q)
        meals=int(meals)
        effective=int(daily if last_date!=today else q)
    except (TypeError,ValueError):
        return ChatBudget(False,0,0,0,0)
    authorized=(status=='vip' and isinstance(expiry,str) and bool(expiry) and today<=expiry and meals>0)
    reserved=outstanding_chat_reservations(conn,user_id)
    return ChatBudget(authorized,max(balance,0),max(effective,0),meals,reserved)


def spend_unreserved_chat_credit(conn,user_id,today):
    """Caller MUST hold BEGIN IMMEDIATE through this read/update/commit."""
    if not conn.in_transaction:
        raise RuntimeError('shared chat spending requires an owned transaction')
    budget=read_chat_budget(conn,user_id,today)
    if not budget.authorized or budget.available<=0:
        return False,budget
    final=budget.effective-1
    conn.execute("UPDATE usage SET remaining_chat_quota=?,last_date=? WHERE user_id=?",(final,today,user_id))
    return True,ChatBudget(True,final,final,budget.meals,budget.reserved)


def _charge_text_meal_estimate_quota(conn, *, user_id, token, attempt_id, now_text):
    """Restore the source attempt-journal boundary using shared capacity checks."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if not conn.in_transaction:
        raise RuntimeError('text estimation requires an owned transaction')
    conn.execute("""CREATE TABLE IF NOT EXISTS text_meal_estimate_quota_ledger
        (attempt_id TEXT PRIMARY KEY,token TEXT NOT NULL,user_id TEXT NOT NULL,
         status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
    existing=conn.execute('SELECT user_id,token,status FROM text_meal_estimate_quota_ledger WHERE attempt_id=?',(attempt_id,)).fetchone()
    if existing:
        return existing==(user_id,token,'charged')
    today=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
    allowed,_=spend_unreserved_chat_credit(conn,user_id,today)
    if not allowed:
        return False
    conn.execute("""INSERT INTO text_meal_estimate_quota_ledger
        (attempt_id,token,user_id,status,created_at,updated_at)
        VALUES (?,?,?,'charged',?,?)""",(attempt_id,token,user_id,now_text,now_text))
    _ensure_quota_epochs(conn)
    conn.execute('INSERT INTO text_meal_estimate_quota_epoch VALUES(?,?,?,?)',(attempt_id,user_id,_quota_generation(conn,user_id),today))
    return True


def _refund_text_meal_estimate_quota(conn, *, user_id, attempt_id, now_text):
    return refund_owned_chat_attempt(conn,user_id=user_id,attempt_id=attempt_id,now_text=now_text)


def _quota_generation(conn,user_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_quota_generations'").fetchone():
        return 0
    row=conn.execute('SELECT generation FROM chat_quota_generations WHERE user_id=?',(user_id,)).fetchone()
    return row[0] if row else 0


def _ensure_quota_epochs(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS chat_quota_generations(user_id TEXT PRIMARY KEY,generation INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS text_meal_estimate_quota_epoch(attempt_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,generation INTEGER NOT NULL,business_date TEXT NOT NULL)')


def chat_attempt_has_provider_start(conn, *, user_id, attempt_id):
    """Caller transaction sees child starts by their actual quota owner."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_meal_provider_attempts'").fetchone():
        return False
    return bool(conn.execute(
        """SELECT 1 FROM text_meal_provider_attempts
           WHERE user_id=? AND quota_attempt_id=?
             AND (state IN ('provider_started','unknown','completed')
                  OR COALESCE(provider_started_at,'')!='') LIMIT 1""",
        (user_id,attempt_id),
    ).fetchone())


def refund_owned_chat_attempt(conn, *, user_id, attempt_id, now_text, after_provider_failure=False):
    """Refund one owned debit at most once.

    Policy (2026-10-07, owner decision): an AI estimate that failed, timed out or
    whose worker crashed is refunded so the customer can retry. Callers pass
    after_provider_failure=True only on a failure path where no estimate was
    committed for the customer; the ledger charged->refunded transition keeps
    the refund single-shot.
    """
    if not conn.in_transaction:
        raise RuntimeError('refund requires an owned transaction')
    if not after_provider_failure and chat_attempt_has_provider_start(conn,user_id=user_id,attempt_id=attempt_id):
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_meal_estimate_quota_ledger'").fetchone():
        return False
    _ensure_quota_epochs(conn)
    epoch=conn.execute('SELECT generation,business_date FROM text_meal_estimate_quota_epoch WHERE attempt_id=? AND user_id=?',(attempt_id,user_id)).fetchone()
    refunded=conn.execute("UPDATE text_meal_estimate_quota_ledger SET status='refunded',updated_at=? WHERE attempt_id=? AND user_id=? AND status='charged'",(now_text,attempt_id,user_id))
    if refunded.rowcount!=1:
        return False
    # Unknown historical debits or replaced/rolled-over budgets never mint credit.
    if epoch and epoch[0]==_quota_generation(conn,user_id):
        conn.execute('UPDATE usage SET remaining_chat_quota=MIN(COALESCE(daily_chat_limit,remaining_chat_quota+1),remaining_chat_quota+1) WHERE user_id=? AND last_date=?',(user_id,epoch[1]))
    return True


def replace_chat_entitlement(conn,user_id,*,balance,meals,today,status,expiry,daily_limit):
    if not conn.in_transaction:
        raise RuntimeError('entitlement replacement requires an owned transaction')
    if outstanding_chat_reservations(conn,user_id):
        return False
    _ensure_quota_epochs(conn)
    conn.execute('INSERT INTO chat_quota_generations VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1',(user_id,))
    conn.execute('INSERT OR REPLACE INTO usage(user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit) VALUES(?,?,?,?,?,?,?)',(user_id,balance,meals,today,status,expiry,daily_limit))
    return True
