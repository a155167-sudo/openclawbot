from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class RewardReservation:
    status: str
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryLease:
    status: str
    links: tuple[str, ...] = ()
    token: str = ""


def _ensure_delivery_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS survey_reward_deliveries (
            user_id TEXT PRIMARY KEY,
            links_json TEXT NOT NULL,
            delivered_at TEXT,
            lease_token TEXT,
            lease_expires_at TEXT
        )
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(survey_reward_deliveries)")
    }
    if "lease_token" not in columns:
        conn.execute("ALTER TABLE survey_reward_deliveries ADD COLUMN lease_token TEXT")
    if "lease_expires_at" not in columns:
        conn.execute(
            "ALTER TABLE survey_reward_deliveries ADD COLUMN lease_expires_at TEXT"
        )


def try_acquire_survey_reward_delivery_lock(
    db_path: str,
    user_id: str,
) -> int | None:
    lock_dir = Path(db_path).resolve().parent / ".survey_reward_locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_name = hashlib.sha256(user_id.encode("utf-8")).hexdigest() + ".lock"
    fd = os.open(lock_dir / lock_name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def release_survey_reward_delivery_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def build_survey_invitation_message(survey_link: str, reward_count: int) -> str:
    if reward_count < 1:
        raise ValueError("reward_count must be at least 1")
    return (
        "🎁 感謝您對一日樂食的支持！\n"
        "請點擊下方專屬連結填寫滿意度調查 (約1分鐘)。\n\n"
        f"完成填寫後，系統將自動發送【{reward_count} 點集點卡點數】給您喔！👇\n\n"
        f"{survey_link}"
    )


def build_survey_reward_message(
    links: tuple[str, ...],
    *,
    points_per_link: int = 1,
) -> str:
    if not links:
        raise ValueError("at least one reward link is required")
    if points_per_link < 1:
        raise ValueError("points_per_link must be at least 1")

    link_count = len(links)
    point_count = link_count * points_per_link
    intro = (
        "🎉 感謝您的寶貴回饋！\n\n"
        f"這是答應您的專屬獎勵【一日樂食集點卡 {point_count} 點】。\n\n"
    )
    if link_count == 1:
        return (
            f"{intro}請點擊下方連結領取：\n{links[0]}\n\n"
            "⚠️ 此連結為專屬一次性連結，請勿轉發給他人。"
        )

    numbered_links = "\n\n".join(
        (
            f"第 {index} 點：\n{link}"
            if points_per_link == 1
            else f"第 {index} 個獎勵連結：\n{link}"
        )
        for index, link in enumerate(links, start=1)
    )
    click_instruction = (
        "兩個連結都要分別點擊"
        if link_count == 2
        else "所有連結都要分別點擊"
    )
    return (
        f"{intro}{numbered_links}\n\n"
        f"⚠️ {click_instruction}，才會完成 {point_count} 點領取。"
        "每個連結都是專屬一次性連結，請勿轉發給他人。"
    )


def acquire_survey_reward_delivery(
    db_path: str,
    user_id: str,
    *,
    now: str,
    lease_seconds: int = 60,
) -> DeliveryLease:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")
    now_dt = datetime.fromisoformat(now)
    if now_dt.tzinfo is None:
        raise ValueError("now must include timezone information")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_delivery_schema(conn)
        row = conn.execute(
            """
            SELECT links_json, delivered_at, lease_token, lease_expires_at
            FROM survey_reward_deliveries
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return DeliveryLease(status="missing")
        if row[1]:
            conn.rollback()
            return DeliveryLease(status="delivered")

        expires_at = datetime.fromisoformat(row[3]) if row[3] else None
        if row[2] and expires_at and expires_at > now_dt:
            conn.rollback()
            return DeliveryLease(status="in_progress")

        token = secrets.token_urlsafe(24)
        new_expiry = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        cursor = conn.execute(
            """
            UPDATE survey_reward_deliveries
            SET lease_token=?, lease_expires_at=?
            WHERE user_id=? AND delivered_at IS NULL
            """,
            (token, new_expiry, user_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return DeliveryLease(status="in_progress")
        links = tuple(str(link) for link in json.loads(row[0]))
        conn.commit()
        return DeliveryLease(status="acquired", links=links, token=token)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_survey_reward_delivery(
    db_path: str,
    user_id: str,
    token: str,
) -> bool:
    with sqlite3.connect(db_path, timeout=10) as conn:
        cursor = conn.execute(
            """
            UPDATE survey_reward_deliveries
            SET lease_token=NULL, lease_expires_at=NULL
            WHERE user_id=? AND lease_token=? AND delivered_at IS NULL
            """,
            (user_id, token),
        )
        return cursor.rowcount == 1


def mark_survey_reward_delivered(
    db_path: str,
    user_id: str,
    token: str,
    *,
    delivered_at: str,
) -> bool:
    with sqlite3.connect(db_path, timeout=10) as conn:
        cursor = conn.execute(
            """
            UPDATE survey_reward_deliveries
            SET delivered_at=?, lease_token=NULL, lease_expires_at=NULL
            WHERE user_id=? AND lease_token=? AND delivered_at IS NULL
            """,
            (delivered_at, user_id, token),
        )
        return cursor.rowcount == 1


def reserve_survey_reward_links(
    db_path: str,
    user_id: str,
    *,
    claim_date: str,
    reward_count: int = 2,
) -> RewardReservation:
    """Atomically reserve one-point reward links for a survey respondent."""
    if reward_count < 1:
        raise ValueError("reward_count must be at least 1")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_delivery_schema(conn)
        delivery = conn.execute(
            "SELECT links_json, delivered_at FROM survey_reward_deliveries WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if delivery:
            links = tuple(str(link) for link in json.loads(delivery[0]))
            conn.rollback()
            if delivery[1]:
                return RewardReservation(status="already_claimed")
            return RewardReservation(status="pending_delivery", links=links)

        existing_claim = conn.execute(
            "SELECT 1 FROM survey_records WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if existing_claim:
            conn.rollback()
            return RewardReservation(status="already_claimed")

        rows = conn.execute(
            "SELECT link FROM reward_links WHERE is_used=0 ORDER BY rowid LIMIT ?",
            (reward_count,),
        ).fetchall()
        links = tuple(str(row[0]) for row in rows)
        if len(links) != reward_count:
            conn.rollback()
            return RewardReservation(status="insufficient_stock")

        placeholders = ",".join("?" for _ in links)
        cursor = conn.execute(
            f"UPDATE reward_links SET is_used=1 "
            f"WHERE is_used=0 AND link IN ({placeholders})",
            links,
        )
        if cursor.rowcount != reward_count:
            conn.rollback()
            raise RuntimeError("reward link reservation conflict")

        conn.execute(
            "INSERT INTO survey_records (user_id, claim_date) VALUES (?, ?)",
            (user_id, claim_date),
        )
        conn.execute(
            "INSERT INTO survey_reward_deliveries (user_id, links_json) VALUES (?, ?)",
            (user_id, json.dumps(links)),
        )
        conn.commit()
        return RewardReservation(status="claimed", links=links)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
