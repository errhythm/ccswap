"""Read Codex rate-limit windows through the same backend route Codex uses."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

DEFAULT_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Codex CLI's public OAuth client identifier. Keep this aligned with Codex's
# own refresh request so saved ChatGPT logins are refreshed by the same flow.
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class CodexUsageError(Exception):
    """Codex usage could not be fetched or decoded."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def usage_url(base_url: str) -> str:
    """Build Codex's upstream rate-limit endpoint from a configured base URL."""
    base = base_url.rstrip("/")
    if (
        base.startswith("https://chatgpt.com")
        or base.startswith("https://chat.openai.com")
    ) and "/backend-api" not in base:
        base = f"{base}/backend-api"
    if "/backend-api" in base:
        return f"{base}/wham/usage"
    return f"{base}/api/codex/usage"


def reset_credits_url(base_url: str) -> str:
    """Build Codex's earned rate-limit reset details endpoint."""
    base = base_url.rstrip("/")
    if (
        base.startswith("https://chatgpt.com")
        or base.startswith("https://chat.openai.com")
    ) and "/backend-api" not in base:
        base = f"{base}/backend-api"
    if "/backend-api" in base:
        return f"{base}/wham/rate-limit-reset-credits"
    return f"{base}/api/codex/rate-limit-reset-credits"


def _jwt_claims(auth: dict[str, Any]) -> dict[str, Any]:
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return {}
    token = tokens.get("id_token")
    if not isinstance(token, str):
        return {}
    import base64

    pieces = token.split(".")
    if len(pieces) != 3:
        return {}
    try:
        payload = pieces[1] + "=" * (-len(pieces[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _headers(auth: dict[str, Any]) -> dict[str, str]:
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexUsageError("saved login has no ChatGPT access token")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexUsageError("saved login has no ChatGPT access token")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "ccswap",
        "Accept": "application/json",
    }
    account_id = tokens.get("account_id")
    claims = _jwt_claims(auth)
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(account_id, str) or not account_id:
        if isinstance(auth_claims, dict):
            account_id = auth_claims.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    if isinstance(auth_claims, dict) and auth_claims.get(
        "chatgpt_account_is_fedramp"
    ):
        headers["X-OpenAI-Fedramp"] = "true"
    return headers


def _window(window: object) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    pct = window.get("used_percent")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    # Keep the type gate above (this path must stay numbers-only, unlike
    # _number's string support) but route the actual conversion through the
    # shared helper so huge/NaN values degrade instead of crashing.
    pct_value = _number(pct)
    if pct_value is None:
        return None
    result: dict[str, Any] = {"pct": pct_value}
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        resets_at = _iso_timestamp(reset_at)
        if resets_at is not None:
            result["resets_at"] = resets_at
    return result


def _window_key(window: object, fallback: str) -> str:
    """Classify a Codex quota window by duration, not API position.

    Historically ``primary_window`` was the five-hour window and
    ``secondary_window`` was weekly. Codex can now return only a weekly primary
    window, so position alone would incorrectly label it as ``5h``.
    """
    if not isinstance(window, dict):
        return fallback
    seconds = window.get("limit_window_seconds", window.get("window_seconds"))
    minutes = window.get("window_duration_mins", window.get("windowDurationMins"))
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        if seconds == 7 * 24 * 60 * 60:
            return "weekly"
        if seconds == 5 * 60 * 60:
            return "five_hour"
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        if minutes == 7 * 24 * 60:
            return "weekly"
        if minutes == 5 * 60:
            return "five_hour"
    return fallback


def _iso_timestamp(value: object) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        # API-controlled numbers can be out of datetime's range (a millisecond
        # epoch where seconds were expected, or plain garbage) — degrade to
        # None instead of crashing the caller.
        try:
            return (
                datetime.fromtimestamp(value, timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    return None


def _reset_credits(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    count = payload.get("available_count", payload.get("availableCount"))
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return None
    count_value = _number(count)
    if count_value is None:
        return None
    result: dict[str, Any] = {"available": max(0, int(count_value))}
    credits = payload.get("credits")
    if isinstance(credits, list):
        expiries = [
            expires
            for credit in credits
            if isinstance(credit, dict)
            and credit.get("status") == "available"
            and (
                expires := _iso_timestamp(
                    credit.get("expires_at", credit.get("expiresAt"))
                )
            )
        ]
        if expiries:
            result["expires_at"] = min(expiries)
    return result


def _number(value: object) -> float | None:
    """Normalize a finite numeric API value, including decimal strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _credits(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize the workspace/purchased Codex credit balance."""
    raw = payload.get("credits")
    if not isinstance(raw, dict):
        return None

    result: dict[str, Any] = {}
    for source_key, target_key in (
        ("has_credits", "has_credits"),
        ("unlimited", "unlimited"),
        ("overage_limit_reached", "limit_reached"),
    ):
        value = raw.get(source_key)
        if isinstance(value, bool):
            result[target_key] = value
    for source_key, target_key in (
        ("balance", "balance"),
        ("approx_local_messages", "approx_local_messages"),
        ("approx_cloud_messages", "approx_cloud_messages"),
    ):
        value = _number(raw.get(source_key))
        if value is not None:
            result[target_key] = value
    return result or None


def _credit_allowance(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a user's monthly workspace credit/spend allowance."""
    spend_control = payload.get("spend_control")
    if not isinstance(spend_control, dict):
        return None

    result: dict[str, Any] = {}
    individual = spend_control.get("individual_limit")
    if isinstance(individual, dict):
        for source_key, target_key in (
            ("used", "used"),
            ("limit", "limit"),
            ("remaining", "remaining"),
            ("used_percent", "pct"),
        ):
            value = _number(individual.get(source_key))
            if value is not None:
                result[target_key] = value
        resets_at = _iso_timestamp(individual.get("reset_at"))
        if resets_at is not None:
            result["resets_at"] = resets_at

    reached = spend_control.get("reached")
    if reached is True or (reached is False and result):
        result["limit_reached"] = reached
    return result or None


# Credit fields that reflect real credit state. `approx_local_messages` and
# `approx_cloud_messages` are rough estimates only, not evidence of usable
# credit, so they don't count on their own.
_SUBSTANTIVE_CREDIT_KEYS = frozenset({"has_credits", "unlimited", "limit_reached", "balance"})
# A bare `resets_at` (or nothing) is not a real allowance — only these mean the
# account actually carries spend/limit state.
_SUBSTANTIVE_ALLOWANCE_KEYS = frozenset({"limit", "used", "remaining", "pct", "limit_reached"})


def _has_substantive_data(usage: dict[str, Any]) -> bool:
    """Reject a payload that only degrades to banked resets or message
    estimates — those alone must not overwrite a healthy last-good usage row."""
    if "five_hour" in usage or "weekly" in usage:
        return True
    credits = usage.get("credits")
    if isinstance(credits, dict) and not _SUBSTANTIVE_CREDIT_KEYS.isdisjoint(credits):
        return True
    allowance = usage.get("credit_allowance")
    if isinstance(allowance, dict) and not _SUBSTANTIVE_ALLOWANCE_KEYS.isdisjoint(allowance):
        return True
    return False


def _convert_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CodexUsageError("Codex usage response is not a JSON object")
    usage: dict[str, Any] = {}
    rate_limit = payload.get("rate_limit")
    if isinstance(rate_limit, dict):
        primary_raw = rate_limit.get("primary_window")
        secondary_raw = rate_limit.get("secondary_window")
        primary = _window(primary_raw)
        secondary = _window(secondary_raw)
        if primary is not None:
            usage[_window_key(primary_raw, "five_hour")] = primary
        if secondary is not None:
            usage[_window_key(secondary_raw, "weekly")] = secondary
    reset_credits = _reset_credits(payload.get("rate_limit_reset_credits"))
    if reset_credits is not None:
        usage["reset_credits"] = reset_credits
    credits = _credits(payload)
    if credits is not None:
        usage["credits"] = credits
    credit_allowance = _credit_allowance(payload)
    if credit_allowance is not None:
        usage["credit_allowance"] = credit_allowance
    if not _has_substantive_data(usage):
        raise CodexUsageError("Codex did not return quota or credit data for this account")
    return usage


def _merge_reset_credit_details(usage: dict[str, Any], payload: object) -> None:
    """Merge per-credit expiry details while keeping the usage count authoritative."""
    details = _reset_credits(payload)
    if details is None:
        return
    existing = usage.get("reset_credits")
    if isinstance(existing, dict):
        details["available"] = existing.get("available", details["available"])
    usage["reset_credits"] = details


def fetch_codex_usage(
    auth: dict[str, Any],
    *,
    base_url: str = DEFAULT_CHATGPT_BASE_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fetch normalized Codex quota windows and earned-reset availability."""
    url = usage_url(base_url)
    request = urllib.request.Request(url, headers=_headers(auth))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CodexUsageError(
                "Codex OAuth token is expired or unauthorized; run 'codex login', "
                "then refresh it with 'ccswap codex add'",
                status_code=exc.code,
            ) from exc
        raise CodexUsageError(
            f"Codex usage request failed ({exc.code})", status_code=exc.code
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CodexUsageError(f"Codex usage request failed: {exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexUsageError(f"Could not decode Codex usage response: {exc}") from exc
    usage = _convert_payload(payload)

    # The usage response carries the authoritative count. Fetch the separate
    # detail endpoint only when there is something whose expiry can be shown;
    # failure here must not hide otherwise valid quota data.
    reset_credits = usage.get("reset_credits")
    if isinstance(reset_credits, dict) and reset_credits.get("available", 0) > 0:
        detail_headers = _headers(auth)
        detail_headers["OpenAI-Beta"] = "codex-1"
        detail_request = urllib.request.Request(
            reset_credits_url(base_url), headers=detail_headers
        )
        try:
            with urllib.request.urlopen(detail_request, timeout=timeout) as response:
                details_payload = json.loads(response.read().decode("utf-8"))
            _merge_reset_credit_details(usage, details_payload)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            pass
    return usage


def refresh_codex_auth(
    auth: dict[str, Any],
    *,
    refresh_url: str = DEFAULT_REFRESH_TOKEN_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Return ``auth`` with its ChatGPT OAuth tokens refreshed.

    Codex rotates refresh tokens. Callers must persist the returned document
    before using it and must not invoke this for the currently active login,
    which Codex itself may be refreshing concurrently.
    """
    tokens = auth.get("tokens")
    refresh_token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexUsageError("saved login has no ChatGPT refresh token")

    body = json.dumps(
        {
            "client_id": CODEX_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        refresh_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ccswap",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise CodexUsageError(
                "Codex refresh token is expired, revoked, or already used; "
                "run 'codex login', then refresh it with 'ccswap codex add'",
                status_code=exc.code,
            ) from exc
        raise CodexUsageError(
            f"Codex token refresh failed ({exc.code})", status_code=exc.code
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CodexUsageError(f"Codex token refresh failed: {exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexUsageError(f"Could not decode Codex token refresh response: {exc}") from exc

    if not isinstance(payload, dict):
        raise CodexUsageError("Codex token refresh response is not a JSON object")
    access_token = payload.get("access_token")
    rotated_refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexUsageError("Codex token refresh response has no access token")

    # The auth document is JSON-only; copying it through JSON avoids mutating a
    # caller's stale snapshot before the caller has durably saved the rotation.
    refreshed = json.loads(json.dumps(auth))
    refreshed_tokens = refreshed["tokens"]
    refreshed_tokens["access_token"] = access_token
    if isinstance(rotated_refresh_token, str) and rotated_refresh_token:
        refreshed_tokens["refresh_token"] = rotated_refresh_token
    id_token = payload.get("id_token")
    if isinstance(id_token, str) and id_token:
        refreshed_tokens["id_token"] = id_token
    refreshed["last_refresh"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    return refreshed
