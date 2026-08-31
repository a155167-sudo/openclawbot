"""Environment-specific configuration for staging and production deployments."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Mapping
from urllib.parse import quote, urlsplit


LEGACY_ADMIN_UID = "Uefd72ca53a9a6ac39781fe673c398530"
LEGACY_COACH_UIDS = (
    LEGACY_ADMIN_UID,
    "U9540c22cea2d6e0b1df8edbd9e3ebc41",
)
LEGACY_LIFF_ID = "2009824277-W3lYtSjF"
LEGACY_SPREADSHEET_ID = "1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ"
LEGACY_SUBSCRIPTION_FORM_URL_TEMPLATE = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSfblmRmSc669n_C7JU1wja0g4KrEGs1oRQwdq6cfNCC8b1DFA/"
    "viewform?usp=pp_url&entry.1461831832={uid}"
)
LEGACY_SURVEY_FORM_URL_TEMPLATE = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLScF6Va_sdq6KMaKFd8BUVB2x5SyLji3JqX28-Z7h-tuLnpB-Q/"
    "viewform?usp=pp_url&entry.1048958109={uid}"
)
_ALLOWED_APP_ENVS = {"legacy", "staging", "production"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _parse_bool(name: str, value: str | None, *, default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} 必須是 true 或 false")


def _form_template(name: str, value: str) -> str:
    template = str(value or "").strip()
    if "{uid}" not in template:
        raise ValueError(f"{name} 必須包含 {{uid}}")
    parsed = urlsplit(template.replace("{uid}", "U" + "0" * 32))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{name} 必須是有效的 HTTPS 網址")
    return template


def _validate_line_uid(name: str, value: str) -> str:
    uid = str(value or "").strip()
    if not re.fullmatch(r"U[0-9a-fA-F]{32}", uid):
        raise ValueError(f"{name} 必須是有效的 LINE UID")
    return uid


def _validate_public_base_url(value: str) -> str:
    public_base_url = str(value or "").rstrip("/")
    parsed = urlsplit(public_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL 必須是沒有路徑的 HTTPS 網址")
    return public_base_url


@dataclass(frozen=True)
class AppSettings:
    app_env: str
    enable_scheduler: bool
    data_dir: str
    public_base_url: str
    admin_uid: str
    coach_uids: tuple[str, ...]
    liff_id: str
    spreadsheet_id: str
    form_webhook_secret: str
    survey_webhook_secret: str
    survey_reward_link_count: int
    survey_reward_points_per_link: int
    subscription_form_url_template: str
    survey_form_url_template: str

    def subscription_form_url(self, uid: str) -> str:
        return self.subscription_form_url_template.replace("{uid}", quote(str(uid), safe=""))

    def survey_form_url(self, uid: str) -> str:
        return self.survey_form_url_template.replace("{uid}", quote(str(uid), safe=""))


def load_settings(environ: Mapping[str, str]) -> AppSettings:
    explicit_app_env = str(environ.get("APP_ENV") or "").strip()
    railway_markers = (
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_PUBLIC_DOMAIN",
    )
    if not explicit_app_env and any(environ.get(name) for name in railway_markers):
        raise ValueError("Railway 部署必須明確設定 APP_ENV=staging 或 production")
    app_env = (explicit_app_env or "legacy").lower()
    if app_env not in _ALLOWED_APP_ENVS:
        raise ValueError(
            "APP_ENV 必須是 legacy、staging 或 production"
        )
    if app_env != "legacy":
        isolated_names = (
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
            "SPREADSHEET_ID",
            "SUBSCRIPTION_FORM_URL_TEMPLATE",
            "SURVEY_WEBHOOK_SECRET",
            "SURVEY_REWARD_LINK_COUNT",
            "SURVEY_FORM_URL_TEMPLATE",
        )
        missing = [
            name for name in isolated_names
            if not str(environ.get(name) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"APP_ENV={app_env} 缺少獨立環境設定：{', '.join(missing)}"
            )
        if not (
            str(environ.get("PUBLIC_BASE_URL") or "").strip()
            or str(environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        ):
            raise ValueError(
                f"APP_ENV={app_env} 缺少獨立環境設定：PUBLIC_BASE_URL 或 RAILWAY_PUBLIC_DOMAIN"
            )

    scheduler_default = app_env != "staging"
    enable_scheduler = _parse_bool(
        "ENABLE_SCHEDULER",
        environ.get("ENABLE_SCHEDULER"),
        default=scheduler_default,
    )

    admin_uid = _validate_line_uid(
        "ADMIN_UID", environ.get("ADMIN_UID") or LEGACY_ADMIN_UID
    )
    raw_coaches = str(environ.get("COACH_UIDS") or "").strip()
    coach_uids = (
        tuple(item.strip() for item in raw_coaches.split(",") if item.strip())
        if raw_coaches
        else LEGACY_COACH_UIDS
    )
    for coach_uid in coach_uids:
        _validate_line_uid("COACH_UIDS", coach_uid)

    liff_id = str(environ.get("LIFF_ID") or LEGACY_LIFF_ID).strip()
    if not re.fullmatch(r"[0-9]{5,}-[A-Za-z0-9_-]+", liff_id):
        raise ValueError("LIFF_ID 格式無效")

    if app_env != "legacy":
        try:
            google_credentials = json.loads(str(environ.get("GOOGLE_CREDENTIALS")))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("GOOGLE_CREDENTIALS 必須是有效 JSON") from exc
        required_google_fields = {
            "type", "project_id", "private_key_id", "private_key",
            "client_email", "token_uri",
        }
        if (
            not isinstance(google_credentials, dict)
            or google_credentials.get("type") != "service_account"
            or any(not google_credentials.get(name) for name in required_google_fields)
        ):
            raise ValueError("GOOGLE_CREDENTIALS 缺少必要的 service account 欄位")

    subscription_template = _form_template(
        "SUBSCRIPTION_FORM_URL_TEMPLATE",
        environ.get("SUBSCRIPTION_FORM_URL_TEMPLATE")
        or LEGACY_SUBSCRIPTION_FORM_URL_TEMPLATE,
    )
    survey_template = _form_template(
        "SURVEY_FORM_URL_TEMPLATE",
        environ.get("SURVEY_FORM_URL_TEMPLATE")
        or LEGACY_SURVEY_FORM_URL_TEMPLATE,
    )
    railway_domain = str(environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    raw_public_base_url = (
        environ.get("PUBLIC_BASE_URL")
        or (f"https://{railway_domain}" if railway_domain else "")
        or "https://openclawbot-production-36ed.up.railway.app"
    )
    public_base_url = (
        str(raw_public_base_url).rstrip("/")
        if app_env == "legacy"
        else _validate_public_base_url(raw_public_base_url)
    )
    form_webhook_secret = str(environ.get("FORM_WEBHOOK_SECRET") or "")
    survey_webhook_secret = str(environ.get("SURVEY_WEBHOOK_SECRET") or "")
    if app_env != "legacy":
        for name, secret in (
            ("FORM_WEBHOOK_SECRET", form_webhook_secret),
            ("SURVEY_WEBHOOK_SECRET", survey_webhook_secret),
        ):
            if len(secret.encode("utf-8")) < 32:
                raise ValueError(f"{name} 必須至少 32 bytes")

    raw_reward_count = str(environ.get("SURVEY_REWARD_LINK_COUNT") or "1").strip()
    try:
        survey_reward_link_count = int(raw_reward_count)
    except ValueError as exc:
        raise ValueError("SURVEY_REWARD_LINK_COUNT 必須是 1 到 10 的整數") from exc
    if not 1 <= survey_reward_link_count <= 10:
        raise ValueError("SURVEY_REWARD_LINK_COUNT 必須是 1 到 10 的整數")

    raw_points_per_link = str(
        environ.get("SURVEY_REWARD_POINTS_PER_LINK") or "2"
    ).strip()
    try:
        survey_reward_points_per_link = int(raw_points_per_link)
    except ValueError as exc:
        raise ValueError(
            "SURVEY_REWARD_POINTS_PER_LINK 必須是 1 到 100 的整數"
        ) from exc
    if not 1 <= survey_reward_points_per_link <= 100:
        raise ValueError("SURVEY_REWARD_POINTS_PER_LINK 必須是 1 到 100 的整數")

    return AppSettings(
        app_env=app_env,
        enable_scheduler=enable_scheduler,
        data_dir=str(environ.get("DATA_DIR") or "data").strip(),
        public_base_url=public_base_url,
        admin_uid=admin_uid,
        coach_uids=coach_uids,
        liff_id=liff_id,
        spreadsheet_id=str(
            environ.get("SPREADSHEET_ID") or LEGACY_SPREADSHEET_ID
        ).strip(),
        form_webhook_secret=form_webhook_secret,
        survey_webhook_secret=survey_webhook_secret,
        survey_reward_link_count=survey_reward_link_count,
        survey_reward_points_per_link=survey_reward_points_per_link,
        subscription_form_url_template=subscription_template,
        survey_form_url_template=survey_template,
    )
