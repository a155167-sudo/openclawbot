import asyncio
from dataclasses import replace
import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


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
    monkeypatch.setattr(server, "LIFF_ID", "production-liff-id")

    response = asyncio.run(server.coach_dashboard())

    assert 'liffId: "production-liff-id"' in response
    assert 'liffId: "2009824277-W3lYtSjF"' not in response


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
