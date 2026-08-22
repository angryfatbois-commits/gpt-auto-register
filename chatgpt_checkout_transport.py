"""Runtime-only metadata for the read-only ChatGPT checkout capability probe.

The checkout page adds several short-lived browser values to its request.  A
HAR is evidence of their *shape*, not a source of reusable credentials.  This
module therefore accepts values created by the current browser session,
validates their boundaries, and keeps them in memory only.  It deliberately
does not mint deployment signatures or copy values from a recorded session.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse


CHATGPT_ORIGIN = "https://chatgpt.com"
_HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_ATTESTATION_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SCRIPT_PATH_RE = re.compile(r"^/sentinel/[^/]+/sdk\.js$")


def _bounded_header(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or not _HEADER_VALUE_RE.fullmatch(text):
        return ""
    return text


def _safe_account_header(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", text) else ""


def _safe_bearer_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 65536:
        return ""
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in text):
        return ""
    return text


def _decode_b64url_json(value: str) -> Mapping[str, Any] | None:
    try:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def validate_deployment_attestation(value: Any, *, now: float | None = None) -> str:
    """Return an attestation only when its envelope is structurally current.

    Signature verification belongs to the upstream service.  We still reject
    malformed, stale, overlong, or cross-purpose values before they reach the
    request builder.  The raw value is never included in a probe result.
    """
    text = _bounded_header(value, limit=4096)
    parts = text.split(".") if text else []
    if len(parts) != 2 or not all(_ATTESTATION_PART_RE.fullmatch(part) for part in parts):
        return ""
    payload = _decode_b64url_json(parts[0])
    if not payload:
        return ""
    required = ("version", "track", "deployId", "subject", "issuedAt", "expiresAt")
    if any(key not in payload for key in required):
        return ""
    if payload.get("version") != 1 or str(payload.get("track") or "") not in {"stable", "canary"}:
        return ""
    for key in ("deployId", "subject"):
        value_text = str(payload.get(key) or "")
        if not value_text or len(value_text) > 512 or not _HEADER_VALUE_RE.fullmatch(value_text):
            return ""
    try:
        issued = float(payload["issuedAt"])
        expires = float(payload["expiresAt"])
        current = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        return ""
    if not all(map(lambda number: number == number and abs(number) != float("inf"), (issued, expires, current))):
        return ""
    # Allow a small clock skew, but never accept a value that is already stale.
    if expires <= current - 30 or issued > current + 300 or expires <= issued:
        return ""
    return text


def _extract_first(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _same_origin_script(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        if f"{parsed.scheme}://{parsed.netloc}" != CHATGPT_ORIGIN:
            return ""
        path = parsed.path
    else:
        path = text
        text = f"{CHATGPT_ORIGIN}{path}"
    if not _SCRIPT_PATH_RE.fullmatch(path):
        return ""
    return text


def parse_bootstrap_metadata(
    html: Any,
    *,
    response_headers: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Extract safe, current browser metadata from a ChatGPT page response."""
    text = str(html or "")
    headers = response_headers or {}
    lower_headers = {str(key).lower(): value for key, value in headers.items()}
    client_version = _extract_first(
        (r"<html[^>]+data-build=[\"']([^\"']+)", r"data-build\\?\"?:\\?\"([^\"']+)"),
        text,
    )
    client_build = _extract_first(
        (r"<html[^>]+data-seq=[\"']([^\"']+)", r"data-seq\\?\"?:\\?\"([^\"']+)"),
        text,
    )
    script = _same_origin_script(
        _extract_first(
            (
                r"<script[^>]+src=[\"'](https://chatgpt\.com/sentinel/[^\"']+/sdk\.js)",
                r"<script[^>]+src=[\"'](/sentinel/[^\"']+/sdk\.js)",
            ),
            text,
        )
    )
    raw_attestation = _extract_first(
        (
            r"webDeploymentAttestation\\?\"?\s*[:=]\s*\\?\"([^\"']+)",
            r"oai-web-deployment-attestation\\?\"?\s*[:=]\s*\\?\"([^\"']+)",
        ),
        text,
    )
    attestation = validate_deployment_attestation(raw_attestation, now=now)
    session_id = _extract_first(
        (
            r"sessionId\\?\"?\s*[:=]\s*\\?\"([0-9a-f-]{36})",
            r"oai-session-id\\?\"?\s*[:=]\s*\\?\"([0-9a-f-]{36})",
        ),
        text,
    )
    if not _UUID_RE.fullmatch(session_id):
        session_id = ""
    observation = _bounded_header(
        lower_headers.get("x-oai-is-client-observation"), limit=256
    )
    if observation and not re.fullmatch(r"v1\.[A-Za-z0-9._-]{1,240}", observation):
        observation = ""
    return {
        "client_version": _bounded_header(client_version, limit=256),
        "client_build": _bounded_header(client_build, limit=64),
        "session_id": session_id,
        "sentinel_script_url": script,
        "attestation": attestation,
        "client_observation": observation,
    }


def build_oai_telemetry(
    *,
    started_monotonic: float,
    now_monotonic: float | None = None,
    events: tuple[int, ...] = (),
) -> str:
    """Build a bounded per-probe timing value; never reuse a HAR sample."""
    try:
        start = float(started_monotonic)
        end = time.monotonic() if now_monotonic is None else float(now_monotonic)
        elapsed_ms = max(0, min(600_000, round((end - start) * 1000)))
    except (TypeError, ValueError):
        elapsed_ms = 0
    safe_events = [max(0, min(600_000, int(value))) for value in events[:8]]
    return json.dumps([1, elapsed_ms, *safe_events], separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CheckoutTransportMetadata:
    """Ephemeral values used only while building one Checkout request."""

    sentinel_token: str = ""
    telemetry: str = ""
    attestation: str = ""
    client_observation: str = ""
    client_version: str = ""
    client_build: str = ""
    session_id: str = ""
    sentinel_script_url: str = ""
    strict_har: bool = False

    def validated(self) -> "CheckoutTransportMetadata":
        return CheckoutTransportMetadata(
            sentinel_token=_bounded_header(self.sentinel_token, limit=65536),
            telemetry=_bounded_header(self.telemetry, limit=4096),
            attestation=validate_deployment_attestation(self.attestation),
            client_observation=(
                self.client_observation
                if re.fullmatch(r"v1\.[A-Za-z0-9._-]{1,240}", self.client_observation or "")
                else ""
            ),
            client_version=_bounded_header(self.client_version, limit=256),
            client_build=_bounded_header(self.client_build, limit=64),
            session_id=self.session_id if _UUID_RE.fullmatch(self.session_id or "") else "",
            sentinel_script_url=_same_origin_script(self.sentinel_script_url),
            strict_har=bool(self.strict_har),
        )


def build_checkout_header_overrides(
    metadata: CheckoutTransportMetadata | None,
) -> dict[str, str]:
    """Return only dynamic Checkout headers; never include auth/cookie headers."""
    if metadata is None:
        return {}
    value = metadata.validated()
    headers: dict[str, str] = {}
    if value.sentinel_token:
        headers["openai-sentinel-token"] = value.sentinel_token
    if value.telemetry:
        headers["oai-telemetry"] = value.telemetry
    if value.attestation:
        headers["oai-web-deployment-attestation"] = value.attestation
    if value.client_observation:
        headers["x-oai-is-client-observation"] = value.client_observation
    return headers


def _response_text(response: Any) -> str:
    try:
        return str(getattr(response, "text", "") or "")[:1_000_000]
    except Exception:
        return ""


def _response_headers(response: Any) -> dict[str, str]:
    try:
        return {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
    except Exception:
        return {}


def _safe_get(session: Any, url: str, *, headers: Mapping[str, str], timeout: int) -> Any:
    try:
        return session.get(
            url,
            headers=dict(headers),
            timeout=max(5, min(int(timeout or 30), 60)),
            allow_redirects=False,
        )
    except Exception:
        return None


def _sentinel_loader_url(session: Any, *, timeout: int) -> str:
    response = _safe_get(
        session,
        f"{CHATGPT_ORIGIN}/backend-api/sentinel/sdk.js",
        headers={
            "Accept": "*/*",
            "Referer": f"{CHATGPT_ORIGIN}/",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=timeout,
    )
    if response is None:
        return ""
    return _same_origin_script(
        _extract_first(
            (
                r"script\.src\s*=\s*[\"'](https://chatgpt\.com/sentinel/[^\"']+/sdk\.js)",
                r"script\.src\s*=\s*[\"'](/sentinel/[^\"']+/sdk\.js)",
            ),
            _response_text(response),
        )
    )


def _send_sentinel_ping(
    session: Any,
    *,
    user_agent: str,
    referer: str,
    timeout: int,
    client_hints: Mapping[str, str] | None = None,
) -> None:
    """Send the browser's lightweight Sentinel heartbeat before Checkout."""
    post = getattr(session, "post_isolated", None)
    if not callable(post):
        post = getattr(session, "post", None)
    if not callable(post):
        return
    try:
        headers = {
            "Accept": "*/*",
            "Origin": CHATGPT_ORIGIN,
            "Referer": referer or f"{CHATGPT_ORIGIN}/",
            "User-Agent": _bounded_header(user_agent, limit=512) or "Mozilla/5.0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        for name, value in (client_hints or {}).items():
            safe_value = _bounded_header(value, limit=512)
            if safe_value:
                headers[name] = safe_value
        post(
            f"{CHATGPT_ORIGIN}/backend-api/sentinel/ping",
            headers=headers,
            timeout=max(5, min(int(timeout or 30), 60)),
            allow_redirects=False,
        )
    except Exception:
        # Ping is additive telemetry; an unavailable ping must not cause a
        # direct fallback or leak the exception into the eligibility result.
        return


def prepare_checkout_transport(
    *,
    session: Any,
    access_token: str,
    account_id: str,
    device_id: str,
    cookie_header: str,
    session_id: str,
    timeout: int,
    user_agent: str,
    sec_ch_ua: str = "",
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = '"x86"',
    sec_ch_ua_bitness: str = '"64"',
    sec_ch_ua_model: str = '""',
    sec_ch_ua_platform_version: str = '"10.0.19045"',
    strict_har: bool | None = None,
) -> CheckoutTransportMetadata:
    """Prepare ephemeral headers for one checkout capability request.

    This is a best-effort preflight.  Missing page attestation is represented
    by an empty value; the caller then keeps its authenticated fallback rather
    than fabricating a signature.  Sentinel generation is kept in memory and
    is never returned by the eligibility API.
    """
    started = time.monotonic()
    page_headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Referer": f"{CHATGPT_ORIGIN}/",
        "User-Agent": _bounded_header(user_agent, limit=512) or "Mozilla/5.0",
        "OAI-Device-Id": _bounded_header(device_id, limit=128),
    }
    safe_access_token = _safe_bearer_token(access_token)
    if safe_access_token:
        page_headers["Authorization"] = f"Bearer {safe_access_token}"
    safe_account_id = _safe_account_header(account_id)
    if safe_account_id:
        page_headers["ChatGPT-Account-ID"] = safe_account_id
    safe_cookie = str(cookie_header or "").strip()
    if safe_cookie and _HEADER_VALUE_RE.fullmatch(safe_cookie) and len(safe_cookie) <= 16384:
        page_headers["Cookie"] = safe_cookie
    page_response = _safe_get(
        session,
        f"{CHATGPT_ORIGIN}/",
        headers=page_headers,
        timeout=timeout,
    )
    page_text = _response_text(page_response)
    metadata = parse_bootstrap_metadata(
        page_text,
        response_headers=_response_headers(page_response),
    )
    if not metadata["sentinel_script_url"]:
        metadata["sentinel_script_url"] = _sentinel_loader_url(session, timeout=timeout)

    # A deployment attestation may be supplied by an authenticated page
    # response or by an operator-managed environment variable.  Never create
    # one locally: its signature is owned by the upstream web deployment.
    if not metadata["attestation"]:
        import os

        metadata["attestation"] = validate_deployment_attestation(
            os.getenv("GPT_AUTO_REGISTER_WEB_DEPLOYMENT_ATTESTATION", "")
        )

    sentinel_token = ""
    if metadata["sentinel_script_url"]:
        try:
            from sentinel import get_sentinel_token

            sentinel_token, _ = get_sentinel_token(
                session,
                device_id=device_id,
                flow="chatgpt_checkout",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                sec_ch_ua_platform='"Windows"',
                sec_ch_ua_mobile="?0",
                sec_ch_ua_full_version_list=sec_ch_ua_full_version_list,
                sec_ch_ua_arch=sec_ch_ua_arch,
                sec_ch_ua_bitness=sec_ch_ua_bitness,
                sec_ch_ua_model=sec_ch_ua_model,
                sec_ch_ua_platform_version=sec_ch_ua_platform_version,
                screen="1920x1080",
                lang="en-US",
                lang_full="en-US,en;q=0.9",
                navigator_platform="Win32",
                navigator_vendor="Google Inc.",
                hardware_concurrency=8,
                device_memory=8,
                max_touch_points=0,
                device_pixel_ratio=1.0,
                timezone="UTC",
                sentinel_sdk_url=metadata["sentinel_script_url"],
                sentinel_req_url=f"{CHATGPT_ORIGIN}/backend-api/sentinel/req",
            )
        except Exception:
            sentinel_token = ""

    # This is an opaque upstream value.  Omit it when the current response did
    # not provide one; never fabricate or reuse a value observed in the HAR.
    observation = metadata["client_observation"]
    telemetry = build_oai_telemetry(started_monotonic=started)
    if strict_har is None:
        # The first Checkout request in the observed browser flow omits the
        # account headers whenever a fresh Sentinel token is available. The
        # attestation is an additional deployment signal, not a prerequisite
        # for entering strict mode; the bounded authenticated fallback covers
        # deployments that require it on the first attempt.
        strict = bool(sentinel_token)
    else:
        strict = bool(strict_har)
    if sentinel_token:
        _send_sentinel_ping(
            session,
            user_agent=user_agent,
            referer=f"{CHATGPT_ORIGIN}/?promo_campaign=plus-1-month-free",
            timeout=timeout,
            client_hints={
                "sec-ch-ua": sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
                "sec-ch-ua-arch": sec_ch_ua_arch,
                "sec-ch-ua-bitness": sec_ch_ua_bitness,
                "sec-ch-ua-model": sec_ch_ua_model,
                "sec-ch-ua-platform-version": sec_ch_ua_platform_version,
            },
        )
    return CheckoutTransportMetadata(
        sentinel_token=sentinel_token,
        telemetry=telemetry,
        attestation=metadata["attestation"],
        client_observation=observation,
        client_version=metadata["client_version"],
        client_build=metadata["client_build"],
        session_id=session_id or metadata["session_id"],
        sentinel_script_url=metadata["sentinel_script_url"],
        strict_har=strict,
    ).validated()


__all__ = [
    "CHATGPT_ORIGIN",
    "CheckoutTransportMetadata",
    "build_checkout_header_overrides",
    "build_oai_telemetry",
    "parse_bootstrap_metadata",
    "prepare_checkout_transport",
    "validate_deployment_attestation",
]
