"""Tests for the read-only Codex rate-limit client."""

from __future__ import annotations

import base64
import json

from claude_swap import codex_usage


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _auth() -> dict:
    return {
        "tokens": {
            "access_token": "access-token",
            "id_token": _jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "account-from-claim",
                        "chatgpt_account_is_fedramp": True,
                    }
                }
            ),
        }
    }


def test_usage_url_matches_codex_backend_paths():
    assert codex_usage.usage_url("https://chatgpt.com") == (
        "https://chatgpt.com/backend-api/wham/usage"
    )
    assert codex_usage.usage_url("https://example.test/api") == (
        "https://example.test/api/api/codex/usage"
    )
    assert codex_usage.reset_credits_url("https://chatgpt.com") == (
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
    )


def test_fetch_usage_sends_codex_auth_headers_and_normalizes_windows(monkeypatch):
    requested = {}

    class Response:
        def read(self):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {"used_percent": 12.5, "reset_at": 1_800_000_000},
                        "secondary_window": {"used_percent": 65},
                    }
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, *, timeout):
        requested["url"] = request.full_url
        requested["headers"] = {name.lower(): value for name, value in request.header_items()}
        requested["timeout"] = timeout
        return Response()

    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)

    usage = codex_usage.fetch_codex_usage(_auth(), base_url="https://chatgpt.com")

    assert requested["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert requested["headers"] == {
        "authorization": "Bearer access-token",
        "user-agent": "ccswap",
        "accept": "application/json",
        "chatgpt-account-id": "account-from-claim",
        "x-openai-fedramp": "true",
    }
    assert usage == {
        "five_hour": {"pct": 12.5, "resets_at": "2027-01-15T08:00:00Z"},
        "weekly": {"pct": 65.0},
    }


def test_weekly_only_primary_is_classified_from_duration(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 42,
                            "limit_window_seconds": 7 * 24 * 60 * 60,
                            "reset_at": 1_800_000_000,
                        },
                        "secondary_window": None,
                    }
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        codex_usage.urllib.request, "urlopen", lambda request, *, timeout: Response()
    )

    assert codex_usage.fetch_codex_usage(_auth()) == {
        "weekly": {"pct": 42.0, "resets_at": "2027-01-15T08:00:00Z"}
    }


def test_fetch_usage_adds_credit_allowance_and_availability(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 28,
                            "limit_window_seconds": 604_800,
                        }
                    },
                    "credits": {
                        "has_credits": True,
                        "unlimited": False,
                        "overage_limit_reached": False,
                        "balance": None,
                    },
                    "spend_control": {
                        "reached": False,
                        "individual_limit": {
                            "limit": "5000",
                            "used": "321.55212020874023",
                            "remaining": "4678.44787979126",
                            "used_percent": 6,
                            "reset_at": 1_800_000_000,
                        },
                    },
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        codex_usage.urllib.request, "urlopen", lambda request, *, timeout: Response()
    )

    assert codex_usage.fetch_codex_usage(_auth()) == {
        "weekly": {"pct": 28.0},
        "credits": {
            "has_credits": True,
            "unlimited": False,
            "limit_reached": False,
        },
        "credit_allowance": {
            "limit": 5000.0,
            "used": 321.55212020874023,
            "remaining": 4678.44787979126,
            "pct": 6.0,
            "limit_reached": False,
            "resets_at": "2027-01-15T08:00:00Z",
        },
    }


def test_fetch_usage_keeps_credit_balance_when_no_individual_limit(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "rate_limit": {"primary_window": {"used_percent": 1}},
                    "credits": {
                        "has_credits": True,
                        "unlimited": False,
                        "balance": "2500",
                        "approx_local_messages": 125,
                    },
                    "spend_control": {"reached": False, "individual_limit": None},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        codex_usage.urllib.request, "urlopen", lambda request, *, timeout: Response()
    )

    usage = codex_usage.fetch_codex_usage(_auth())

    assert usage["credits"] == {
        "has_credits": True,
        "unlimited": False,
        "balance": 2500.0,
        "approx_local_messages": 125.0,
    }
    assert "credit_allowance" not in usage


def test_fetch_usage_accepts_credit_only_payload_and_preserves_no_credits(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "rate_limit": None,
                    "credits": {
                        "has_credits": False,
                        "unlimited": False,
                        "overage_limit_reached": False,
                        "balance": "0",
                    },
                    "spend_control": {"reached": False, "individual_limit": None},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        codex_usage.urllib.request, "urlopen", lambda request, *, timeout: Response()
    )

    assert codex_usage.fetch_codex_usage(_auth()) == {
        "credits": {
            "has_credits": False,
            "unlimited": False,
            "limit_reached": False,
            "balance": 0.0,
        }
    }


def test_fetch_usage_accepts_reached_allowance_without_rate_limit(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "spend_control": {"reached": True, "individual_limit": None},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        codex_usage.urllib.request, "urlopen", lambda request, *, timeout: Response()
    )

    assert codex_usage.fetch_codex_usage(_auth()) == {
        "credit_allowance": {"limit_reached": True}
    }


def test_fetch_usage_adds_banked_reset_count_and_earliest_expiry(monkeypatch):
    requested = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, *, timeout):
        requested.append(request)
        if request.full_url.endswith("/usage"):
            return Response(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 12,
                            "limit_window_seconds": 604_800,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 3},
                }
            )
        return Response(
            {
                "available_count": 3,
                "credits": [
                    {
                        "status": "available",
                        "expires_at": "2026-08-10T12:00:00Z",
                    },
                    {
                        "status": "redeemed",
                        "expires_at": "2026-07-01T12:00:00Z",
                    },
                    {
                        "status": "available",
                        "expires_at": "2026-07-20T12:00:00Z",
                    },
                ],
            }
        )

    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)

    usage = codex_usage.fetch_codex_usage(_auth())

    assert usage == {
        "weekly": {"pct": 12.0},
        "reset_credits": {
            "available": 3,
            "expires_at": "2026-07-20T12:00:00Z",
        },
    }
    assert requested[1].full_url.endswith("/wham/rate-limit-reset-credits")
    headers = {name.lower(): value for name, value in requested[1].header_items()}
    assert headers["openai-beta"] == "codex-1"


def test_reset_credit_detail_failure_keeps_usage_and_count(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 12,
                            "limit_window_seconds": 604_800,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 2},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = 0

    def urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response()
        raise codex_usage.urllib.error.HTTPError(
            request.full_url, 503, "Unavailable", {}, None
        )

    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)

    assert codex_usage.fetch_codex_usage(_auth()) == {
        "weekly": {"pct": 12.0},
        "reset_credits": {"available": 2},
    }


def test_number_rejects_a_huge_integer_instead_of_raising():
    # float(10**400) raises OverflowError; _number must degrade to None, not crash.
    assert codex_usage._number(10**400) is None


def test_iso_timestamp_rejects_millisecond_epoch_and_nan():
    # A millisecond epoch where seconds were expected overflows datetime's year range.
    assert codex_usage._iso_timestamp(1_800_000_000_000) is None
    assert codex_usage._iso_timestamp(float("nan")) is None
    assert codex_usage._iso_timestamp(10**19) is None


def test_convert_payload_rejects_banked_resets_alone():
    try:
        codex_usage._convert_payload({"rate_limit_reset_credits": {"available_count": 0}})
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_convert_payload_rejects_message_estimate_only_credits():
    try:
        codex_usage._convert_payload({"credits": {"approx_local_messages": 5}})
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_convert_payload_rejects_empty_payload():
    try:
        codex_usage._convert_payload({})
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_convert_payload_accepts_real_credit_balance_without_windows():
    assert codex_usage._convert_payload({"credits": {"balance": 12.5}}) == {
        "credits": {"balance": 12.5}
    }


def test_convert_payload_accepts_has_credits_flag_without_windows():
    assert codex_usage._convert_payload({"credits": {"has_credits": True}}) == {
        "credits": {"has_credits": True}
    }


def test_convert_payload_accepts_unlimited_flag_without_windows():
    assert codex_usage._convert_payload({"credits": {"unlimited": True}}) == {
        "credits": {"unlimited": True}
    }


def test_convert_payload_accepts_real_spend_allowance_without_windows():
    assert codex_usage._convert_payload(
        {"spend_control": {"individual_limit": {"limit": 100, "used": 10}}}
    ) == {"credit_allowance": {"limit": 100.0, "used": 10.0}}


def test_window_degrades_millisecond_epoch_reset_instead_of_raising():
    # A millisecond epoch (year overflow) must drop resets_at, not crash the
    # whole conversion; used_percent is still valid so the window survives.
    usage = codex_usage._convert_payload(
        {"rate_limit": {"primary_window": {"used_percent": 50, "reset_at": 1_800_000_000_000}}}
    )
    assert usage == {"five_hour": {"pct": 50.0}}


def test_window_degrades_nan_reset_instead_of_raising():
    usage = codex_usage._convert_payload(
        {"rate_limit": {"primary_window": {"used_percent": 50, "reset_at": float("nan")}}}
    )
    assert usage == {"five_hour": {"pct": 50.0}}


def test_window_rejects_huge_used_percent_instead_of_raising():
    try:
        codex_usage._convert_payload(
            {"rate_limit": {"primary_window": {"used_percent": 10**400}}}
        )
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_window_drops_nan_used_percent_rather_than_emitting_bare_nan():
    # json.dumps({"pct": float("nan")}) emits invalid JSON (`NaN`) for
    # `ccswap codex usage --json` consumers; a NaN pct must not reach output.
    try:
        codex_usage._convert_payload(
            {"rate_limit": {"primary_window": {"used_percent": float("nan")}}}
        )
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_reset_credits_rejects_nan_available_count_instead_of_raising():
    try:
        codex_usage._convert_payload(
            {"rate_limit_reset_credits": {"available_count": float("nan")}}
        )
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_reset_credits_rejects_infinite_available_count_instead_of_raising():
    try:
        codex_usage._convert_payload(
            {"rate_limit_reset_credits": {"available_count": float("inf")}}
        )
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_convert_payload_rejects_bare_allowance_reset_timestamp():
    # A bare reset_at with no limit/used/remaining/pct/limit_reached is not a
    # real allowance and must not be accepted as healthy account data.
    try:
        codex_usage._convert_payload(
            {"spend_control": {"individual_limit": {"reset_at": 1_800_000_000}}}
        )
    except codex_usage.CodexUsageError as exc:
        assert "Codex did not return quota or credit data" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_fetch_usage_explains_expired_or_unauthorized_credentials(monkeypatch):
    def urlopen(request, *, timeout):
        raise codex_usage.urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, None
        )

    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)

    try:
        codex_usage.fetch_codex_usage(_auth())
    except codex_usage.CodexUsageError as exc:
        assert "codex login" in str(exc)
    else:
        raise AssertionError("expected a Codex usage error")


def test_refresh_auth_uses_codex_refresh_contract_and_rotates_tokens(monkeypatch):
    requested = {}

    class Response:
        def read(self):
            return json.dumps(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "id_token": "new-id",
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, *, timeout):
        requested["url"] = request.full_url
        requested["method"] = request.get_method()
        requested["headers"] = {
            name.lower(): value for name, value in request.header_items()
        }
        requested["body"] = json.loads(request.data.decode())
        requested["timeout"] = timeout
        return Response()

    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", urlopen)

    auth = _auth()
    auth["tokens"]["refresh_token"] = "old-refresh"
    refreshed = codex_usage.refresh_codex_auth(auth)

    assert requested == {
        "url": "https://auth.openai.com/oauth/token",
        "method": "POST",
        "headers": {
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "ccswap",
        },
        "body": {
            "client_id": codex_usage.CODEX_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
        },
        "timeout": 10.0,
    }
    assert refreshed["last_refresh"].endswith("Z")
    assert refreshed["tokens"] == {
        **auth["tokens"],
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "id_token": "new-id",
    }
    assert auth["tokens"]["refresh_token"] == "old-refresh"
