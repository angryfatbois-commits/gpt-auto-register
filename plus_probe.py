"""Read-only ChatGPT Plus trial eligibility probe.

The probe performs one accounts/check request and returns a persistence-safe
result. It never follows redirects, never retries through a direct connection,
and never includes tokens, proxy credentials, or upstream response text in its
result.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any, Callable

from eligibility import parse_plus_eligibility, plus_probe_error


ACCOUNTS_CHECK_URL = (
    "https://chatgpt.com/backend-api/accounts/check/"
    "v4-2023-04-27?timezone_offset_min=-420"
)
ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_ROUTE = "/backend-api/accounts/check/{version}"
_IMPERSONATE = "chrome146"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_SEC_CH_UA = (
    '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"'
)
_SEC_CH_UA_FULL = (
    '"Chromium";v="146.0.0.0", "Google Chrome";v="146.0.0.0", '
    '"Not?A_Brand";v="99"'
)
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",
    "potential abuse",
    "terminated",
)
_SAFE_RESULT_LABELS = {
    "plus_eligible": "Plus trial eligible",
    "plus_active": "Plus active",
    "free": "Free - no eligible promotion",
    "banned": "Account deactivated",
    "token_invalid": "Access token invalid",
    "no_at": "No access token",
    "not_found": "Account not found",
    "unknown": "Plus trial status unknown",
    "error": "Plus trial check failed",
}


def _valid_access_token(value: str) -> bool:
    token = str(value or "")
    return bool(token) and len(token) <= 65536 and all(
        0x21 <= ord(character) <= 0x7E for character in token
    )


def _safe_identity(value: Any, *, max_length: int = 256) -> str:
    text = str(value or "").strip()
    if len(text) > max_length or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return ""
    return text


def _safe_cookie_header(value: Any, *, max_length: int = 16384) -> str:
    """Keep only RFC-like cookie pairs before placing them in a request header."""
    text = str(value or "")
    if len(text) > max_length:
        return ""
    pairs: list[str] = []
    for item in text.split(";"):
        name, separator, content = item.strip().partition("=")
        if not separator:
            continue
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            continue
        if not re.fullmatch(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+", content):
            continue
        pairs.append(f"{name}={content}")
    return "; ".join(pairs)


def _safe_user_agent(value: Any) -> str:
    text = str(value or _USER_AGENT).strip()
    if not text or len(text) > 512 or any(char in text for char in "\r\n"):
        return _USER_AGENT
    return text


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        encoded = parts[1]
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _jwt_account_id(token: str) -> str:
    payload = _decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, Mapping):
        return ""
    return _safe_identity(auth.get("chatgpt_account_id") or auth.get("account_id"))


def _response_text(response: Any) -> str:
    try:
        return str(getattr(response, "text", "") or "")[:16384]
    except Exception:
        return ""


def _looks_deactivated(value: str) -> bool:
    text = str(value or "").casefold()
    return any(marker in text for marker in _DEACTIVATED_MARKERS)


def _network_error_result(
    error: BaseException,
    *,
    proxy: str,
    checked_at: float | None,
) -> dict[str, Any]:
    text = str(error or "")
    if proxy and ("(97)" in text or "rejected by the SOCKS5" in text):
        decision = "proxy_auth_rejected"
    elif proxy and "(7)" in text:
        decision = "proxy_unreachable"
    elif proxy:
        decision = "proxy_network_error"
    else:
        decision = "network_error"
    return plus_probe_error(
        decision,
        retryable=True,
        status="error",
        label="Network error",
        checked_at=checked_at,
    )


def should_persist_plus_result(result: Mapping[str, Any]) -> bool:
    """Keep meaningful observations while leaving transient failures retryable."""
    status = str(result.get("status") or "").strip().lower()
    return bool(status) and status not in {"not_found", "no_at", "error"}


def safe_plus_result_label(result: Mapping[str, Any]) -> str:
    """Return an allowlisted operator-facing label for logs and SSE events."""
    status = str(result.get("status") or "error").strip().lower()
    return _SAFE_RESULT_LABELS.get(status, _SAFE_RESULT_LABELS["error"])


def plus_operator_note(result: Mapping[str, Any]) -> str:
    """Map transport decisions to a safe API note without exception details."""
    decision = str(result.get("decision") or "").strip().lower()
    return {
        "proxy_auth_rejected": "Proxy authentication was rejected (SOCKS5 error 97)",
        "proxy_unreachable": "The proxy is unreachable (curl error 7)",
        "proxy_network_error": "The proxy request failed; direct fallback was not used",
    }.get(decision, "")


def _probe_plus_eligibility_in_session(
    access_token: str,
    *,
    session: Any,
    email: str = "",
    device_id: str = "",
    cookie_header: str = "",
    timeout: int = 15,
    account_id: str = "",
    proxy_for_error: str = "",
    user_agent: str = _USER_AGENT,
    session_id: str = "",
    client_version: str = "prod-86c6b1bb92aff517de1c44f3c1215fac97a108a0",
    client_build: str = "9696124",
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Read accounts/check through a caller-owned browser session.

    The caller owns the session lifecycle.  This is used by the combined
    checkout probe so accounts/check and checkout share one proxy, TLS
    profile, and browser identity while Stripe remains isolated in its own
    cookie-less session.
    """
    token = str(access_token or "").strip()
    if not token:
        return plus_probe_error(
            "missing_access_token",
            retryable=False,
            status="no_at",
            label="No access token",
            checked_at=checked_at,
        )
    if not _valid_access_token(token):
        return plus_probe_error(
            "invalid_access_token",
            retryable=False,
            status="token_invalid",
            label="Access token invalid",
            checked_at=checked_at,
        )

    account_id = _safe_identity(account_id, max_length=256) or _jwt_account_id(token)
    stable_device = _safe_identity(device_id, max_length=128)
    if not stable_device:
        stable_device = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"gpt-auto-register-check-plus:{str(email or '').strip().lower()[:320]}",
        ))
    stable_session = _safe_identity(session_id, max_length=128) or str(uuid.uuid4())
    safe_client_version = _safe_identity(client_version, max_length=128)
    safe_client_build = _safe_identity(client_build, max_length=32)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "User-Agent": _safe_user_agent(user_agent),
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Device-Id": stable_device,
        "oai-language": "en-US",
        "oai-session-id": stable_session,
        "oai-client-version": safe_client_version,
        "oai-client-build-number": safe_client_build,
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_ROUTE,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-full-version-list": _SEC_CH_UA_FULL,
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform-version": '"10.0.0"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    safe_cookie = _safe_cookie_header(cookie_header)
    if safe_cookie:
        headers["Cookie"] = safe_cookie

    try:
        response = session.get(
            ACCOUNTS_CHECK_URL,
            headers=headers,
            timeout=max(5, min(int(timeout or 15), 60)),
            allow_redirects=False,
        )
    except Exception as error:
        # A session-aware caller intentionally does not pass the proxy here;
        # the selected route is owned by the session and never falls back.
        return _network_error_result(
            error,
            proxy=str(proxy_for_error or "").strip(),
            checked_at=checked_at,
        )

    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {401, 403}:
        if _looks_deactivated(_response_text(response)):
            result = plus_probe_error(
                "account_deactivated",
                retryable=False,
                status="banned",
                label="Account deactivated",
                checked_at=checked_at,
            )
            result.update({
                "classification": "ineligible",
                "eligible": False,
                "conclusive": True,
            })
            return result
        if status_code == 401:
            return plus_probe_error(
                "token_invalid",
                retryable=False,
                status="token_invalid",
                label="Access token invalid",
                checked_at=checked_at,
            )
        return plus_probe_error(
            "http_403",
            retryable=False,
            status="error",
            label="HTTP 403",
            checked_at=checked_at,
        )
    if status_code != 200:
        return plus_probe_error(
            f"http_{status_code}",
            retryable=status_code >= 500 or status_code in {0, 408, 425, 429},
            status="error",
            label=f"HTTP {status_code}",
            checked_at=checked_at,
        )
    try:
        payload = response.json()
    except Exception:
        return plus_probe_error(
            "invalid_json",
            retryable=True,
            status="error",
            label="Invalid response",
            checked_at=checked_at,
        )
    return parse_plus_eligibility(
        payload,
        account_id=account_id,
        checked_at=checked_at,
    )


def probe_plus_eligibility_in_session(
    access_token: str,
    *,
    session: Any,
    email: str = "",
    account_id: str = "",
    device_id: str = "",
    cookie_header: str = "",
    timeout: int = 15,
    proxy_for_error: str = "",
    user_agent: str = _USER_AGENT,
    session_id: str = "",
    client_version: str = "prod-86c6b1bb92aff517de1c44f3c1215fac97a108a0",
    client_build: str = "9696124",
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Public session-aware accounts/check helper used by combined probes."""
    return _probe_plus_eligibility_in_session(
        access_token,
        session=session,
        email=email,
        account_id=account_id,
        device_id=device_id,
        cookie_header=cookie_header,
        timeout=timeout,
        proxy_for_error=proxy_for_error,
        user_agent=user_agent,
        session_id=session_id,
        client_version=client_version,
        client_build=client_build,
        checked_at=checked_at,
    )


def probe_plus_eligibility(
    access_token: str,
    *,
    email: str = "",
    device_id: str = "",
    proxy: str = "",
    timeout: int = 15,
    session_factory: Callable[..., Any] | None = None,
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Read the exact ``plus-1-month-free`` eligibility for one account."""
    token = str(access_token or "").strip()
    if not token:
        return plus_probe_error(
            "missing_access_token", retryable=False, status="no_at",
            label="No access token", checked_at=checked_at,
        )
    if not _valid_access_token(token):
        return plus_probe_error(
            "invalid_access_token", retryable=False, status="token_invalid",
            label="Access token invalid", checked_at=checked_at,
        )
    selected_proxy = str(proxy or "").strip()
    if len(selected_proxy) > 2048:
        return plus_probe_error(
            "proxy_url_too_long", retryable=False, status="error",
            label="Proxy configuration invalid", checked_at=checked_at,
        )
    if session_factory is None:
        from http_client import create_http_session

        session_factory = create_http_session
    session = None
    try:
        session = session_factory(
            proxy=selected_proxy or None,
            impersonate=_IMPERSONATE,
        )
        return _probe_plus_eligibility_in_session(
            token,
            session=session,
            email=email,
            device_id=device_id,
            timeout=timeout,
            proxy_for_error=selected_proxy,
            checked_at=checked_at,
        )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


__all__ = [
    "plus_operator_note",
    "probe_plus_eligibility_in_session",
    "probe_plus_eligibility",
    "safe_plus_result_label",
    "should_persist_plus_result",
]
