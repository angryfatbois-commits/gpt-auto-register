"""Side-effect-limited GCash payment-method availability probe.

The probe answers one question only: does the account's current ChatGPT
checkout expose GCash as a payment method? Promotion eligibility, taxes,
currency, and the amount due are retained as optional diagnostics but never
participate in the decision. The probe stops before checkout confirmation,
custom-payment start, or any payment execution operation.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Callable


CHATGPT_PAYMENTS_BASE = "https://chatgpt.com/backend-api/payments"
STRIPE_PAYMENT_PAGE_INIT_URL = "https://api.stripe.com/v1/payment_pages/{session_id}/init"
# Kept as a compatibility constant for callers that imported the old probe.
STRIPE_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
# Kept as a public compatibility constant and used by the source-compatible
# checkout and promotion-update stages.
PROMOTION_ID = "plus-1-month-free"
GCASH_CHECKOUT_COUNTRY = "PH"
GCASH_CHECKOUT_CURRENCY = "PHP"
CHECKOUT_UPDATE_PATH = "/backend-api/payments/checkout/update"
CHECKOUT_UPDATE_URL = f"https://chatgpt.com{CHECKOUT_UPDATE_PATH}"
CHECKOUT_TAXES_PATH = "/backend-api/payments/checkout/taxes"
CHECKOUT_TAXES_URL = f"https://chatgpt.com{CHECKOUT_TAXES_PATH}"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) "
    "Gecko/20100101 Firefox/144.0"
)
_STRIPE_USER_AGENTS = {
    "firefox144": _FIREFOX_USER_AGENT,
    "chrome110": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/110.0.0.0 Safari/537.36"
    ),
}
_STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
_LOGGER = logging.getLogger("gcash_probe")

_BROWSER_PROFILES = {
    "PH": ("en-PH", "Asia/Manila"),
    "US": ("en-US", "America/New_York"),
    "CA": ("en-CA", "America/Toronto"),
    "GB": ("en-GB", "Europe/London"),
    "SG": ("en-SG", "Asia/Singapore"),
    "ID": ("id-ID", "Asia/Jakarta"),
    "AU": ("en-AU", "Australia/Sydney"),
}

_METHOD_COLLECTION_KEYS = frozenset({
    "payment_method_types",
    "ordered_payment_method_types",
    "custom_payment_methods",
    "custom_payment_method_data",
    "available_payment_method_types",
    "supported_payment_method_types",
    "payment_methods",
    "available_payment_methods",
    "supported_payment_methods",
})
_METHOD_SCALAR_KEYS = frozenset({
    "payment_method",
    "payment_method_type",
    "selected_payment_method_type",
    "custom_payment_method",
    "custom_payment_method_type_id",
    "payment_method_type_id",
})
_DIRECT_GCASH_KEYS = frozenset({
    "gcash_available",
    "is_gcash_available",
    "gcash_supported",
    "supports_gcash",
})
_LABEL_KEYS = frozenset({
    "display_name",
    "displayname",
    "name",
    "label",
    "type",
    "payment_method_type",
    "paymentmethodtype",
    "slug",
    "code",
})
_IDENTIFIER_KEYS = frozenset({
    "id",
    "code",
    "slug",
    "custom_payment_method_type_id",
    "payment_method_type_id",
})


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
        self.code = code
        self.retryable = retryable
        self.status_code = int(status_code or 0)
        safe_type = str(exception_type or "").strip()
        self.exception_type = (
            safe_type
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", safe_type)
            else ""
        )


def _checked_at(value: float | None) -> float:
    return float(time.time() if value is None else value)


def normalize_gcash_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize current and legacy GCash results to a two-state contract.

    Only affirmative method evidence produces ``eligible``. Every other
    outcome is ``ineligible``/``GCash unavailable``; technical reason codes
    remain available through ``decision``, ``status``, and ``retryable``.
    """
    result = dict(value) if isinstance(value, Mapping) else {}
    original_classification = str(result.get("classification") or "").strip().lower()
    available = (
        original_classification == "eligible"
        or result.get("eligible") is True
        or result.get("method_available") is True
    )
    result.setdefault("operation", "gcash_payment_eligibility")
    result.setdefault("check_scope", "payment_method_only")
    result["classification"] = "eligible" if available else "ineligible"
    result["eligible"] = available
    result["conclusive"] = True
    result["label"] = "GCash available" if available else "GCash unavailable"
    if available:
        result["status"] = "eligible"
        result.setdefault("decision", "gcash_available")
    else:
        original_status = str(result.get("status") or "").strip().lower()
        if original_status in {"no_at", "not_found", "token_invalid", "error"}:
            result["status"] = original_status
        elif original_classification == "unknown" or original_status == "unknown":
            result["status"] = "error"
        else:
            result["status"] = "ineligible"
        result.setdefault("decision", "gcash_unavailable")
    return result


def gcash_probe_error(
    decision: str,
    *,
    retryable: bool,
    status: str = "error",
    label: str = "GCash unavailable",
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Build a redacted binary result when GCash cannot be observed.

    The operator requested a two-state availability policy. Therefore missing
    credentials, authentication failures, and transport errors are reported as
    unavailable instead of introducing a third ``unknown`` classification.
    ``decision``, ``status``, and ``retryable`` retain the operational reason.

    ``label`` remains accepted for API compatibility but is intentionally not
    exposed: every negative GCash result uses the same user-facing label.
    """
    del label
    probe_status = str(status or "error").strip().lower() or "error"
    if probe_status == "unknown":
        probe_status = "error"
    return {
        "operation": "gcash_payment_eligibility",
        "check_scope": "payment_method_only",
        "classification": "ineligible",
        "eligible": False,
        "decision": str(decision or "probe_failed"),
        "conclusive": True,
        "retryable": bool(retryable),
        "status": probe_status,
        "label": "GCash unavailable",
        "checked_at": _checked_at(checked_at),
        "method_available": None,
        "method_evidence_present": False,
        "custom_method_id_discovered": False,
        "amount_minor": None,
        "currency": "",
        "checkout_country": "",
    }


def gcash_unavailable(
    decision: str,
    *,
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Build a conclusive negative result for rejected/incomplete evidence."""
    return {
        "operation": "gcash_payment_eligibility",
        "check_scope": "payment_method_only",
        "classification": "ineligible",
        "eligible": False,
        "decision": str(decision or "gcash_evidence_incomplete"),
        "conclusive": True,
        "retryable": False,
        "status": "ineligible",
        "label": "GCash unavailable",
        "checked_at": _checked_at(checked_at),
        "method_available": None,
        "method_evidence_present": False,
        "custom_method_id_discovered": False,
        "amount_minor": None,
        "currency": "",
    }


def _walk(value: Any) -> Iterable[Any]:
    """Yield nested JSON-like values without traversing arbitrary objects."""
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk(child)


def _minor_amount(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, Mapping):
        for key in ("amount", "value", "unit_amount", "amount_minor"):
            if key in value:
                parsed = _minor_amount(value[key])
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip().replace(",", "")):
        return int(value.strip().replace(",", ""))
    return None


def _key(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", text)
    return text.lower().replace("-", "_").replace(" ", "_")


def _is_gcash_text(value: Any) -> bool:
    return "gcash" in _key(value)


def _custom_method_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"(?i)cpmt_[A-Za-z0-9_-]+", text) else ""


def _method_values(value: Any) -> Iterable[Any]:
    """Flatten a method collection while retaining map keys as candidates."""
    if isinstance(value, (list, tuple, set)):
        yield from value
        return
    if isinstance(value, Mapping):
        for name, item in value.items():
            name_text = _key(name)
            if name_text:
                yield name_text
            if isinstance(item, (Mapping, list, tuple)):
                yield from _method_values(item)
            else:
                yield item
        return
    if value is not None:
        yield value


def _inspect_method(value: Any) -> tuple[bool, list[str], list[str]]:
    """Return (gcash_seen, custom_ids, method_tokens) for one method value."""
    gcash_seen = False
    custom_ids: list[str] = []
    tokens: list[str] = []

    if isinstance(value, str):
        text = value.strip()
        normalized = _key(text)
        if normalized:
            tokens.append(normalized)
        if custom_id := _custom_method_id(text):
            custom_ids.append(custom_id)
        if _is_gcash_text(text):
            gcash_seen = True
        return gcash_seen, custom_ids, tokens

    if not isinstance(value, Mapping):
        return False, custom_ids, tokens

    for name, item in value.items():
        name_key = _key(name)
        if isinstance(item, str):
            if custom_id := _custom_method_id(item):
                custom_ids.append(custom_id)
        if name_key in _IDENTIFIER_KEYS:
            identifier = str(item or "").strip()
            if custom_id := _custom_method_id(identifier):
                custom_ids.append(custom_id)
        if name_key in _LABEL_KEYS or name_key in _IDENTIFIER_KEYS:
            if isinstance(item, str):
                normalized = _key(item)
                if normalized:
                    tokens.append(normalized)
                if _is_gcash_text(item):
                    gcash_seen = True
        if isinstance(item, (Mapping, list, tuple)):
            nested_gcash, nested_ids, nested_tokens = _inspect_method(item)
            gcash_seen = gcash_seen or nested_gcash
            custom_ids.extend(nested_ids)
            tokens.extend(nested_tokens)

    return gcash_seen, custom_ids, tokens


def _has_non_gcash_label(value: Any) -> bool:
    """Whether a custom-method object explicitly names another provider."""
    if not isinstance(value, Mapping):
        return False
    generic = {
        "custom_payment_method",
        "custom_payment_methods",
        "custom_payment_method_data",
        "payment_method",
        "payment_method_type",
    }
    for name, item in value.items():
        name_key = _key(name)
        if name_key in _LABEL_KEYS and isinstance(item, str):
            token = _key(item)
            if token and token not in generic and not _custom_method_id(token):
                return not _is_gcash_text(item)
        if isinstance(item, Mapping) and _has_non_gcash_label(item):
            return True
        if isinstance(item, (list, tuple)) and any(
            _has_non_gcash_label(child) for child in item
        ):
            return True
    return False


def _evidence(
    payloads: Iterable[Any],
    *,
    trusted_custom_method_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Extract method evidence and retain amount/currency for diagnostics."""
    trusted_ids = {
        method_id
        for value in trusted_custom_method_ids
        if (method_id := _custom_method_id(value))
    }
    method_present = False
    explicit_method_collection = False
    custom_candidate_present = False
    custom_non_gcash_present = False
    custom_candidate_ids: set[str] = set()
    custom_non_gcash_ids: set[str] = set()
    custom_ids: list[str] = []
    method_tokens: list[str] = []
    amounts: set[int] = set()
    currencies: set[str] = set()
    countries: set[str] = set()

    for payload in payloads:
        for item in _walk(payload):
            if not isinstance(item, Mapping):
                continue
            for key, value in item.items():
                key_text = _key(key)
                if key_text in _DIRECT_GCASH_KEYS and isinstance(value, bool):
                    explicit_method_collection = True
                    if value:
                        method_present = True
                    continue

                is_collection = key_text in _METHOD_COLLECTION_KEYS
                if not is_collection and "payment_method" in key_text:
                    is_collection = key_text.endswith(("types", "methods", "data", "options"))
                if is_collection:
                    explicit_method_collection = True
                    for method in _method_values(value):
                        found, ids, tokens = _inspect_method(method)
                        method_present = method_present or found
                        custom_ids.extend(ids)
                        method_tokens.extend(tokens)
                        # ChatGPT's PH custom checkout exposes GCash as an
                        # opaque ``cpmt_...`` entry. Its presence is capability
                        # evidence even if the display label is omitted. A
                        # descriptive non-GCash label overrides that candidate.
                        if key_text in {"custom_payment_methods", "custom_payment_method_data"}:
                            if _has_non_gcash_label(method):
                                custom_non_gcash_present = True
                                custom_non_gcash_ids.update(ids)
                            elif ids:
                                custom_candidate_present = True
                                custom_candidate_ids.update(ids)
                        elif ids and not _has_non_gcash_label(method):
                            custom_candidate_present = True
                            custom_candidate_ids.update(ids)
                elif key_text in _METHOD_SCALAR_KEYS:
                    explicit_method_collection = True
                    found, ids, tokens = _inspect_method(value)
                    method_present = method_present or found
                    custom_ids.extend(ids)
                    method_tokens.extend(tokens)
                    # Some checkout/Elements responses expose the opaque
                    # custom-method ID as a scalar field (for example
                    # ``custom_payment_method_type_id``) instead of placing
                    # it in ``custom_payment_methods``. Treat that ID as the
                    # same GCash capability candidate, while preserving an
                    # explicit non-GCash label on an object as a negative
                    # override.
                    if ids:
                        if _has_non_gcash_label(value):
                            custom_non_gcash_present = True
                            custom_non_gcash_ids.update(ids)
                        else:
                            custom_candidate_present = True
                            custom_candidate_ids.update(ids)

                if key_text in {"currency", "currency_code"} and isinstance(value, str):
                    currency = value.strip().upper()
                    if re.fullmatch(r"[A-Z]{3}", currency):
                        currencies.add(currency)
                if key_text in {"country", "billing_country", "checkout_country"} and isinstance(value, str):
                    country = value.strip().upper()
                    if re.fullmatch(r"[A-Z]{2}", country):
                        countries.add(country)

            total_summary = item.get("total_summary")
            if isinstance(total_summary, Mapping):
                amount = _minor_amount(total_summary.get("due"))
                if amount is not None:
                    amounts.add(amount)
            invoice = item.get("invoice")
            if isinstance(invoice, Mapping):
                amount = _minor_amount(invoice.get("amount_due"))
                if amount is not None:
                    amounts.add(amount)
            for amount_key in ("amount_due", "amount", "amount_minor"):
                amount = _minor_amount(item.get(amount_key))
                if amount is not None:
                    amounts.add(amount)

    trusted_custom_method_matched = bool(trusted_ids.intersection(custom_ids))
    if method_present or trusted_custom_method_matched:
        method_available: bool | None = True
    elif custom_candidate_ids - custom_non_gcash_ids:
        # A response may describe more than one custom method. A negative
        # label for one opaque ID must not hide a different candidate ID.
        method_available = True
    elif custom_non_gcash_present:
        method_available = False
    elif custom_candidate_present:
        # The checkout exposes a live opaque custom-method slot. In this
        # provider-specific probe that slot is the GCash capability candidate;
        # Elements metadata is still queried when credentials are available.
        method_available = True
    else:
        method_available = False if explicit_method_collection else None
    return {
        "method_available": method_available,
        "explicit_method_present": bool(method_present),
        "method_evidence_present": explicit_method_collection,
        "custom_candidate_present": custom_candidate_present,
        "custom_non_gcash_present": custom_non_gcash_present,
        "trusted_custom_method_matched": trusted_custom_method_matched,
        "custom_method_ids": list(dict.fromkeys(custom_ids)),
        "method_tokens": list(dict.fromkeys(method_tokens)),
        "amounts": amounts,
        "currencies": currencies,
        "countries": countries,
    }


def classify_gcash_evidence(
    payloads: Iterable[Any],
    *,
    require_zero: bool = True,
    checked_at: float | None = None,
    trusted_custom_method_ids: Iterable[str] = (),
    require_trusted_custom_method_match: bool = False,
) -> dict[str, Any]:
    """Return a method-only availability result from checkout evidence.

    ``require_zero`` remains accepted for backwards compatibility, but is
    intentionally ignored. A successful response with no GCash method is a
    conclusive ineligible result under the requested binary policy.
    """
    del require_zero
    evidence = _evidence(
        payloads,
        trusted_custom_method_ids=trusted_custom_method_ids,
    )
    method = evidence["method_available"]
    if (
        require_trusted_custom_method_match
        and not evidence["explicit_method_present"]
        and not evidence["trusted_custom_method_matched"]
    ):
        # Once Elements has answered a custom-method capability request, an
        # opaque checkout candidate must make an exact round trip.  Otherwise
        # a different wallet returned by Elements could become a false GCash
        # positive merely because both methods use a ``cpmt_...`` identifier.
        method = False
    amounts = evidence["amounts"]
    currencies = evidence["currencies"]
    countries = evidence["countries"]
    amount = next(iter(amounts)) if len(amounts) == 1 else None
    currency = next(iter(currencies)) if len(currencies) == 1 else ""
    country = next(iter(countries)) if len(countries) == 1 else ""

    result: dict[str, Any] = {
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
        "method_available": method,
        "method_evidence_present": bool(evidence["method_evidence_present"]),
        "custom_method_id_discovered": bool(evidence["custom_method_ids"]),
        "trusted_custom_method_matched": bool(
            evidence["trusted_custom_method_matched"]
        ),
        "amount_minor": amount,
        "currency": currency,
        "checkout_country": country,
    }
    if method is True:
        result.update({
            "classification": "eligible",
            "eligible": True,
            "decision": "gcash_available",
            "status": "eligible",
            "label": "GCash available",
        })
    elif method is False:
        result["decision"] = "gcash_unavailable"
    return result


def _chatgpt_headers(
    access_token: str,
    *,
    account_id: str,
    device_id: str,
    cookie_header: str,
    route: str,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": _USER_AGENT,
        "OAI-Device-Id": device_id,
        "x-openai-target-path": route,
        "x-openai-target-route": route,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _stripe_headers(publishable_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Bearer {publishable_key}",
        "Origin": "https://checkout.stripe.com",
        "Referer": "https://checkout.stripe.com/",
        "User-Agent": _USER_AGENT,
    }


def _json_response(response: Any, stage: str) -> Mapping[str, Any]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        retryable = status_code in {408, 425, 429} or status_code >= 500
        code = f"{stage}_http_{status_code}"
        if status_code in {400, 422}:
            # Keep only a stable, non-sensitive reason code. The response body
            # is never returned or persisted.
            try:
                body = response.json()
            except Exception:
                body = None
            detail = str(body.get("detail") or "").lower() if isinstance(body, Mapping) else ""
            if "billing country" in detail and "request country" in detail:
                code = f"{stage}_billing_country_mismatch"
        raise _ProbeFailure(code, retryable=retryable, status_code=status_code)
    try:
        payload = response.json()
    except Exception as exc:
        raise _ProbeFailure(f"{stage}_invalid_json", retryable=True) from exc
    if not isinstance(payload, Mapping):
        raise _ProbeFailure(f"{stage}_invalid_payload", retryable=True)
    return payload


def _post_json(
    session: Any,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: int,
    stage: str,
) -> Mapping[str, Any]:
    try:
        response = session.post(
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


def _post_form(
    session: Any,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: int,
    stage: str,
) -> Mapping[str, Any]:
    try:
        response = session.post(
            url,
            data=dict(payload),
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
    timeout: int,
    stage: str,
    params: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    try:
        request_kwargs: dict[str, Any] = {
            "headers": dict(headers),
            "timeout": timeout,
            "allow_redirects": False,
        }
        if params is not None:
            request_kwargs["params"] = dict(params)
        response = session.get(url, **request_kwargs)
    except Exception as exc:
        raise _ProbeFailure(
            f"{stage}_transport_error",
            retryable=True,
            exception_type=type(exc).__name__,
        ) from exc
    return _json_response(response, stage)


def _checkout_session(payload: Mapping[str, Any]) -> tuple[str, str]:
    session_id = str(
        payload.get("checkout_session_id")
        or payload.get("session_id")
        or payload.get("id")
        or ""
    ).strip()
    if not session_id.startswith(("cs_", "oaics_")):
        raise _ProbeFailure("checkout_session_missing", retryable=True)
    if not re.fullmatch(r"(?:cs|oaics)_[A-Za-z0-9_-]+", session_id):
        raise _ProbeFailure("checkout_session_invalid", retryable=False)
    processor = str(
        payload.get("processor_entity")
        or payload.get("processorEntity")
        or "openai_ie"
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", processor):
        raise _ProbeFailure("checkout_processor_invalid", retryable=False)
    return session_id, processor


def _method_only_checkout_payload() -> dict[str, Any]:
    """Build the browser-compatible PH/PHP checkout needed to list methods."""
    return {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": GCASH_CHECKOUT_COUNTRY,
            "currency": GCASH_CHECKOUT_CURRENCY,
        },
        "checkout_ui_mode": "custom",
    }


def _checkout_payload(*, promo_campaign_id: str = PROMOTION_ID) -> dict[str, Any]:
    """Build the Philippines Checkout contract used by the source workflow.

    GCash is a Philippines/PHP payment method. The selected proxy must exit in
    the same region; otherwise ChatGPT can reject the request as a billing
    country mismatch. The region is intentionally explicit here so the probe
    does not silently report a different country's method set.
    """
    return {
        **_method_only_checkout_payload(),
        "promo_campaign": {
            "promo_campaign_id": str(promo_campaign_id or PROMOTION_ID).strip(),
            "is_coupon_from_query_param": False,
        },
        # The upstream GCash adapter asks Checkout to perform its card/proxy
        # capability check. This is still a checkout capability request; it is
        # not a payment-method creation or confirmation operation.
        "check_card_proxy": True,
    }


def _promotion_update_payload(
    checkout_session_id: str,
    processor: str,
    *,
    promo_campaign_id: str = PROMOTION_ID,
) -> dict[str, Any]:
    """Build the source-compatible promotion update for one checkout.

    The update is deliberately tied to the session returned by Checkout. It
    cannot create a second session or select a different account.
    """
    return {
        "checkout_session_id": checkout_session_id,
        "processor_entity": processor,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": str(promo_campaign_id or PROMOTION_ID).strip(),
            "is_coupon_from_query_param": False,
        },
    }


def _tax_update_payload(
    checkout_session_id: str,
    processor: str,
    payloads: Iterable[Any],
    *,
    checkout_email: str = "",
) -> dict[str, Any]:
    """Build a minimal best-effort tax-sync request.

    The source workflow performs this stage after promotion update. We only
    reuse region data observed in the checkout response and an explicitly
    supplied account email; no fabricated address, card, or payment identity
    is inserted. A tax endpoint rejection is non-fatal to capability reading.
    """
    country = _first_mapping_value(
        payloads,
        {"country", "billing_country", "checkout_country"},
    ).strip().upper() or GCASH_CHECKOUT_COUNTRY
    currency = _first_mapping_value(
        payloads,
        {"currency", "currency_code"},
    ).strip().upper() or GCASH_CHECKOUT_CURRENCY
    payload: dict[str, Any] = {
        "checkout_session_id": checkout_session_id,
        "checkout_email": str(checkout_email or "").strip(),
        "billing_country": country,
        "currency": currency,
        "tax_id": None,
        "processor_entity": processor,
    }
    if country:
        payload["billing_address"] = {"country": country}
    return payload


def _first_mapping_value(payloads: Iterable[Any], names: set[str]) -> str:
    for payload in payloads:
        for item in _walk(payload):
            if not isinstance(item, Mapping):
                continue
            for key, value in item.items():
                if _key(key) in names and isinstance(value, (str, int, float)):
                    text = str(value).strip()
                    if text:
                        return text
    return ""


def _stripe_context(payloads: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge Stripe credentials that may be returned by resolve instead of checkout."""
    payload_list = list(payloads)
    context: dict[str, Any] = dict(payload_list[0]) if payload_list else {}
    for target, names in {
        "publishable_key": {"publishable_key", "stripe_publishable_key"},
        "customer_session_client_secret": {"customer_session_client_secret"},
    }.items():
        if not str(context.get(target) or "").strip():
            value = _first_mapping_value(payload_list, names)
            if value:
                context[target] = value
    return context


def _stripe_init_params(
    checkout: Mapping[str, Any],
    custom_ids: list[str],
) -> tuple[str, dict[str, str]] | None:
    publishable_key = str(
        checkout.get("publishable_key")
        or checkout.get("stripe_publishable_key")
        or checkout.get("publishableKey")
        or ""
    ).strip()
    if not publishable_key.startswith("pk_"):
        return None
    country = str(
        checkout.get("billing_details", {}).get("country")
        if isinstance(checkout.get("billing_details"), Mapping)
        else checkout.get("country") or ""
    ).strip().upper()
    locale, timezone = _BROWSER_PROFILES.get(country, ("en-US", "America/New_York"))
    params: dict[str, str] = {
        "browser_locale": locale,
        "browser_timezone": timezone,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
    }
    return publishable_key, params


def _stripe_params(
    checkout: Mapping[str, Any],
    custom_ids: list[str],
) -> dict[str, str] | None:
    """Backward-compatible wrapper for the former Elements parameter helper."""
    result = _stripe_elements_params(checkout, custom_ids)
    return result[1] if result is not None else None


def _stripe_elements_params(
    checkout: Mapping[str, Any],
    custom_ids: list[str],
) -> tuple[str, dict[str, str]] | None:
    """Build the optional custom-method capability request."""
    customer_secret = str(checkout.get("customer_session_client_secret") or "").strip()
    publishable_key = str(
        checkout.get("publishable_key")
        or checkout.get("stripe_publishable_key")
        or checkout.get("publishableKey")
        or ""
    ).strip()
    configured_id = os.getenv("GCASH_CUSTOM_PAYMENT_METHOD_ID", "").strip()
    ids = list(dict.fromkeys([*custom_ids, configured_id] if configured_id else custom_ids))
    ids = [item for item in ids if _custom_method_id(item)]
    if not customer_secret or not publishable_key.startswith("pk_") or not ids:
        return None
    billing = checkout.get("billing_details")
    raw_currency = billing.get("currency") if isinstance(billing, Mapping) else checkout.get("currency")
    currency = (
        str(raw_currency or GCASH_CHECKOUT_CURRENCY).strip().lower()
        or GCASH_CHECKOUT_CURRENCY.lower()
    )
    raw_country = billing.get("country") if isinstance(billing, Mapping) else checkout.get("country")
    country = (
        str(raw_country or GCASH_CHECKOUT_COUNTRY).strip().upper()
        or GCASH_CHECKOUT_COUNTRY
    )
    locale, timezone = _BROWSER_PROFILES.get(country, ("en-US", "America/New_York"))
    params: dict[str, str] = {
        "customer_session_client_secret": customer_secret,
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": "0",
        "deferred_intent[currency]": currency,
        "currency": currency,
        "key": publishable_key,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(uuid.uuid4()),
        "locale": locale,
        "browser_timezone": timezone,
        "type": "deferred_intent",
        "deferred_intent[setup_future_usage]": "off_session",
        "_stripe_version": _STRIPE_VERSION,
    }
    payment_methods = checkout.get("payment_method_types")
    if isinstance(payment_methods, (list, tuple)):
        for index, method in enumerate(payment_methods):
            if isinstance(method, Mapping):
                method = (
                    method.get("type")
                    or method.get("payment_method_type")
                    or method.get("name")
                    or method.get("id")
                )
            token = _key(method)
            if token:
                params[f"deferred_intent[payment_method_types][{index}]"] = token
    for index, custom_id in enumerate(ids):
        params[f"custom_payment_methods[{index}]"] = custom_id
    return publishable_key, params




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
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Probe whether GCash is exposed, without confirming or starting payment."""
    token = str(access_token or "").strip()
    if not token:
        return gcash_probe_error(
            "missing_access_token",
            retryable=False,
            status="no_at",
            label="No access token",
            checked_at=checked_at,
        )
    timeout = max(5, min(int(timeout or 30), 60))
    stable_device_id = str(device_id or "").strip() or str(uuid.uuid4())
    if session_factory is None:
        from http_client import create_http_session

        session_factory = create_http_session

    session = None
    stage = "session"
    optional_failures: list[_ProbeFailure] = []
    trusted_custom_method_matches: list[str] = []
    custom_method_requires_roundtrip = False
    custom_capability_status = "not_requested"
    custom_capability_response_id_count = 0
    custom_capability_failure_codes: list[str] = []
    custom_capability_failure_types: list[str] = []
    try:
        session = session_factory(
            proxy=str(proxy or "").strip() or None,
            impersonate="chrome110",
        )
        checkout_route = "/backend-api/payments/checkout"
        stage = "checkout"
        checkout_headers = _chatgpt_headers(
            token,
            account_id=account_id,
            device_id=stable_device_id,
            cookie_header=cookie_header,
            route=checkout_route,
        )
        try:
            checkout = _post_json(
                session,
                f"{CHATGPT_PAYMENTS_BASE}/checkout",
                _checkout_payload(),
                checkout_headers,
                timeout,
                stage,
            )
        except _ProbeFailure as exc:
            # The source repository also has a base checkout contract that
            # omits optional promotion/card-proxy fields and applies promotion
            # later. Some otherwise valid accounts reject the richer create
            # payload with a generic 400/422 even though their browser checkout
            # exposes GCash. Retry that exact compatibility shape once with a
            # fresh fingerprint. Never retry a known country mismatch, never
            # remove PH/PHP, and never fall back outside the selected proxy.
            if exc.code not in {"checkout_http_400", "checkout_http_422"}:
                raise
            _LOGGER.info(
                "[gcash_probe] checkout compatibility retry reason=%s",
                exc.code,
            )
            try:
                session.close()
            except Exception:
                pass
            session = session_factory(
                proxy=str(proxy or "").strip() or None,
                impersonate="firefox144",
            )
            compatibility_headers = dict(checkout_headers)
            compatibility_headers["User-Agent"] = _FIREFOX_USER_AGENT
            checkout = _post_json(
                session,
                f"{CHATGPT_PAYMENTS_BASE}/checkout",
                _method_only_checkout_payload(),
                compatibility_headers,
                timeout,
                stage,
            )
        checkout_session_id, processor = _checkout_session(checkout)
        payloads: list[Mapping[str, Any]] = [checkout]

        # Match the reference GCash adapter's promotion-update and tax-sync
        # stages. Both are best-effort capability preparation steps; neither
        # creates a payment method, confirms Checkout, or starts a provider
        # redirect. Their responses are retained as additional evidence.
        checkout_referer = f"https://chatgpt.com/checkout/{processor}/{checkout_session_id}"
        update_route = CHECKOUT_UPDATE_PATH
        stage = "promotion_update"
        try:
            update_headers = _chatgpt_headers(
                token,
                account_id=account_id,
                device_id=stable_device_id,
                cookie_header=cookie_header,
                route=update_route,
            )
            update_headers["Referer"] = checkout_referer
            updated = _post_json(
                session,
                CHECKOUT_UPDATE_URL,
                _promotion_update_payload(
                    checkout_session_id,
                    processor,
                ),
                update_headers,
                timeout,
                stage,
            )
            payloads.append(updated)
        except _ProbeFailure as exc:
            optional_failures.append(exc)

        taxes_route = CHECKOUT_TAXES_PATH
        stage = "taxes"
        try:
            taxes_headers = _chatgpt_headers(
                token,
                account_id=account_id,
                device_id=stable_device_id,
                cookie_header=cookie_header,
                route=taxes_route,
            )
            taxes_headers["Referer"] = checkout_referer
            taxes = _post_json(
                session,
                CHECKOUT_TAXES_URL,
                _tax_update_payload(
                    checkout_session_id,
                    processor,
                    payloads,
                    checkout_email=checkout_email,
                ),
                taxes_headers,
                timeout,
                stage,
            )
            payloads.append(taxes)
        except _ProbeFailure as exc:
            optional_failures.append(exc)

        # Resolve is useful when the initial response only contains a session
        # and credentials. It is best effort because checkout evidence alone
        # can answer the availability question.
        stage = "resolve"
        resolve_route = f"/backend-api/payments/checkout/{processor}/{checkout_session_id}"
        try:
            resolved = _get_json(
                session,
                f"{CHATGPT_PAYMENTS_BASE}/checkout/{processor}/{checkout_session_id}",
                headers=_chatgpt_headers(
                    token,
                    account_id=account_id,
                    device_id=stable_device_id,
                    cookie_header=cookie_header,
                    route=resolve_route,
                ),
                timeout=timeout,
                stage=stage,
            )
            payloads.append(resolved)
        except _ProbeFailure as exc:
            optional_failures.append(exc)
            pass

        discovered = _evidence(payloads)["custom_method_ids"]
        configured_id = _custom_method_id(os.getenv("GCASH_CUSTOM_PAYMENT_METHOD_ID", ""))
        custom_ids_for_probe = list(dict.fromkeys(
            [*discovered, configured_id] if configured_id else discovered
        ))
        checkout_evidence = _evidence(payloads)
        # The request itself is fixed to PH/PHP.  Accept omitted region fields
        # from an upstream response, but reject an explicit mismatch before
        # trusting an opaque custom-method ID.
        ph_php_checkout = (
            checkout_evidence["countries"] in (set(), {GCASH_CHECKOUT_COUNTRY})
            and checkout_evidence["currencies"] in (set(), {GCASH_CHECKOUT_CURRENCY})
        )
        custom_method_requires_roundtrip = bool(custom_ids_for_probe)
        stripe_context = _stripe_context(payloads)
        # Payment Pages init is the read-only capability source for standard
        # method lists. Custom methods use the Elements endpoint below because
        # it accepts the live opaque custom-method IDs.
        stripe_request = _stripe_init_params(stripe_context, custom_ids_for_probe)
        if not custom_ids_for_probe and stripe_request is not None and checkout_session_id.startswith("cs_"):
            publishable_key, stripe_params = stripe_request
            stage = "stripe_capability"
            try:
                stripe = _post_form(
                    session,
                    STRIPE_PAYMENT_PAGE_INIT_URL.format(session_id=checkout_session_id),
                    stripe_params,
                    _stripe_headers(publishable_key),
                    timeout=timeout,
                    stage=stage,
                )
                payloads.append(stripe)
            except _ProbeFailure as exc:
                optional_failures.append(exc)

        # If a custom ID is available but Payment Pages did not name GCash,
        # ask the Elements endpoint for the custom-method display metadata.
        if custom_ids_for_probe:
            elements_request = _stripe_elements_params(stripe_context, custom_ids_for_probe)
            if elements_request is not None:
                custom_capability_status = "requested"
                publishable_key, elements_params = elements_request
                stage = "stripe_custom_capability"
                # A proxy/TLS path can reject one browser fingerprint. Retry
                # transport failures once with a fresh session and a second
                # Stripe fingerprint; HTTP/auth responses are not retried.
                for attempt_index, stripe_impersonate in enumerate(
                    ("firefox144", "chrome110")
                ):
                    elements_session = None
                    try:
                        # Stripe Elements is a separate browser-origin
                        # capability request. Keep it out of the authenticated
                        # ChatGPT session so cookies, TLS fingerprints, and
                        # proxy state do not cross-contaminate the two hosts.
                        elements_session = session_factory(
                            proxy=str(proxy or "").strip() or None,
                            impersonate=stripe_impersonate,
                        )
                        stripe = _get_json(
                            elements_session,
                            STRIPE_ELEMENTS_URL,
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Bearer {publishable_key}",
                                "Origin": "https://js.stripe.com",
                                "Referer": "https://js.stripe.com/",
                                "User-Agent": _STRIPE_USER_AGENTS[stripe_impersonate],
                            },
                            params=elements_params,
                            timeout=timeout,
                            stage=stage,
                        )
                        stripe_custom_ids = _evidence([stripe])["custom_method_ids"]
                        custom_capability_response_id_count = len(stripe_custom_ids)
                        # Stripe Elements returns the custom-method entries that
                        # it accepted for this exact customer session. A
                        # checkout ID that makes the same round trip is stronger
                        # capability evidence than a merchant-defined/localized
                        # display label; labels need not contain "GCash".
                        accepted_ids = (
                            set(custom_ids_for_probe).intersection(stripe_custom_ids)
                            if ph_php_checkout
                            else set()
                        )
                        trusted_custom_method_matches.extend(sorted(accepted_ids))
                        custom_capability_status = (
                            "accepted" if accepted_ids else "rejected"
                        )
                        payloads.append(stripe)
                        break
                    except _ProbeFailure as exc:
                        should_retry = (
                            exc.code.endswith("_transport_error")
                            and attempt_index == 0
                        )
                        if should_retry:
                            continue
                        optional_failures.append(exc)
                        custom_capability_failure_codes.append(exc.code)
                        if exc.exception_type:
                            custom_capability_failure_types.append(exc.exception_type)
                        custom_capability_status = "failed"
                        break
                    except Exception as exc:
                        failure = _ProbeFailure(
                            "stripe_custom_capability_transport_error",
                            retryable=True,
                            exception_type=type(exc).__name__,
                        )
                        if attempt_index == 0:
                            continue
                        optional_failures.append(failure)
                        custom_capability_failure_codes.append(failure.code)
                        if failure.exception_type:
                            custom_capability_failure_types.append(failure.exception_type)
                        custom_capability_status = "failed"
                        break
                    finally:
                        if elements_session is not None and elements_session is not session:
                            try:
                                elements_session.close()
                            except Exception:
                                pass
            else:
                custom_capability_status = "not_configured"

        _LOGGER.info(
            "[gcash_probe] custom capability status=%s candidates=%d response_ids=%d matches=%d ph_php=%s failures=%s",
            custom_capability_status,
            len(custom_ids_for_probe),
            custom_capability_response_id_count,
            len(trusted_custom_method_matches),
            ph_php_checkout,
            ",".join(custom_capability_failure_codes) or "none",
        )

        evidence = _evidence(payloads)
        if evidence["method_available"] is None and optional_failures:
            auth_failure = next(
                (
                    failure
                    for failure in optional_failures
                    if failure.code.startswith("resolve_http_")
                    and failure.status_code in {401, 403}
                ),
                None,
            )
            if auth_failure is not None:
                return gcash_probe_error(
                    auth_failure.code,
                    retryable=auth_failure.retryable,
                    status="token_invalid",
                    label="Access token invalid",
                    checked_at=checked_at,
                )
            return gcash_unavailable("gcash_evidence_incomplete", checked_at=checked_at)
        result = classify_gcash_evidence(
            payloads,
            require_zero=require_zero,
            checked_at=checked_at,
            trusted_custom_method_ids=trusted_custom_method_matches,
            require_trusted_custom_method_match=custom_method_requires_roundtrip,
        )
        result["custom_method_probe_status"] = custom_capability_status
        result["custom_method_probe_failure"] = (
            custom_capability_failure_codes[0]
            if custom_capability_failure_codes
            else ""
        )
        result["custom_method_probe_exception"] = (
            custom_capability_failure_types[0]
            if custom_capability_failure_types
            else ""
        )
        return result
    except _ProbeFailure as exc:
        if exc.status_code in {400, 422}:
            return gcash_unavailable(exc.code, checked_at=checked_at)
        status = "token_invalid" if exc.code.endswith(("http_401", "http_403")) else "error"
        return gcash_probe_error(
            exc.code,
            retryable=exc.retryable,
            status=status,
            checked_at=checked_at,
        )
    except Exception:
        return gcash_probe_error(
            f"{stage}_unexpected_error",
            retryable=True,
            checked_at=checked_at,
        )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


__all__ = [
    "classify_gcash_evidence",
    "gcash_probe_error",
    "gcash_unavailable",
    "normalize_gcash_result",
    "probe_gcash",
]
