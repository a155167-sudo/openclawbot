import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from survey_rewards import reserve_survey_reward_links

os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


class HeaderRequest:
    def __init__(self, secret=None):
        self.headers = {}
        if secret is not None:
            self.headers["X-Webhook-Secret"] = secret


class JsonRequest(HeaderRequest):
    def __init__(self, payload, secret=None):
        super().__init__(secret)
        self.payload = payload

    async def json(self):
        return self.payload


class RecordingLineBotApi:
    def __init__(self):
        self.pushes = []

    def push_message(self, user_id, message):
        self.pushes.append((user_id, message))


class FailFirstLineBotApi(RecordingLineBotApi):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def push_message(self, user_id, message):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("simulated LINE outage")
        super().push_message(user_id, message)


class BlockingLineBotApi(RecordingLineBotApi):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def push_message(self, user_id, message):
        with self._lock:
            self.pushes.append((user_id, message))
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release LINE push")


class BlockFirstLineBotApi(RecordingLineBotApi):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def push_message(self, user_id, message):
        with self._lock:
            self.pushes.append((user_id, message))
            call_number = len(self.pushes)
        if call_number == 1:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release first LINE push")


def test_webhook_secret_is_required_when_configured():
    server.require_webhook_secret(
        HeaderRequest("correct"), "correct", "FORM_WEBHOOK_SECRET"
    )

    for supplied in (None, "wrong"):
        with pytest.raises(server.HTTPException) as exc:
            server.require_webhook_secret(
                HeaderRequest(supplied), "correct", "FORM_WEBHOOK_SECRET"
            )
        assert exc.value.status_code == 401


def test_webhook_secret_remains_optional_in_legacy_mode():
    server.require_webhook_secret(HeaderRequest(), "", "FORM_WEBHOOK_SECRET")


def test_form_endpoints_reject_before_reading_body(monkeypatch):
    monkeypatch.setattr(server, "FORM_WEBHOOK_SECRET", "f" * 32)
    monkeypatch.setattr(server, "SURVEY_WEBHOOK_SECRET", "s" * 32)

    with pytest.raises(server.HTTPException) as form_exc:
        asyncio.run(
            server.receive_form_data(
                HeaderRequest(), server.BackgroundTasks()
            )
        )
    assert form_exc.value.status_code == 401

    with pytest.raises(server.HTTPException) as survey_exc:
        asyncio.run(server.receive_survey_data(HeaderRequest()))
    assert survey_exc.value.status_code == 401


def test_survey_endpoint_awards_two_one_point_links(monkeypatch, tmp_path):
    db_path = tmp_path / "quota.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE admin_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.executemany(
            "INSERT INTO reward_links (link, is_used) VALUES (?, 0)",
            [("https://reward/1",), ("https://reward/2",), ("https://reward/3",)],
        )

    line_api = RecordingLineBotApi()
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "SURVEY_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setattr(server, "SURVEY_REWARD_LINK_COUNT", 2)
    monkeypatch.setattr(server, "line_bot_api", line_api)

    response = asyncio.run(
        server.receive_survey_data(
            JsonRequest({"系統綁定碼 UID": "U123"}, "s" * 32)
        )
    )

    assert response == {"status": "success"}
    with sqlite3.connect(db_path) as conn:
        used = conn.execute(
            "SELECT link FROM reward_links WHERE is_used=1 ORDER BY link"
        ).fetchall()
        claim_count = conn.execute("SELECT COUNT(*) FROM survey_records").fetchone()[0]
    assert used == [("https://reward/1",), ("https://reward/2",)]
    assert claim_count == 1
    assert len(line_api.pushes) == 1
    user_id, message = line_api.pushes[0]
    assert user_id == "U123"
    assert "集點卡 2 點" in message.text
    assert "https://reward/1" in message.text
    assert "https://reward/2" in message.text
    retry = reserve_survey_reward_links(
        str(db_path),
        "U123",
        claim_date="2026-08-30",
        reward_count=2,
    )
    assert retry.status == "already_claimed"


def test_survey_endpoint_retries_same_links_after_line_push_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "quota.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE admin_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.executemany(
            "INSERT INTO reward_links (link, is_used) VALUES (?, 0)",
            [("https://reward/1",), ("https://reward/2",), ("https://reward/3",)],
        )

    line_api = FailFirstLineBotApi()
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "SURVEY_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setattr(server, "SURVEY_REWARD_LINK_COUNT", 2)
    monkeypatch.setattr(server, "line_bot_api", line_api)
    request = JsonRequest({"系統綁定碼 UID": "U123"}, "s" * 32)

    first_response = asyncio.run(server.receive_survey_data(request))
    second_response = asyncio.run(server.receive_survey_data(request))

    assert first_response == {"status": "error"}
    assert second_response == {"status": "success"}
    with sqlite3.connect(db_path) as conn:
        used = conn.execute(
            "SELECT link FROM reward_links WHERE is_used=1 ORDER BY link"
        ).fetchall()
        claim_count = conn.execute("SELECT COUNT(*) FROM survey_records").fetchone()[0]
    assert used == [("https://reward/1",), ("https://reward/2",)]
    assert claim_count == 1
    assert line_api.attempts == 2
    assert len(line_api.pushes) == 1
    message = line_api.pushes[0][1].text
    assert "https://reward/1" in message
    assert "https://reward/2" in message


def test_concurrent_duplicate_survey_callbacks_push_only_once(monkeypatch, tmp_path):
    db_path = tmp_path / "quota.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE admin_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.executemany(
            "INSERT INTO reward_links (link, is_used) VALUES (?, 0)",
            [("https://reward/1",), ("https://reward/2",), ("https://reward/3",)],
        )

    line_api = BlockingLineBotApi()
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "SURVEY_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setattr(server, "SURVEY_REWARD_LINK_COUNT", 2)
    monkeypatch.setattr(server, "line_bot_api", line_api)
    payload = {"uid": "U123"}

    def call_endpoint():
        return asyncio.run(
            server.receive_survey_data(JsonRequest(payload, "s" * 32))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call_endpoint)
        assert line_api.entered.wait(timeout=5)
        second = pool.submit(call_endpoint)
        try:
            second_result = second.result(timeout=5)
        finally:
            line_api.release.set()
        first_result = first.result(timeout=5)

    assert first_result == {"status": "success"}
    assert second_result == {"status": "delivery_in_progress"}
    assert len(line_api.pushes) == 1
    with sqlite3.connect(db_path) as conn:
        used_count = conn.execute(
            "SELECT COUNT(*) FROM reward_links WHERE is_used=1"
        ).fetchone()[0]
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM survey_records WHERE user_id='U123'"
        ).fetchone()[0]
    assert used_count == 2
    assert claim_count == 1


def test_active_sender_blocks_takeover_after_database_lease_expiry(monkeypatch, tmp_path):
    db_path = tmp_path / "quota.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE admin_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.executemany(
            "INSERT INTO reward_links (link, is_used) VALUES (?, 0)",
            [("https://reward/1",), ("https://reward/2",), ("https://reward/3",)],
        )

    line_api = BlockFirstLineBotApi()
    current_time = [datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "SURVEY_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setattr(server, "SURVEY_REWARD_LINK_COUNT", 2)
    monkeypatch.setattr(server, "line_bot_api", line_api)
    monkeypatch.setattr(server, "tw_now", lambda: current_time[0])
    payload = {"uid": "U123"}

    def call_endpoint():
        return asyncio.run(
            server.receive_survey_data(JsonRequest(payload, "s" * 32))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call_endpoint)
        assert line_api.entered.wait(timeout=5)
        current_time[0] += timedelta(seconds=61)
        second = pool.submit(call_endpoint)
        try:
            second_result = second.result(timeout=5)
        finally:
            line_api.release.set()
        first_result = first.result(timeout=5)

    assert first_result == {"status": "success"}
    assert second_result == {"status": "delivery_in_progress"}
    assert len(line_api.pushes) == 1


def test_survey_endpoint_preserves_one_point_staging_reward(monkeypatch, tmp_path):
    db_path = tmp_path / "quota.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE admin_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.executemany(
            "INSERT INTO reward_links (link, is_used) VALUES (?, 0)",
            [("https://reward/1",), ("https://reward/2",)],
        )

    line_api = RecordingLineBotApi()
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "SURVEY_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setattr(server, "SURVEY_REWARD_LINK_COUNT", 1)
    monkeypatch.setattr(server, "line_bot_api", line_api)

    response = asyncio.run(
        server.receive_survey_data(
            JsonRequest({"系統綁定碼 UID": "U123"}, "s" * 32)
        )
    )

    assert response == {"status": "success"}
    with sqlite3.connect(db_path) as conn:
        used_count = conn.execute(
            "SELECT COUNT(*) FROM reward_links WHERE is_used=1"
        ).fetchone()[0]
    assert used_count == 1
    assert len(line_api.pushes) == 1
    assert "集點卡 1 點" in line_api.pushes[0][1].text
    assert "集點卡 2 點" not in line_api.pushes[0][1].text


def test_server_source_contains_no_direct_legacy_account_resource_ids():
    source = Path(server.__file__).read_text(encoding="utf-8")
    forbidden = (
        "1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ",
        "2009824277-W3lYtSjF",
        "Uefd72ca53a9a6ac39781fe673c398530",
        "U9540c22cea2d6e0b1df8edbd9e3ebc41",
        "1FAIpQLSfblmRmSc669n_C7JU1wja0g4KrEGs1oRQwdq6cfNCC8b1DFA",
        "1FAIpQLScF6Va_sdq6KMaKFd8BUVB2x5SyLji3JqX28-Z7h-tuLnpB-Q",
    )

    for resource_id in forbidden:
        assert resource_id not in source


def test_server_constants_are_loaded_from_app_settings():
    assert server.APP_ENV == server.APP_SETTINGS.app_env
    assert server.ENABLE_SCHEDULER == server.APP_SETTINGS.enable_scheduler
    assert server.ADMIN_UID == server.APP_SETTINGS.admin_uid
    assert tuple(server.COACH_UIDS) == server.APP_SETTINGS.coach_uids
    assert server.LIFF_ID == server.APP_SETTINGS.liff_id
    assert server.SPREADSHEET_ID == server.APP_SETTINGS.spreadsheet_id
    assert server.SURVEY_REWARD_LINK_COUNT == server.APP_SETTINGS.survey_reward_link_count


def test_subscription_and_survey_links_use_environment_templates(monkeypatch):
    settings = replace(
        server.APP_SETTINGS,
        subscription_form_url_template="https://forms.test/monthly?uid={uid}",
        survey_form_url_template="https://forms.test/survey?uid={uid}",
    )
    monkeypatch.setattr(server, "APP_SETTINGS", settings)

    assert server.get_subscription_form_link("U ABC") == "https://forms.test/monthly?uid=U%20ABC"
    assert server.get_survey_form_link("U ABC") == "https://forms.test/survey?uid=U%20ABC"


def test_coach_dashboard_uses_configured_liff_id(monkeypatch):
    monkeypatch.setattr(server, "LIFF_ID", "2000000000-productionLiff")

    response = asyncio.run(server.coach_dashboard())

    assert 'liffId: "2000000000-productionLiff"' in response
    assert 'liffId: "2009824277-W3lYtSjF"' not in response


def test_coach_dashboard_json_encodes_liff_id(monkeypatch):
    malicious = '\";alert(document.domain);//'
    monkeypatch.setattr(server, "LIFF_ID", malicious)

    response = asyncio.run(server.coach_dashboard())

    assert f"liffId: {json.dumps(malicious)}" in response
    assert 'liffId: "";alert' not in response


def test_named_environment_requires_google_resource(monkeypatch):
    monkeypatch.setattr(server, "APP_ENV", "staging")
    with pytest.raises(RuntimeError, match="Google Sheets"):
        server.require_named_environment_resource("Google Sheets", None)


def test_legacy_allows_optional_google_resource(monkeypatch):
    monkeypatch.setattr(server, "APP_ENV", "legacy")
    server.require_named_environment_resource("Google Sheets", None)


def test_disabled_scheduler_registers_no_jobs_and_does_not_start(monkeypatch):
    class FakeScheduler:
        def __init__(self, **kwargs):
            self.jobs = []
            self.started = False
            self.shutdown_called = False

        def add_job(self, *args, **kwargs):
            self.jobs.append((args, kwargs))

        def start(self):
            self.started = True

        def shutdown(self):
            self.shutdown_called = True

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(server, "ENABLE_SCHEDULER", False, raising=False)
    monkeypatch.setattr(server, "BackgroundScheduler", lambda **kwargs: fake_scheduler)
    monkeypatch.setattr(server, "retry_pending_nutrition_plan_links", lambda: None)
    monkeypatch.setattr(server, "flush_nutrition_sheet_outbox", lambda: None)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)

    async def run_lifespan():
        async with server.lifespan(server.app):
            pass

    asyncio.run(run_lifespan())

    assert fake_scheduler.jobs == []
    assert fake_scheduler.started is False
    assert fake_scheduler.shutdown_called is False
