import json

import pytest

from app_config import load_settings


LEGACY_ADMIN_UID = "Uefd72ca53a9a6ac39781fe673c398530"
LEGACY_LIFF_ID = "2009824277-W3lYtSjF"
LEGACY_SPREADSHEET_ID = "1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ"


def valid_google_credentials():
    return json.dumps({
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "test-key-id",
        "private_key": "test-private-key",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    })


def isolated_environment(app_env="staging"):
    return {
        "APP_ENV": app_env,
        "DATA_DIR": "/app/data",
        "PUBLIC_BASE_URL": f"https://{app_env}.example",
        "LINE_CHANNEL_ACCESS_TOKEN": f"{app_env}-token",
        "LINE_CHANNEL_SECRET": f"{app_env}-secret",
        "OPENAI_API_KEY": f"{app_env}-openai",
        "GOOGLE_CREDENTIALS": valid_google_credentials(),
        "MEAL_PHOTO_IMAGE_SECRET": "x" * 32,
        "ADMIN_SECRET": f"{app_env}-admin-secret",
        "FORM_WEBHOOK_SECRET": "f" * 32,
        "SURVEY_WEBHOOK_SECRET": "s" * 32,
        "ADMIN_UID": "U" + "1" * 32,
        "COACH_UIDS": "U" + "1" * 32,
        "LIFF_ID": "2000000000-" + app_env + "Liff",
        "SPREADSHEET_ID": f"{app_env}-sheet",
        "SUBSCRIPTION_FORM_URL_TEMPLATE": f"https://{app_env}.example/form?uid={{uid}}",
        "SURVEY_FORM_URL_TEMPLATE": f"https://{app_env}.example/survey?uid={{uid}}",
    }


def test_legacy_defaults_preserve_current_environment():
    settings = load_settings({})

    assert settings.app_env == "legacy"
    assert settings.enable_scheduler is True
    assert settings.public_base_url == "https://openclawbot-production-36ed.up.railway.app"
    assert settings.admin_uid == LEGACY_ADMIN_UID
    assert settings.coach_uids == (
        LEGACY_ADMIN_UID,
        "U9540c22cea2d6e0b1df8edbd9e3ebc41",
    )
    assert settings.liff_id == LEGACY_LIFF_ID
    assert settings.spreadsheet_id == LEGACY_SPREADSHEET_ID


def test_staging_environment_can_isolate_account_resources():
    settings = load_settings(
        {
            "APP_ENV": "staging",
            "ENABLE_SCHEDULER": "false",
            "DATA_DIR": "/app/data",
            "PUBLIC_BASE_URL": "https://staging.example",
            "LINE_CHANNEL_ACCESS_TOKEN": "staging-token",
            "LINE_CHANNEL_SECRET": "staging-secret",
            "OPENAI_API_KEY": "staging-openai",
            "GOOGLE_CREDENTIALS": valid_google_credentials(),
            "MEAL_PHOTO_IMAGE_SECRET": "x" * 32,
            "ADMIN_SECRET": "staging-admin-secret",
            "FORM_WEBHOOK_SECRET": "f" * 32,
            "SURVEY_WEBHOOK_SECRET": "s" * 32,
            "ADMIN_UID": "U" + "1" * 32,
            "COACH_UIDS": "U" + "1" * 32 + ", U" + "2" * 32,
            "LIFF_ID": "2000000000-stagingLiff",
            "SPREADSHEET_ID": "staging-sheet",
            "SUBSCRIPTION_FORM_URL_TEMPLATE": "https://example.test/form?uid={uid}",
            "SURVEY_FORM_URL_TEMPLATE": "https://example.test/survey?uid={uid}",
        }
    )

    assert settings.app_env == "staging"
    assert settings.enable_scheduler is False
    assert settings.admin_uid == "U" + "1" * 32
    assert settings.coach_uids == ("U" + "1" * 32, "U" + "2" * 32)
    assert settings.liff_id == "2000000000-stagingLiff"
    assert settings.spreadsheet_id == "staging-sheet"
    assert settings.public_base_url == "https://staging.example"
    assert settings.subscription_form_url("U abc") == "https://example.test/form?uid=U%20abc"
    assert settings.survey_form_url("U abc") == "https://example.test/survey?uid=U%20abc"


def test_staging_defaults_scheduler_to_disabled():
    assert load_settings(isolated_environment()).enable_scheduler is False


def test_production_defaults_scheduler_to_enabled():
    assert load_settings(isolated_environment("production")).enable_scheduler is True


@pytest.mark.parametrize(
    "missing_name",
    [
        "ADMIN_UID",
        "ADMIN_SECRET",
        "COACH_UIDS",
        "DATA_DIR",
        "FORM_WEBHOOK_SECRET",
        "GOOGLE_CREDENTIALS",
        "LIFF_ID",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_SECRET",
        "MEAL_PHOTO_IMAGE_SECRET",
        "OPENAI_API_KEY",
        "PUBLIC_BASE_URL",
        "SPREADSHEET_ID",
        "SUBSCRIPTION_FORM_URL_TEMPLATE",
        "SURVEY_WEBHOOK_SECRET",
        "SURVEY_FORM_URL_TEMPLATE",
    ],
)
def test_named_environment_requires_explicit_isolated_resources(missing_name):
    environ = isolated_environment()
    environ.pop(missing_name)

    with pytest.raises(ValueError, match=missing_name):
        load_settings(environ)


@pytest.mark.parametrize("value", ["maybe", "2", "enabled"])
def test_invalid_scheduler_boolean_fails_closed(value):
    with pytest.raises(ValueError, match="ENABLE_SCHEDULER"):
        load_settings({"ENABLE_SCHEDULER": value})


def test_invalid_app_environment_is_rejected():
    with pytest.raises(ValueError, match="APP_ENV"):
        load_settings({"APP_ENV": "prodution"})


def test_railway_public_domain_is_accepted_as_public_base_url():
    environ = isolated_environment()
    environ.pop("PUBLIC_BASE_URL")
    environ["RAILWAY_PUBLIC_DOMAIN"] = "service.example.railway.app"

    settings = load_settings(environ)

    assert settings.public_base_url == "https://service.example.railway.app"


def test_form_template_must_contain_uid_placeholder():
    with pytest.raises(ValueError, match="SUBSCRIPTION_FORM_URL_TEMPLATE"):
        load_settings({"SUBSCRIPTION_FORM_URL_TEMPLATE": "https://example.test/form"})


def test_named_environment_rejects_invalid_admin_uid():
    environ = isolated_environment()
    environ["ADMIN_UID"] = "jason"

    with pytest.raises(ValueError, match="ADMIN_UID"):
        load_settings(environ)


def test_named_environment_rejects_invalid_coach_uid():
    environ = isolated_environment()
    environ["COACH_UIDS"] = "U" + "1" * 32 + ",bad"

    with pytest.raises(ValueError, match="COACH_UIDS"):
        load_settings(environ)


def test_form_templates_must_use_https():
    environ = isolated_environment()
    environ["SUBSCRIPTION_FORM_URL_TEMPLATE"] = "http://example.test/form?uid={uid}"

    with pytest.raises(ValueError, match="SUBSCRIPTION_FORM_URL_TEMPLATE"):
        load_settings(environ)


def test_public_base_url_must_be_https_origin_without_path():
    environ = isolated_environment()
    environ["PUBLIC_BASE_URL"] = "https://production.example/wrong-path"

    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        load_settings(environ)


@pytest.mark.parametrize("name", ["FORM_WEBHOOK_SECRET", "SURVEY_WEBHOOK_SECRET"])
def test_named_environment_rejects_short_webhook_secret(name):
    environ = isolated_environment()
    environ[name] = "too-short"

    with pytest.raises(ValueError, match=name):
        load_settings(environ)


def test_legacy_preserves_previous_public_base_url_acceptance():
    settings = load_settings({"PUBLIC_BASE_URL": "http://localhost:8000/dev/"})

    assert settings.public_base_url == "http://localhost:8000/dev"


@pytest.mark.parametrize(
    "marker",
    [
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_PUBLIC_DOMAIN",
    ],
)
def test_railway_deployment_requires_explicit_app_env(marker):
    with pytest.raises(ValueError, match="APP_ENV"):
        load_settings({marker: "railway-value"})


def test_named_environment_rejects_malformed_google_credentials():
    environ = isolated_environment()
    environ["GOOGLE_CREDENTIALS"] = "not-json"

    with pytest.raises(ValueError, match="GOOGLE_CREDENTIALS"):
        load_settings(environ)


def test_named_environment_rejects_incomplete_google_credentials():
    environ = isolated_environment()
    environ["GOOGLE_CREDENTIALS"] = "{}"

    with pytest.raises(ValueError, match="GOOGLE_CREDENTIALS"):
        load_settings(environ)


def test_named_environment_rejects_javascript_liff_injection():
    environ = isolated_environment()
    environ["LIFF_ID"] = '\";alert(document.domain);//'

    with pytest.raises(ValueError, match="LIFF_ID"):
        load_settings(environ)
