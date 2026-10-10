import sqlite3
from concurrent.futures import ThreadPoolExecutor

from survey_rewards import (
    acquire_survey_reward_delivery,
    build_survey_invitation_message,
    build_survey_reward_message,
    mark_survey_reward_delivered,
    release_survey_reward_delivery,
    reserve_survey_reward_links,
)


def make_reward_db(path, links):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)")
        conn.executemany(
            "INSERT INTO reward_links (link, is_used) VALUES (?, 0)",
            [(link,) for link in links],
        )


def test_reserves_two_one_point_links_for_new_survey_claim(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(db_path, ["https://reward/1", "https://reward/2", "https://reward/3"])

    result = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    assert result.status == "claimed"
    assert result.links == ("https://reward/1", "https://reward/2")
    with sqlite3.connect(db_path) as conn:
        used = conn.execute(
            "SELECT link FROM reward_links WHERE is_used=1 ORDER BY link"
        ).fetchall()
        claim = conn.execute(
            "SELECT user_id, claim_date FROM survey_records"
        ).fetchone()
    assert used == [("https://reward/1",), ("https://reward/2",)]
    assert claim == ("U123", "2026-08-30")


def test_already_claimed_user_does_not_consume_more_links(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(db_path, ["https://reward/1", "https://reward/2"])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO survey_records (user_id, claim_date) VALUES (?, ?)",
            ("U123", "2026-08-29"),
        )

    result = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    assert result.status == "already_claimed"
    assert result.links == ()
    with sqlite3.connect(db_path) as conn:
        unused = conn.execute(
            "SELECT COUNT(*) FROM reward_links WHERE is_used=0"
        ).fetchone()[0]
    assert unused == 2


def test_pending_delivery_reuses_the_same_reserved_links(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(
        db_path,
        ["https://reward/1", "https://reward/2", "https://reward/3"],
    )
    first = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    retry = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    assert first.status == "claimed"
    assert retry.status == "pending_delivery"
    assert retry.links == first.links
    with sqlite3.connect(db_path) as conn:
        used_count = conn.execute(
            "SELECT COUNT(*) FROM reward_links WHERE is_used=1"
        ).fetchone()[0]
    assert used_count == 2


def test_delivery_lease_allows_only_one_concurrent_sender(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(db_path, ["https://reward/1", "https://reward/2"])
    reservation = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    def acquire():
        return acquire_survey_reward_delivery(
            str(db_path),
            "U123",
            now="2026-08-30T08:30:00+00:00",
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(lambda _: acquire(), range(2)))

    assert sorted(lease.status for lease in leases) == ["acquired", "in_progress"]
    acquired = next(lease for lease in leases if lease.status == "acquired")
    assert acquired.links == reservation.links
    assert acquired.token
    assert release_survey_reward_delivery(str(db_path), "U123", acquired.token) is True

    retry = acquire()
    assert retry.status == "acquired"
    assert retry.links == reservation.links
    assert retry.token != acquired.token


def test_delivered_claim_is_not_resent(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(db_path, ["https://reward/1", "https://reward/2"])
    reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    lease = acquire_survey_reward_delivery(
        str(db_path),
        "U123",
        now="2026-08-30T08:30:00+00:00",
    )
    marked = mark_survey_reward_delivered(
        str(db_path),
        "U123",
        lease.token,
        delivered_at="2026-08-30T16:30:00+08:00",
    )
    retry = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    assert marked is True
    assert retry.status == "already_claimed"
    assert retry.links == ()


def test_insufficient_stock_does_not_consume_partial_reward(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(db_path, ["https://reward/1"])

    result = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )

    assert result.status == "insufficient_stock"
    assert result.links == ()
    with sqlite3.connect(db_path) as conn:
        unused = conn.execute(
            "SELECT COUNT(*) FROM reward_links WHERE is_used=0"
        ).fetchone()[0]
        claims = conn.execute("SELECT COUNT(*) FROM survey_records").fetchone()[0]
    assert unused == 1
    assert claims == 0


def test_concurrent_claims_never_share_the_same_two_links(tmp_path):
    db_path = tmp_path / "quota.db"
    make_reward_db(db_path, ["https://reward/1", "https://reward/2"])

    def claim(user_id):
        return reserve_survey_reward_links(
            str(db_path),
            user_id,
            claim_date="2026-08-30",
            reward_count=2,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["U1", "U2"]))

    assert sorted(result.status for result in results) == [
        "claimed",
        "insufficient_stock",
    ]
    claimed = next(result for result in results if result.status == "claimed")
    assert set(claimed.links) == {"https://reward/1", "https://reward/2"}


def test_two_point_message_can_use_one_two_point_link():
    message = build_survey_reward_message(
        ("https://reward/1",),
        points_per_link=2,
    )

    assert "集點卡 2 點" in message
    assert "https://reward/1" in message
    assert "集點卡 1 點" not in message
    assert "第 2 點" not in message
    assert "兩個連結都要分別點擊" not in message


def test_two_point_message_contains_both_one_point_links():
    message = build_survey_reward_message(
        ("https://reward/1", "https://reward/2")
    )

    assert "集點卡 2 點" in message
    assert "第 1 點" in message
    assert "https://reward/1" in message
    assert "第 2 點" in message
    assert "https://reward/2" in message
    assert "兩個連結都要分別點擊" in message


def test_one_point_message_preserves_staging_behavior():
    message = build_survey_reward_message(("https://reward/1",))

    assert "集點卡 1 點" in message
    assert "https://reward/1" in message
    assert "第 2 點" not in message
    assert "兩個連結都要分別點擊" not in message


def test_survey_invitation_uses_environment_reward_count():
    staging = build_survey_invitation_message("https://survey/staging", 1)
    production = build_survey_invitation_message("https://survey/production", 2)

    assert "1 點集點卡點數" in staging
    assert "https://survey/staging" in staging
    assert "2 點集點卡點數" in production
    assert "1 點集點卡點數" not in production
    assert "https://survey/production" in production
