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
    "v4-2023-04-27?timezone_offset_min=-"
)
_IMPERSONATE = "chrome110"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
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
    if len(text) > max_length or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return ""
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

    selected_proxy = str(proxy or "").strip()
    if len(selected_proxy) > 2048:
        return plus_probe_error(
            "proxy_url_too_long",
            retryable=False,
            status="error",
            label="Proxy configuration invalid",
            checked_at=checked_at,
        )
    account_id = _jwt_account_id(token)
    stable_device = _safe_identity(device_id, max_length=128)
    if not stable_device:
        stable_device = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"gpt-auto-register-check-plus:{str(email or '').strip().lower()[:320]}",
        ))

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Device-Id": stable_device,
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id

    if session_factory is None:
        from http_client import create_http_session

        session_factory = create_http_session

    session = None
    try:
        try:
            session = session_factory(
                proxy=selected_proxy or None,
                impersonate=_IMPERSONATE,
            )
            response = session.get(
                ACCOUNTS_CHECK_URL,
                headers=headers,
                timeout=max(5, min(int(timeout or 15), 60)),
                allow_redirects=False,
            )
        except Exception as error:
            return _network_error_result(
                error,
                proxy=selected_proxy,
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
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


__all__ = [
    "plus_operator_note",
    "probe_plus_eligibility",
    "safe_plus_result_label",
    "should_persist_plus_result",
]
