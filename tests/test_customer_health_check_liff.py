from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def test_verify_line_id_token_uses_official_endpoint_and_returns_subject():
    from customer_health_check_liff import verify_line_id_token

    calls = []

    def fake_post(url, *, data, timeout):
        calls.append((url, data, timeout))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "iss": "https://access.line.me",
                "sub": "U1234567890abcdef1234567890abcdef",
                "aud": "2009251085",
            },
        )

    user_id = verify_line_id_token(
        "signed-id-token",
        channel_id="2009251085",
        http_post=fake_post,
    )

    assert user_id == "U1234567890abcdef1234567890abcdef"
    assert calls == [
        (
            "https://api.line.me/oauth2/v2.1/verify",
            {"id_token": "signed-id-token", "client_id": "2009251085"},
            5,
        )
    ]


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (400, {"error": "invalid_request"}),
        (200, {"iss": "https://evil.example", "sub": "U1234567890abcdef1234567890abcdef", "aud": "2009251085"}),
        (200, {"iss": "https://access.line.me", "sub": "U1234567890abcdef1234567890abcdef", "aud": "9999999999"}),
        (200, {"iss": "https://access.line.me", "sub": "not-a-line-user", "aud": "2009251085"}),
    ],
)
def test_verify_line_id_token_rejects_invalid_line_response_without_leaking_token(
    status_code, payload
):
    from customer_health_check_liff import LineAuthenticationError, verify_line_id_token

    secret_token = "secret-signed-id-token"

    def fake_post(_url, *, data, timeout):
        assert data["id_token"] == secret_token
        assert timeout == 5
        return SimpleNamespace(status_code=status_code, json=lambda: payload)

    with pytest.raises(LineAuthenticationError) as error:
        verify_line_id_token(
            secret_token,
            channel_id="2009251085",
            http_post=fake_post,
        )

    assert secret_token not in str(error.value)


def test_verify_line_id_token_marks_line_outage_as_temporarily_unavailable():
    import requests

    from customer_health_check_liff import (
        LineAuthenticationUnavailable,
        verify_line_id_token,
    )

    def timeout_post(_url, *, data, timeout):
        raise requests.Timeout("network timeout")

    with pytest.raises(LineAuthenticationUnavailable):
        verify_line_id_token(
            "signed-id-token",
            channel_id="2009251085",
            http_post=timeout_post,
        )

    def server_error_post(_url, *, data, timeout):
        return SimpleNamespace(status_code=503, json=lambda: {})

    with pytest.raises(LineAuthenticationUnavailable):
        verify_line_id_token(
            "signed-id-token",
            channel_id="2009251085",
            http_post=server_error_post,
        )

    for malformed_json in (
        lambda: ["not", "an", "object"],
        lambda: (_ for _ in ()).throw(ValueError("broken-json")),
    ):
        with pytest.raises(LineAuthenticationUnavailable):
            verify_line_id_token(
                "signed-id-token",
                channel_id="2009251085",
                http_post=lambda _url, *, data, timeout, payload=malformed_json: SimpleNamespace(
                    status_code=200,
                    json=payload,
                ),
            )


def _customer_liff_client(
    *, state, verifier=None, loader_error=None, raise_server_exceptions=True
):
    from customer_health_check_liff import create_customer_health_check_router

    seen = []

    def load_state(user_id):
        seen.append(user_id)
        if loader_error is not None:
            raise loader_error
        return state

    app = FastAPI()
    app.include_router(
        create_customer_health_check_router(
            liff_id="2009251085-customerCheckup",
            channel_id="2009251085",
            state_loader=load_state,
            token_verifier=verifier
            or (lambda _token, *, channel_id: "U1234567890abcdef1234567890abcdef"),
        )
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), seen


def test_customer_liff_page_uses_id_token_and_never_client_supplied_user_id():
    client, _seen = _customer_liff_client(state=None)

    response = client.get("/vip-health-check")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "2009251085-customerCheckup" in response.text
    assert "liff.getIDToken()" in response.text
    assert "Authorization" in response.text
    assert "Bearer" in response.text
    assert "liff.getProfile" not in response.text
    assert "userId=" not in response.text
    for status in (
        "collecting",
        "ready_for_review",
        "needs_more_info",
        "approved_pending_delivery",
        "delivery_failed",
        "delivered",
        "expired",
        "cancelled",
    ):
        assert status in response.text
    for impossible_status in ("ready_for_ai", "ai_processing", "dietitian_review"):
        assert impossible_status not in response.text


def test_customer_state_api_requires_bearer_token():
    client, seen = _customer_liff_client(state=None)

    response = client.get("/api/vip-health-check/me")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert seen == []


def test_customer_state_api_reads_only_verified_subject_and_ignores_query_uid():
    state = {
        "case_id": "vhc_customer",
        "status": "collecting",
        "valid_day_count": 2,
        "window_started_at": "2026-09-09T09:00:00+08:00",
        "window_ends_at": "2026-09-16T09:00:00+08:00",
        "report_published_at": "",
        "report": None,
    }
    client, seen = _customer_liff_client(state=state)

    response = client.get(
        "/api/vip-health-check/me?user_id=Uffffffffffffffffffffffffffffffff",
        headers={"Authorization": "Bearer signed-id-token"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"eligible": True, "state": state}
    assert seen == ["U1234567890abcdef1234567890abcdef"]


def test_customer_state_api_returns_safe_empty_state_for_user_without_case():
    client, seen = _customer_liff_client(state=None)

    response = client.get(
        "/api/vip-health-check/me",
        headers={"Authorization": "Bearer signed-id-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"eligible": False, "state": None}
    assert seen == ["U1234567890abcdef1234567890abcdef"]


def test_customer_state_api_loader_failure_is_generic_and_never_cacheable():
    secret_detail = "private-report-json-decode-failed"
    client, seen = _customer_liff_client(
        state=None,
        loader_error=RuntimeError(secret_detail),
        raise_server_exceptions=False,
    )

    response = client.get(
        "/api/vip-health-check/me",
        headers={"Authorization": "Bearer signed-id-token"},
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert secret_detail not in response.text
    assert response.json() == {"detail": "健檢資料暫時無法載入"}
    assert seen == ["U1234567890abcdef1234567890abcdef"]


def test_customer_state_api_serialization_failure_is_generic_and_never_cacheable():
    client, seen = _customer_liff_client(
        state={"bad": object()},
        raise_server_exceptions=False,
    )

    response = client.get(
        "/api/vip-health-check/me",
        headers={"Authorization": "Bearer signed-id-token"},
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "健檢資料暫時無法載入"}
    assert "object" not in response.text
    assert seen == ["U1234567890abcdef1234567890abcdef"]


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [("auth", 401), ("unavailable", 503), ("unexpected", 503)],
)
def test_customer_state_api_fails_closed_before_loading_health_data(
    failure,
    expected_status,
):
    from customer_health_check_liff import (
        LineAuthenticationError,
        LineAuthenticationUnavailable,
    )

    def reject(_token, *, channel_id):
        if failure == "auth":
            raise LineAuthenticationError("invalid")
        if failure == "unavailable":
            raise LineAuthenticationUnavailable("offline")
        raise RuntimeError("unexpected-private-verifier-error")

    client, seen = _customer_liff_client(
        state={"private": "must-not-load"},
        verifier=reject,
        raise_server_exceptions=failure != "unexpected",
    )
    response = client.get(
        "/api/vip-health-check/me",
        headers={"Authorization": "Bearer untrusted-token"},
    )

    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    assert seen == []
    assert "untrusted-token" not in response.text
    assert "private" not in response.text


def test_router_factory_rejects_liff_from_another_line_login_channel():
    from customer_health_check_liff import create_customer_health_check_router

    with pytest.raises(ValueError, match="不屬於"):
        create_customer_health_check_router(
            liff_id="2009251085-customerCheckup",
            channel_id="9999999999",
            state_loader=lambda _uid: None,
        )


def test_attach_customer_health_check_routes_is_absent_when_feature_disabled():
    from customer_health_check_liff import attach_customer_health_check_routes

    app = FastAPI()
    attached = attach_customer_health_check_routes(
        app,
        enabled=False,
        environ={},
        state_loader=lambda _user_id: None,
    )

    assert attached is False
    assert TestClient(app).get("/vip-health-check").status_code == 404
    assert TestClient(app).get("/api/vip-health-check/me").status_code == 404


def test_attach_customer_health_check_routes_fails_closed_when_enabled_config_missing():
    from customer_health_check_liff import attach_customer_health_check_routes

    with pytest.raises(ValueError, match="VIP_HEALTH_CHECK_LIFF_ID"):
        attach_customer_health_check_routes(
            FastAPI(),
            enabled=True,
            environ={},
            state_loader=lambda _user_id: None,
        )


@pytest.mark.parametrize(
    "environ",
    [
        {
            "VIP_HEALTH_CHECK_LIFF_ID": "2009251085-customerCheckup",
            "VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": "9999999999",
        },
        {
            "VIP_HEALTH_CHECK_LIFF_ID": "invalid-liff",
            "VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": "2009251085",
        },
    ],
)
def test_attach_customer_health_check_routes_rejects_mismatched_or_invalid_identity(
    environ,
):
    from customer_health_check_liff import attach_customer_health_check_routes

    with pytest.raises(ValueError):
        attach_customer_health_check_routes(
            FastAPI(),
            enabled=True,
            environ=environ,
            state_loader=lambda _user_id: None,
        )


def test_attach_customer_health_check_routes_registers_valid_dedicated_liff():
    from customer_health_check_liff import attach_customer_health_check_routes

    app = FastAPI()
    attached = attach_customer_health_check_routes(
        app,
        enabled=True,
        environ={
            "VIP_HEALTH_CHECK_LIFF_ID": "2009251085-customerCheckup",
            "VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": "2009251085",
        },
        state_loader=lambda _user_id: None,
        token_verifier=lambda _token, *, channel_id: "U1234567890abcdef1234567890abcdef",
    )

    assert attached is True
    assert TestClient(app).get("/vip-health-check").status_code == 200
