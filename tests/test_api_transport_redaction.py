from __future__ import annotations

import pytest

from api_client import ApiClient, ApiTransportError


class SecretLeakingTimeoutRequest:
    def post(self, *_args, **_kwargs):
        raise TimeoutError(
            "APIRequestContext.post timed out; authorization: SECRET_BEARER; "
            "cookie: SECRET_COOKIE"
        )


class SecretLeakingTransportRequest:
    def post(self, *_args, **_kwargs):
        raise OSError(
            "socket failed; authorization: SECRET_BEARER; cookie: SECRET_COOKIE"
        )


def _client(request) -> ApiClient:
    return ApiClient(
        request,
        {
            "endpoint_url": "https://example.invalid/graphql",
            "method": "POST",
            "payload": {},
            "shifts_path": "data.items",
            "field_map": {},
        },
        timeout_ms=10000,
        token_provider=lambda: "SECRET_BEARER",
    )


def test_poll_timeout_does_not_expose_playwright_call_log_secrets():
    with pytest.raises(ApiTransportError) as caught:
        _client(SecretLeakingTimeoutRequest()).fetch_shifts()

    text = str(caught.value)
    assert text == "API request timed out after 10000ms"
    assert "SECRET_BEARER" not in text
    assert "SECRET_COOKIE" not in text
    assert caught.value.safe_for_log is True
    assert caught.value.__suppress_context__ is True


def test_other_transport_failure_is_type_only_and_secret_free():
    with pytest.raises(ApiTransportError) as caught:
        _client(SecretLeakingTransportRequest()).fetch_shifts()

    text = str(caught.value)
    assert text == "API transport failed (OSError)"
    assert "SECRET_BEARER" not in text
    assert "SECRET_COOKIE" not in text
    assert caught.value.__suppress_context__ is True


def test_schedule_graphql_transport_uses_same_redaction_path():
    with pytest.raises(ApiTransportError) as caught:
        _client(SecretLeakingTimeoutRequest())._post_json({"query": "query Q { ok }"})

    assert str(caught.value) == "API request timed out after 10000ms"
