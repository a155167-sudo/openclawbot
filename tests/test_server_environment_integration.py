import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


class HeaderRequest:
    def __init__(self, secret=None):
        self.headers = {}
        if secret is not None:
            self.headers["X-Webhook-Secret"] = secret


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
