"""Read-only Plus, checkout, and GCash capability probing.

The workflow is intentionally limited to the three authenticated requests
verified in the supplied browser HAR:

    accounts/check -> payments/checkout -> Stripe Elements

It creates no payment method, never confirms or subscribes, and never follows
an authenticated redirect. The checkout response is used only to obtain the
opaque custom-method identifier, the exact amount due, and the billing
contract. GCash is affirmative only when that identifier makes an exact
round trip to Stripe and Stripe explicitly names the method GCash.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
import base64
from dataclasses import replace
from collections.abc import Iterable, Mapping
from typing import Any, Callable

from chatgpt_checkout_transport import (
    CheckoutTransportMetadata,
    build_checkout_header_overrides,
)
from plus_probe import probe_plus_eligibility_in_session


CHATGPT_PAYMENTS_BASE = "https://chatgpt.com/backend-api/payments"
CHECKOUT_URL = f"{CHATGPT_PAYMENTS_BASE}/checkout"
STRIPE_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
PROMOTION_ID = "plus-1-month-free"
GCASH_CHECKOUT_COUNTRY = "PH"
GCASH_CHECKOUT_CURRENCY = "PHP"

_CHATGPT_IMPERSONATE = "chrome146"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_CHATGPT_CLIENT_VERSION = "prod-86c6b1bb92aff517de1c44f3c1215fac97a108a0"
_CHATGPT_CLIENT_BUILD = "9696124"
_STRIPE_VERSION = "2025-03-31.basil"
_CHATGPT_SEC_CH_UA = (
    '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"'
)
_CHATGPT_SEC_CH_UA_FULL = (
    '"Chromium";v="146.0.0.0", "Google Chrome";v="146.0.0.0", '
    '"Not?A_Brand";v="99"'
)
_LOGGER = logging.getLogger("gcash_probe")
_OPAQUE_ID_RE = re.compile(r"(?i)^cpmt_[A-Za-z0-9_-]+$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class _ProbeFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status_code: int = 0,
        exception_type: str = "",
    ) -> None:
        super().__init__(code)
        self.code = str(code or "probe_failed")
        self.retryable = bool(retryable)
        self.status_code = int(status_code or 0)
        self.exception_type = (
            str(exception_type)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(exception_type or ""))
            else ""
        )


def _checked_at(value: float | None) -> float:
    try:
        parsed = float(time.time() if value is None else value)
    except (TypeError, ValueError):
        return float(time.time())
    return parsed if math.isfinite(parsed) else float(time.time())


def _safe_id(value: Any, *, max_length: int = 256) -> str:
    text = str(value or "").strip()
    if len(text) > max_length or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return ""
    return text


def _valid_access_token(value: str) -> bool:
    token = str(value or "")
    return bool(token) and len(token) <= 65536 and all(
        0x21 <= ord(char) <= 0x7E for char in token
    )


def _jwt_account_id(token: str) -> str:
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, Mapping):
        return _safe_id(auth.get("chatgpt_account_id") or auth.get("account_id"))
    return ""


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _jwt_header(token: str) -> dict[str, Any]:
    try:
        part = str(token or "").split(".")[0]
        part += "=" * ((4 - len(part) % 4) % 4)
        header = json.loads(base64.urlsafe_b64decode(part))
        return header if isinstance(header, dict) else {}
    except Exception:
        return {}


def _jwt_audiences(payload: Mapping[str, Any]) -> set[str]:
    raw = payload.get("aud")
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return {str(value or "").strip() for value in values if str(value or "").strip()}


def _refreshed_token_claims_match(token: str, previous_token: str) -> bool:
    """Correlate a refreshed token with the stored account session."""
    header = _jwt_header(token)
    payload = _jwt_payload(token)
    previous_header = _jwt_header(previous_token)
    previous = _jwt_payload(previous_token)
    required = ("iss", "sub", "client_id")
    if (
        header.get("alg") != "RS256"
        or not str(header.get("kid") or "").strip()
        or str(header.get("typ") or "JWT").upper() != "JWT"
        or payload.get("iss") != "https://auth.openai.com"
        or "https://api.openai.com/v1" not in _jwt_audiences(payload)
        or any(not str(payload.get(key) or "").strip() for key in required)
        or previous_header.get("alg") != "RS256"
        or not str(previous_header.get("kid") or "").strip()
        or previous.get("iss") != "https://auth.openai.com"
        or "https://api.openai.com/v1" not in _jwt_audiences(previous)
        or any(not str(previous.get(key) or "").strip() for key in required)
    ):
        return False
    try:
        now = time.time()
        if float(payload.get("exp")) <= now:
            return False
        not_before = float(payload.get("nbf", payload.get("iat")))
        if not_before > now + 300:
            return False
    except (TypeError, ValueError):
        return False
    for key in required:
        if str(payload.get(key)).strip() != str(previous.get(key)).strip():
            return False
    return _jwt_audiences(payload) == _jwt_audiences(previous)


def _safe_cookie(name: str, value: str) -> bool:
    return bool(
        re.fullmatch(r"[!#$%&'*+.^_\x60|~0-9A-Za-z-]+", str(name or ""))
        and re.fullmatch(
            r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+",
            str(value or ""),
        )
    )


def _sanitize_cookie_header(value: str, *, device_id: str) -> str:
    pairs: dict[str, str] = {}
    for item in str(value or "").split(";"):
        name, separator, content = item.strip().partition("=")
        if separator and name and _safe_cookie(name, content):
            pairs[name] = content
    if device_id and _safe_cookie("oai-did", device_id):
        pairs["oai-did"] = device_id
    return "; ".join(f"{name}={content}" for name, content in pairs.items())


def _has_session_cookie(value: str) -> bool:
    return bool(_cookie_pairs(value).get("__Secure-next-auth.session-token"))


def _cookie_pairs(value: str) -> dict[str, str]:
    """Parse cookie pairs without accepting header delimiters or attributes."""
    pairs: dict[str, str] = {}
    for item in str(value or "").split(";"):
        name, separator, content = item.strip().partition("=")
        if separator and _safe_cookie(name, content):
            pairs[name] = content
    return pairs


def _merge_session_cookies(base: str, session: Any, *, device_id: str) -> str:
    pairs = _cookie_pairs(base)
    allowed = {
        "oai-did", "oai-sc", "oai-hlib", "oaicom-stable-id", "oai-gn",
        "oai-nav-state", "oai-client-auth-info", "_account_is_fedramp",
        "oai_consent_analytics", "oai_consent_marketing",
        "__Secure-next-auth.session-token", "__Host-next-auth.csrf-token",
        "__Secure-next-auth.callback-url", "__cf_bm", "__cflb", "_cfuvid",
        "__oailb", "cf_clearance",
    }
    try:
        items = session.items()
    except Exception:
        items = ()
    try:
        for name, value in items:
            if str(name) in allowed and _safe_cookie(str(name), str(value)):
                pairs[str(name)] = str(value)
    except Exception:
        pass
    if device_id and _safe_cookie("oai-did", device_id):
        pairs["oai-did"] = device_id
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _clear_session_cookies(session: Any) -> None:
    try:
        session.clear()
    except Exception:
        pass


def _auth_session_refresh(
    session: Any,
    *,
    access_token: str,
    expected_account_id: str,
    device_id: str,
    cookie_header: str,
    timeout: int,
) -> tuple[str, str, str]:
    """Refresh a same-origin session without adopting another account token."""
    if not _has_session_cookie(cookie_header):
        return access_token, cookie_header, "not_requested"
    headers = {
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": _USER_AGENT,
        "OAI-Device-Id": device_id,
        "Cookie": cookie_header,
    }
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            return access_token, cookie_header, f"auth_session_http_{status_code}"
        payload = response.json()
        if not isinstance(payload, Mapping):
            return access_token, cookie_header, "auth_session_invalid_payload"
        refreshed = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
        if not _valid_access_token(refreshed):
            return access_token, cookie_header, "token_invalid"
        expected = _safe_id(expected_account_id)
        observed = _jwt_account_id(refreshed)
        stored = _jwt_account_id(access_token)
        if not expected:
            return access_token, cookie_header, "token_unbound"
        if stored and stored != expected:
            return access_token, cookie_header, "token_claim_mismatch"
        if not _refreshed_token_claims_match(refreshed, access_token):
            return access_token, cookie_header, "token_claim_mismatch"
        if not observed:
            return access_token, cookie_header, "token_unparseable"
        if observed != expected:
            return access_token, cookie_header, "token_account_mismatch"
        merged = _merge_session_cookies(
            cookie_header,
            getattr(session, "cookies", {}),
            device_id=device_id,
        )
        return refreshed, merged or cookie_header, "refreshed"
    except Exception:
        return access_token, cookie_header, "failed"
    finally:
        _clear_session_cookies(getattr(session, "cookies", None))


def _key(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", text)
    return text.lower().replace("-", "_").replace(" ", "_")


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk(child)


def _opaque_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if _OPAQUE_ID_RE.fullmatch(text) else ""


def _minor_units(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _exact_due(checkout: Mapping[str, Any]) -> int | None:
    state = checkout.get("checkout_state")
    total = state.get("total") if isinstance(state, Mapping) else None
    due = total.get("total") if isinstance(total, Mapping) else None
    if not isinstance(due, Mapping):
        return None
    amount = _minor_units(due.get("minorUnitsAmount"))
    return amount if amount is None or amount >= 0 else None


def _checkout_region(checkout: Mapping[str, Any]) -> tuple[str, str]:
    billing = checkout.get("billing_details")
    country = billing.get("country") if isinstance(billing, Mapping) else ""
    currency = billing.get("currency") if isinstance(billing, Mapping) else ""
    state = checkout.get("checkout_state")
    if isinstance(state, Mapping):
        if not country:
            address = state.get("billingAddress")
            if isinstance(address, Mapping):
                address = address.get("address", address)
                if isinstance(address, Mapping):
                    country = address.get("country") or country
        currency = state.get("currency") or currency
    return str(country or "").strip().upper(), str(currency or "").strip().upper()


def _checkout_custom_ids(checkout: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("custom_payment_methods", "custom_payment_method_data"):
        values = checkout.get(key)
        values = values if isinstance(values, (list, tuple)) else [values]
        for value in values:
            if isinstance(value, Mapping):
                for field in ("id", "type", "custom_payment_method_type_id"):
                    if method_id := _opaque_id(value.get(field)):
                        ids.append(method_id)
            elif method_id := _opaque_id(value):
                ids.append(method_id)
    return list(dict.fromkeys(ids))


def _stripe_methods(payload: Any) -> list[tuple[str, str, bool]]:
    methods: list[tuple[str, str, bool]] = []
    if not isinstance(payload, Mapping):
        return methods
    values = payload.get("custom_payment_method_data")
    values = values if isinstance(values, (list, tuple)) else []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        method_id = _opaque_id(
            value.get("type")
            or value.get("id")
            or value.get("custom_payment_method_type_id")
        )
        label = str(value.get("display_name") or "").strip()
        if method_id:
            has_error = bool(value.get("has_error") or value.get("error"))
            methods.append((method_id, label, has_error))
    return methods


def _base_gcash(*, checked_at: float | None = None) -> dict[str, Any]:
    return {
        "operation": "gcash_payment_eligibility",
        "check_scope": "payment_method_only",
        "classification": "ineligible",
        "eligible": False,
        "decision": "gcash_evidence_missing",
        "conclusive": True,
        "retryable": False,
        "status": "ineligible",
        "label": "GCash unavailable",
        "checked_at": _checked_at(checked_at),
        "method_available": None,
        "method_evidence_present": False,
        "custom_method_id_discovered": False,
        "trusted_custom_method_matched": False,
        "amount_minor": None,
        "amount_status": "unavailable",
        "zero_payment": None,
        "currency": "",
        "checkout_country": "",
        "auth_refresh_status": "",
        "custom_method_probe_status": "",
        "custom_method_probe_failure": "",
        "custom_method_probe_exception": "",
    }


def _safe_code(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_CODE_RE.fullmatch(text) else fallback


def gcash_probe_error(
    decision: str,
    *,
    retryable: bool,
    status: str = "error",
    label: str = "GCash unavailable",
    checked_at: float | None = None,
) -> dict[str, Any]:
    del label
    result = _base_gcash(checked_at=checked_at)
    result.update({
        "decision": _safe_code(decision, fallback="probe_failed"),
        "retryable": bool(retryable),
        "status": "error" if str(status).lower() == "unknown" else str(status or "error"),
    })
    return result


def gcash_unavailable(
    decision: str,
    *,
    checked_at: float | None = None,
) -> dict[str, Any]:
    result = _base_gcash(checked_at=checked_at)
    result["decision"] = _safe_code(decision, fallback="gcash_unavailable")
    return result


def normalize_gcash_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize legacy and current results to the binary UI contract."""
    source = dict(value) if isinstance(value, Mapping) else {}
    result = _base_gcash(checked_at=source.get("checked_at"))
    for field in result:
        if field in source and field not in {
            "classification", "eligible", "label", "status", "decision"
        }:
            result[field] = source[field]
    result["checked_at"] = _checked_at(source.get("checked_at"))
    available = (
        str(source.get("classification") or "").lower() == "eligible"
        or source.get("eligible") is True
    )
    result["classification"] = "eligible" if available else "ineligible"
    result["eligible"] = available
    result["conclusive"] = True
    result["label"] = "GCash available" if available else "GCash unavailable"
    if available:
        result["status"] = "eligible"
        result["decision"] = _safe_code(
            source.get("decision"), fallback="gcash_available"
        )
    else:
        status = str(source.get("status") or "ineligible").lower()
        result["status"] = (
            "error"
            if status == "unknown"
            else status
            if status in {"error", "no_at", "not_found", "token_invalid"}
            else "ineligible"
        )
        result["decision"] = _safe_code(
            source.get("decision"), fallback="gcash_unavailable"
        )
    result["amount_minor"] = _minor_units(result.get("amount_minor"))
    invalid_amount = (
        result["amount_minor"] is not None and result["amount_minor"] < 0
    )
    if invalid_amount:
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "gcash_amount_missing",
            "status": "ineligible",
            "label": "GCash unavailable",
            "amount_minor": None,
        })
    if result["amount_minor"] is None:
        result["amount_status"] = "unavailable"
        result["zero_payment"] = None
    else:
        result["amount_status"] = (
            "zero" if result["amount_minor"] == 0 else "positive"
        )
        result["zero_payment"] = result["amount_minor"] == 0
    result["currency"] = str(result.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", result["currency"]):
        result["currency"] = ""
    result["checkout_country"] = str(
        result.get("checkout_country") or ""
    ).upper()
    if not re.fullmatch(r"[A-Z]{2}", result["checkout_country"]):
        result["checkout_country"] = ""
    result["method_available"] = (
        result["method_available"]
        if isinstance(result["method_available"], bool)
        else None
    )
    for field in (
        "method_evidence_present",
        "custom_method_id_discovered",
        "trusted_custom_method_matched",
    ):
        result[field] = bool(result[field])
    safe_statuses = {
        "", "not_requested", "requested", "accepted", "rejected",
        "failed", "not_configured",
    }
    if str(result.get("custom_method_probe_status") or "") not in safe_statuses:
        result["custom_method_probe_status"] = ""
    for field in ("custom_method_probe_failure", "custom_method_probe_exception"):
        result[field] = _safe_code(result.get(field), fallback="")
    auth_status = str(result.get("auth_refresh_status") or "")
    if not re.fullmatch(
        r"(?:not_requested|refreshed|no_token|failed|token_(?:unbound|unparseable|invalid|claim_mismatch|account_mismatch)|auth_session_(?:http_[1-5][0-9]{2}|invalid_(?:json|payload)))?",
        auth_status,
    ):
        result["auth_refresh_status"] = ""
    last = source.get("last_conclusive")
    if isinstance(last, Mapping):
        result["last_conclusive"] = normalize_gcash_result(last)
    return result


def _classify_checkout_and_stripe(
    checkout: Mapping[str, Any],
    stripe: Mapping[str, Any] | None,
    *,
    checked_at: float | None = None,
) -> dict[str, Any]:
    result = _base_gcash(checked_at=checked_at)
    country, currency = _checkout_region(checkout)
    due = _exact_due(checkout)
    checkout_ids = set(_checkout_custom_ids(checkout))
    result.update({
        "currency": currency,
        "checkout_country": country,
        "amount_minor": due,
        "amount_status": (
            "unavailable" if due is None else ("zero" if due == 0 else "positive")
        ),
        "zero_payment": None if due is None else due == 0,
        "method_evidence_present": bool(checkout_ids),
        "custom_method_id_discovered": bool(checkout_ids),
    })
    if country != GCASH_CHECKOUT_COUNTRY or currency != GCASH_CHECKOUT_CURRENCY:
        result["decision"] = "gcash_billing_contract_mismatch"
        return result
    if due is None:
        result["decision"] = "gcash_amount_missing"
        return result
    if not checkout_ids:
        result["decision"] = "gcash_method_metadata_missing"
        return result
    if not isinstance(stripe, Mapping):
        result["decision"] = "gcash_metadata_missing"
        return result
    methods = _stripe_methods(stripe)
    if not methods:
        result["decision"] = "gcash_metadata_missing"
        return result
    matching = [
        (method_id, label, has_error)
        for method_id, label, has_error in methods
        if method_id in checkout_ids
    ]
    if not matching:
        result["decision"] = "gcash_method_id_mismatch"
        return result
    result["trusted_custom_method_matched"] = True
    if any(has_error for _, _, has_error in matching):
        result["decision"] = "gcash_method_error"
        return result
    if not any(label.casefold() == "gcash" for _, label, _ in matching):
        result["decision"] = "gcash_label_missing"
        return result
    result.update({
        "classification": "eligible",
        "eligible": True,
        "decision": "gcash_available",
        "status": "eligible",
        "label": "GCash available",
        "method_available": True,
    })
    return result


def classify_gcash_evidence(
    payloads: Iterable[Any],
    *,
    require_zero: bool = True,
    checked_at: float | None = None,
    trusted_custom_method_ids: Iterable[str] = (),
    require_trusted_custom_method_match: bool = False,
) -> dict[str, Any]:
    """Classify checkout and Stripe payloads using the strict current contract."""
    del require_zero, trusted_custom_method_ids, require_trusted_custom_method_match
    values = [value for value in payloads if isinstance(value, Mapping)]
    checkout = next(
        (
            value
            for value in values
            if isinstance(value.get("checkout_state"), Mapping)
            or isinstance(value.get("billing_details"), Mapping)
            or "custom_payment_methods" in value
        ),
        values[0] if values else {},
    )
    stripe = next(
        (
            value
            for value in values
            if "custom_payment_method_data" in value and value is not checkout
        ),
        None,
    )
    return _classify_checkout_and_stripe(
        checkout, stripe, checked_at=checked_at
    )


def _checkout_payload() -> dict[str, Any]:
    return {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": GCASH_CHECKOUT_COUNTRY,
            "currency": GCASH_CHECKOUT_CURRENCY,
        },
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": PROMOTION_ID,
            "is_coupon_from_query_param": False,
        },
    }


def _stripe_elements_params(
    checkout: Mapping[str, Any],
    custom_ids: list[str],
) -> tuple[str, dict[str, str]] | None:
    billing = checkout.get("billing_details")
    state = checkout.get("checkout_state")
    currency = (
        billing.get("currency") if isinstance(billing, Mapping) else None
    ) or (
        state.get("currency") if isinstance(state, Mapping) else None
    ) or "PHP"
    currency = str(currency).strip().lower()
    publishable_key = str(
        checkout.get("publishable_key")
        or checkout.get("stripe_publishable_key")
        or checkout.get("publishableKey")
        or ""
    ).strip()
    customer_secret = str(
        checkout.get("customer_session_client_secret")
        or checkout.get("customerSessionClientSecret")
        or ""
    ).strip()
    ids = [
        method_id
        for method_id in dict.fromkeys(custom_ids)
        if _opaque_id(method_id)
    ]
    if not publishable_key.startswith("pk_") or not customer_secret or not ids:
        return None
    payment_methods = checkout.get("payment_method_types")
    if not isinstance(payment_methods, (list, tuple)):
        payment_methods = []
    due = _exact_due(checkout)
    if due is None:
        return None
    params: dict[str, str] = {
        "customer_session_client_secret": customer_secret,
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(due),
        "deferred_intent[currency]": currency,
        "currency": currency,
        "key": publishable_key,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(uuid.uuid4()),
        "locale": "en-US",
        "type": "deferred_intent",
        "deferred_intent[setup_future_usage]": "off_session",
        "_stripe_version": _STRIPE_VERSION,
    }
    for index, method in enumerate(payment_methods):
        if isinstance(method, Mapping):
            method = method.get("type") or method.get("name") or method.get("id")
        token = _key(method)
        if token:
            params[f"deferred_intent[payment_method_types][{index}]"] = token
    for index, method_id in enumerate(ids):
        params[f"custom_payment_methods[{index}]"] = method_id
    return publishable_key, params


def _chatgpt_headers(
    access_token: str,
    *,
    account_id: str,
    device_id: str,
    cookie_header: str,
    route: str,
    session_id: str,
    checkout_transport: CheckoutTransportMetadata | None = None,
) -> dict[str, str]:
    transport = (
        checkout_transport.validated()
        if isinstance(checkout_transport, CheckoutTransportMetadata)
        else None
    )
    strict_checkout = bool(
        transport
        and transport.strict_har
        and route.endswith("/payments/checkout")
    )
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": (
            "https://chatgpt.com/?promo_campaign=plus-1-month-free"
            if route.endswith("/payments/checkout")
            else "https://chatgpt.com/"
        ),
        "User-Agent": _USER_AGENT,
        "OAI-Device-Id": device_id,
        "oai-language": "en-US",
        "oai-session-id": (
            transport.session_id if transport and transport.session_id else session_id
        ),
        "oai-client-version": (
            transport.client_version
            if transport and transport.client_version
            else _CHATGPT_CLIENT_VERSION
        ),
        "oai-client-build-number": (
            transport.client_build
            if transport and transport.client_build
            else _CHATGPT_CLIENT_BUILD
        ),
        "sec-ch-ua": _CHATGPT_SEC_CH_UA,
        "sec-ch-ua-full-version-list": _CHATGPT_SEC_CH_UA_FULL,
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform-version": '"10.0.0"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": route,
        "x-openai-target-route": route,
    }
    if access_token and not strict_checkout:
        headers["Authorization"] = f"Bearer {access_token}"
    if account_id and not strict_checkout:
        headers["ChatGPT-Account-ID"] = account_id
    if cookie_header and not strict_checkout:
        headers["Cookie"] = cookie_header
    if route.endswith("/payments/checkout"):
        headers.update(build_checkout_header_overrides(transport))
    return headers


def _stripe_headers(publishable_key: str) -> dict[str, str]:
    del publishable_key
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "sec-ch-ua": _CHATGPT_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


def _json_response(response: Any, stage: str) -> Mapping[str, Any]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        raise _ProbeFailure(
            f"{stage}_http_{status_code}",
            retryable=status_code in {408, 425, 429} or status_code >= 500,
            status_code=status_code,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise _ProbeFailure(f"{stage}_invalid_json", retryable=True) from exc
    if not isinstance(payload, Mapping):
        raise _ProbeFailure(f"{stage}_invalid_payload", retryable=True)
    return payload


def _checkout_retry_allowed(
    failure: _ProbeFailure,
    *,
    attempt: int,
    transport: CheckoutTransportMetadata | None,
) -> bool:
    """Allow only the bounded strict-HAR compatibility retry.

    A 400/422 from the deliberately unauthenticated first Checkout request can
    mean that the upstream deployment requires the account context after all.
    In that narrow case, retry once with the same payload, proxy, Sentinel
    values, and identity. Other stages keep their normal retry policy.
    """
    if attempt:
        return False
    if failure.retryable:
        return True
    return bool(
        transport
        and transport.strict_har
        and failure.status_code in {400, 422}
    )


def _post_json(
    session: Any,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: int,
    stage: str,
) -> Mapping[str, Any]:
    try:
        post = getattr(session, "post_isolated", None)
        if not callable(post):
            post = session.post
        response = post(
            url,
            json=dict(payload),
            headers=dict(headers),
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        raise _ProbeFailure(
            f"{stage}_transport_error",
            retryable=True,
            exception_type=type(exc).__name__,
        ) from exc
    return _json_response(response, stage)


def _get_json(
    session: Any,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, Any],
    timeout: int,
    stage: str,
) -> Mapping[str, Any]:
    try:
        response = session.get(
            url,
            headers=dict(headers),
            params=dict(params),
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        raise _ProbeFailure(
            f"{stage}_transport_error",
            retryable=True,
            exception_type=type(exc).__name__,
        ) from exc
    return _json_response(response, stage)


def _plus_error(checked_at: float | None) -> dict[str, Any]:
    return {
        "operation": "plus_trial_eligibility",
        "classification": "unknown",
        "eligible": None,
        "decision": "accounts_check_failed",
        "conclusive": False,
        "retryable": True,
        "status": "error",
        "label": "Plus trial check failed",
        "checked_at": _checked_at(checked_at),
    }


def _failure_pair(
    plus: Mapping[str, Any] | None,
    decision: str,
    *,
    retryable: bool,
    checked_at: float | None,
    auth_refresh_status: str = "",
) -> dict[str, dict[str, Any]]:
    normalized_decision = _safe_code(decision, fallback="probe_failed")
    status = {
        "missing_access_token": "no_at",
        "invalid_access_token": "token_invalid",
        "account_not_found": "not_found",
    }.get(normalized_decision, "error")
    gcash = gcash_probe_error(
        normalized_decision,
        retryable=retryable,
        status=status,
        checked_at=checked_at,
    )
    if auth_refresh_status:
        gcash["auth_refresh_status"] = auth_refresh_status
    return {
        "plus": dict(plus or _plus_error(checked_at)),
        "gcash": gcash,
    }


def probe_checkout_eligibility(
    access_token: str,
    *,
    account_id: str = "",
    device_id: str = "",
    cookie_header: str = "",
    proxy: str = "",
    timeout: int = 30,
    checkout_email: str = "",
    session_factory: Callable[..., Any] | None = None,
    checkout_transport_factory: Callable[..., Any] | None = None,
    checked_at: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the verified read-only workflow and return Plus + GCash results."""
    token = str(access_token or "").strip()
    if not token:
        return _failure_pair(
            None, "missing_access_token", retryable=False, checked_at=checked_at
        )
    if not _valid_access_token(token):
        return _failure_pair(
            None, "invalid_access_token", retryable=False, checked_at=checked_at
        )
    selected_proxy = str(proxy or "").strip()
    if len(selected_proxy) > 2048:
        return _failure_pair(
            None, "proxy_url_too_long", retryable=False, checked_at=checked_at
        )
    use_default_transport = (
        session_factory is None and checkout_transport_factory is None
    )
    if session_factory is None:
        from http_client import create_http_session

        session_factory = create_http_session
    if use_default_transport:
        from chatgpt_checkout_transport import prepare_checkout_transport

        checkout_transport_factory = prepare_checkout_transport
    stable_device = _safe_id(device_id, max_length=128) or str(uuid.uuid4())
    stable_account = _safe_id(account_id) or _jwt_account_id(token)
    stable_session = str(uuid.uuid4())
    safe_cookie = _sanitize_cookie_header(cookie_header, device_id=stable_device)
    chatgpt = None
    stripe_session = None
    timeout = max(5, min(int(timeout or 30), 60))
    plus: Mapping[str, Any] | None = None
    auth_refresh_status = "not_requested"
    try:
        chatgpt = session_factory(
            proxy=selected_proxy or None,
            impersonate=_CHATGPT_IMPERSONATE,
        )
        checkout_token, checkout_cookie, auth_refresh_status = _auth_session_refresh(
            chatgpt,
            access_token=token,
            expected_account_id=stable_account,
            device_id=stable_device,
            cookie_header=safe_cookie,
            timeout=timeout,
        )
        plus = probe_plus_eligibility_in_session(
            checkout_token,
            session=chatgpt,
            email=checkout_email,
            account_id=stable_account,
            device_id=stable_device,
            cookie_header=checkout_cookie,
            timeout=timeout,
            proxy_for_error=selected_proxy,
            user_agent=_USER_AGENT,
            session_id=stable_session,
            checked_at=checked_at,
        )
        if plus.get("status") in {"token_invalid", "banned", "no_at"}:
            return _failure_pair(
                plus,
                "accounts_check_not_eligible",
                retryable=False,
                checked_at=checked_at,
                auth_refresh_status=auth_refresh_status,
            )
        checkout_transport: CheckoutTransportMetadata | None = None
        if callable(checkout_transport_factory):
            try:
                prepared = checkout_transport_factory(
                    session=chatgpt,
                    access_token=checkout_token,
                    account_id=stable_account,
                    device_id=stable_device,
                    cookie_header=checkout_cookie,
                    session_id=stable_session,
                    timeout=timeout,
                    user_agent=_USER_AGENT,
                    sec_ch_ua=_CHATGPT_SEC_CH_UA,
                    sec_ch_ua_full_version_list=_CHATGPT_SEC_CH_UA_FULL,
                    sec_ch_ua_arch='"x86"',
                    sec_ch_ua_bitness='"64"',
                    sec_ch_ua_model='""',
                    sec_ch_ua_platform_version='"10.0.0"',
                )
                if isinstance(prepared, CheckoutTransportMetadata):
                    checkout_transport = prepared.validated()
            except Exception:
                checkout_transport = None
        checkout: Mapping[str, Any] | None = None
        checkout_failure: _ProbeFailure | None = None
        for attempt in range(2):
            attempt_transport = checkout_transport
            if attempt and checkout_transport and checkout_transport.strict_har:
                # Keep one same-identity retry for installations where the
                # upstream rejects an otherwise valid browser attestation.
                # The retry restores only this account's authenticated headers;
                # it never changes proxy, TLS profile, payload, or Sentinel
                # values.
                attempt_transport = replace(checkout_transport, strict_har=False)
            checkout_headers = _chatgpt_headers(
                checkout_token,
                account_id=stable_account,
                device_id=stable_device,
                cookie_header=checkout_cookie,
                route="/backend-api/payments/checkout",
                session_id=stable_session,
                checkout_transport=attempt_transport,
            )
            try:
                checkout = _post_json(
                    chatgpt,
                    CHECKOUT_URL,
                    _checkout_payload(),
                    checkout_headers,
                    timeout,
                    "checkout",
                )
                checkout_failure = None
                break
            except _ProbeFailure as exc:
                checkout_failure = exc
                if not _checkout_retry_allowed(
                    exc,
                    attempt=attempt,
                    transport=checkout_transport,
                ):
                    break
        if checkout is None:
            failure = checkout_failure or _ProbeFailure(
                "checkout_failed", retryable=True
            )
            return _failure_pair(
                plus,
                failure.code,
                retryable=failure.retryable,
                checked_at=checked_at,
                auth_refresh_status=auth_refresh_status,
            )
        checkout_ids = _checkout_custom_ids(checkout)
        stripe_request = _stripe_elements_params(checkout, checkout_ids)
        if stripe_request is None:
            return {
                "plus": dict(plus),
                "gcash": {
                    **_classify_checkout_and_stripe(
                    checkout, None, checked_at=checked_at
                    ),
                    "auth_refresh_status": auth_refresh_status,
                },
            }
        publishable_key, stripe_params = stripe_request
        stripe_session = session_factory(
            proxy=selected_proxy or None,
            impersonate=_CHATGPT_IMPERSONATE,
        )
        stripe = None
        stripe_failure: _ProbeFailure | None = None
        for attempt in range(2):
            try:
                stripe = _get_json(
                    stripe_session,
                    STRIPE_ELEMENTS_URL,
                    headers=_stripe_headers(publishable_key),
                    params=stripe_params,
                    timeout=timeout,
                    stage="stripe_custom_capability",
                )
                stripe_failure = None
                break
            except _ProbeFailure as exc:
                stripe_failure = exc
                if not exc.retryable or attempt:
                    break
        if stripe is None:
            failure = stripe_failure or _ProbeFailure(
                "stripe_custom_capability_failed", retryable=True
            )
            return _failure_pair(
                plus,
                failure.code,
                retryable=failure.retryable,
                checked_at=checked_at,
                auth_refresh_status=auth_refresh_status,
            )
        gcash = _classify_checkout_and_stripe(
            checkout, stripe, checked_at=checked_at
        )
        gcash["auth_refresh_status"] = auth_refresh_status
        gcash["custom_method_probe_status"] = (
            "accepted" if gcash["eligible"] else "rejected"
        )
        return {"plus": dict(plus), "gcash": gcash}
    except _ProbeFailure as exc:
        return _failure_pair(
            plus,
            exc.code,
            retryable=exc.retryable,
            checked_at=checked_at,
            auth_refresh_status=auth_refresh_status,
        )
    except Exception as exc:
        _LOGGER.warning(
            "[gcash_probe] capability stage failed (%s)", type(exc).__name__
        )
        return _failure_pair(
            plus,
            "probe_unexpected_error",
            retryable=True,
            checked_at=checked_at,
            auth_refresh_status=locals().get("auth_refresh_status", ""),
        )
    finally:
        for session in (stripe_session, chatgpt):
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


def probe_gcash(
    access_token: str,
    *,
    account_id: str = "",
    device_id: str = "",
    cookie_header: str = "",
    proxy: str = "",
    timeout: int = 30,
    require_zero: bool = True,
    checkout_email: str = "",
    session_factory: Callable[..., Any] | None = None,
    checkout_transport_factory: Callable[..., Any] | None = None,
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Backward-compatible GCash-only wrapper around the combined workflow."""
    del require_zero
    return probe_checkout_eligibility(
        access_token,
        account_id=account_id,
        device_id=device_id,
        cookie_header=cookie_header,
        proxy=proxy,
        timeout=timeout,
        checkout_email=checkout_email,
        session_factory=session_factory,
        checkout_transport_factory=checkout_transport_factory,
        checked_at=checked_at,
    )["gcash"]


__all__ = [
    "CHECKOUT_URL",
    "GCASH_CHECKOUT_COUNTRY",
    "GCASH_CHECKOUT_CURRENCY",
    "PROMOTION_ID",
    "STRIPE_ELEMENTS_URL",
    "classify_gcash_evidence",
    "gcash_probe_error",
    "gcash_unavailable",
    "normalize_gcash_result",
    "probe_checkout_eligibility",
    "probe_gcash",
]
