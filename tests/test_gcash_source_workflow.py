"""Contract tests for the sanitized, HAR-backed checkout capability workflow.

All fixtures in this module are synthetic. They preserve only the response
shape and safe values needed by the classifier; no raw HAR content, account
credential, checkout secret, or live opaque method identifier is stored here.
"""

from __future__ import annotations

import unittest
import base64
import json
import time

from gcash_probe import probe_checkout_eligibility


CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
STRIPE_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
SYNTHETIC_METHOD_ID = "cpmt_synthetic_gcash"


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.cookies = {}

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def post_isolated(self, url, **kwargs):
        return self.post(url, **kwargs)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _jwt(account_id="account-stable", *, issued_at=None):
    """Build a synthetic account-bound token for session-refresh tests."""
    issued_at = int(time.time() if issued_at is None else issued_at)

    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = {"alg": "RS256", "kid": "synthetic-key", "typ": "JWT"}
    payload = {
        "iss": "https://auth.openai.com",
        "aud": ["https://api.openai.com/v1"],
        "sub": "synthetic-user",
        "client_id": "synthetic-client",
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    return f"{encode(header)}.{encode(payload)}.synthetic-signature"


def _accounts_check(*, campaign=True):
    campaigns = {}
    if campaign:
        campaigns = {
            "plus": {
                "id": "plus-1-month-free",
                "metadata": {
                    "title": "Try Plus free for 1 month",
                    "plan_name": "chatgptplusplan",
                    "discount": {"percentage": 100},
                    "duration": {"num_periods": 1, "period": "month"},
                    "price_period": "recurring",
                    "promotion_type": "discount",
                },
            }
        }
    return {
        "accounts": {
            "default": {
                "account": {"plan_type": "free", "is_deactivated": False},
                "entitlement": {
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
                "eligible_promo_campaigns": campaigns,
            }
        }
    }


def _checkout(*, amount=0, method_id=SYNTHETIC_METHOD_ID):
    return {
        "tag": "custom_checkout_session",
        "checkout_session_id": "cs_synthetic_checkout",
        "publishable_key": "pk_synthetic_publishable",
        "processor_entity": "openai_llc",
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "PH", "currency": "PHP"},
        "status": "open",
        "payment_status": "unpaid",
        "customer_session_client_secret": "cuss_synthetic_customer_session",
        "payment_method_types": ["card", "link"],
        "custom_payment_methods": [
            {"id": method_id, "options": {"type": "static"}}
        ],
        "checkout_state": {
            "currency": "php",
            "canConfirm": False,
            "total": {
                "subtotal": {"minorUnitsAmount": 98214},
                "discount": {"minorUnitsAmount": 98214},
                "taxInclusive": {"minorUnitsAmount": 0},
                "total": {"minorUnitsAmount": amount},
            },
        },
        # The real response contains this false value. It is not an
        # eligibility verdict and must not override accounts/check evidence.
        "one_click_trial_eligible": False,
    }


def _stripe(*, method_id=SYNTHETIC_METHOD_ID, label="GCash", country="VN"):
    return {
        "merchant_country": "US",
        "merchant_currency": "usd",
        "payment_method_preference": {
            "country_code": country,
            "ordered_payment_method_types": ["card"],
        },
        "custom_payment_method_data": [
            {
                "type": method_id,
                "display_name": label,
                "is_preset": True,
                "error": None,
            }
        ],
    }


def _run(*, accounts=None, checkout=None, stripe=None, proxy="http://proxy.test:8080"):
    chatgpt = _Session([
        _Response(accounts if accounts is not None else _accounts_check()),
        _Response(checkout if checkout is not None else _checkout()),
    ])
    elements = _Session([_Response(stripe if stripe is not None else _stripe())])
    sessions = [chatgpt, elements]
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return sessions.pop(0)

    result = probe_checkout_eligibility(
        "synthetic-access-token",
        device_id="11111111-2222-4333-8444-555555555555",
        proxy=proxy,
        session_factory=factory,
        checked_at=10.0,
    )
    return result, chatgpt, elements, factory_calls


class GCashHarWorkflowTests(unittest.TestCase):
    def test_exact_three_stage_workflow_returns_all_independent_verdicts(self):
        result, chatgpt, elements, factories = _run()

        self.assertEqual(result["plus"]["status"], "plus_eligible")
        self.assertEqual(result["plus"]["discount_percentage"], 100)
        self.assertEqual(result["plus"]["duration_periods"], 1)
        self.assertEqual(result["plus"]["duration_unit"], "month")
        self.assertEqual(result["gcash"]["classification"], "eligible")
        self.assertEqual(result["gcash"]["amount_minor"], 0)
        self.assertIs(result["gcash"]["zero_payment"], True)

        self.assertEqual(
            [(method, url) for method, url, _ in chatgpt.calls],
            [
                ("GET", chatgpt.calls[0][1]),
                ("POST", CHECKOUT_URL),
            ],
        )
        self.assertIn(ACCOUNTS_CHECK_PATH, chatgpt.calls[0][1])
        self.assertEqual(
            [(method, url) for method, url, _ in elements.calls],
            [("GET", STRIPE_ELEMENTS_URL)],
        )
        all_urls = [url for _, url, _ in [*chatgpt.calls, *elements.calls]]
        self.assertFalse(any(
            fragment in url
            for url in all_urls
            for fragment in (
                "/checkout/update",
                "/checkout/taxes",
                "/payment_pages/",
                "/confirm",
                "/subscribe",
                "/payment_intents",
                "/custom_payment_method/start",
            )
        ))

        self.assertEqual(factories[0], factories[1])
        self.assertEqual(factories[0]["proxy"], "http://proxy.test:8080")
        checkout_headers = chatgpt.calls[1][2]["headers"]
        stripe_headers = elements.calls[0][2]["headers"]
        self.assertEqual(checkout_headers["User-Agent"], stripe_headers["User-Agent"])
        self.assertNotIn("Cookie", stripe_headers)
        self.assertNotIn("ChatGPT-Account-Id", stripe_headers)

    def test_checkout_request_exactly_matches_the_observed_payload(self):
        _, chatgpt, _, _ = _run()

        payload = chatgpt.calls[1][2]["json"]
        self.assertEqual(payload, {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        })
        self.assertNotIn("check_card_proxy", payload)

    def test_stripe_query_matches_the_observed_custom_elements_request(self):
        _, _, elements, _ = _run()

        params = elements.calls[0][2]["params"]
        self.assertEqual(set(params), {
            "_stripe_version",
            "currency",
            "custom_payment_methods[0]",
            "customer_session_client_secret",
            "deferred_intent[amount]",
            "deferred_intent[currency]",
            "deferred_intent[mode]",
            "deferred_intent[payment_method_types][0]",
            "deferred_intent[payment_method_types][1]",
            "deferred_intent[setup_future_usage]",
            "elements_init_source",
            "key",
            "locale",
            "referrer_host",
            "stripe_js_id",
            "type",
        })
        self.assertEqual(params["_stripe_version"], "2025-03-31.basil")
        self.assertEqual(params["locale"], "en-US")
        self.assertEqual(params["deferred_intent[amount]"], "0")
        self.assertEqual(params["deferred_intent[currency]"], "php")
        self.assertEqual(params["custom_payment_methods[0]"], SYNTHETIC_METHOD_ID)
        self.assertNotIn("browser_timezone", params)
        self.assertFalse(any("client_betas" in key for key in params))

    def test_vietnam_preference_country_does_not_hide_ph_checkout_gcash(self):
        result, _, _, _ = _run(stripe=_stripe(country="VN"))

        self.assertEqual(result["gcash"]["classification"], "eligible")
        self.assertEqual(result["gcash"]["checkout_country"], "PH")
        self.assertEqual(result["gcash"]["currency"], "PHP")

    def test_positive_due_keeps_gcash_available_but_is_not_zero_payment(self):
        result, _, elements, _ = _run(checkout=_checkout(amount=98214))

        self.assertEqual(result["gcash"]["classification"], "eligible")
        self.assertEqual(result["gcash"]["amount_minor"], 98214)
        self.assertIs(result["gcash"]["zero_payment"], False)
        self.assertEqual(
            elements.calls[0][2]["params"]["deferred_intent[amount]"],
            "98214",
        )

    def test_missing_campaign_does_not_hide_matching_gcash(self):
        result, _, _, _ = _run(accounts=_accounts_check(campaign=False))

        self.assertEqual(result["plus"]["status"], "free")
        self.assertEqual(result["gcash"]["classification"], "eligible")

    def test_exact_id_round_trip_and_explicit_gcash_label_are_required(self):
        cases = (
            (_stripe(method_id="cpmt_synthetic_other"), "gcash_method_id_mismatch"),
            (_stripe(label="Other wallet"), "gcash_label_missing"),
            ({"payment_method_preference": {"country_code": "VN"}}, "gcash_metadata_missing"),
        )

        for stripe, decision in cases:
            with self.subTest(decision=decision):
                result, _, _, _ = _run(stripe=stripe)
                self.assertEqual(result["gcash"]["classification"], "ineligible")
                self.assertEqual(result["gcash"]["decision"], decision)

    def test_total_due_uses_only_the_exact_checkout_state_path(self):
        checkout = _checkout()
        checkout["checkout_state"]["total"]["total"] = {"minorUnitsAmount": "bad"}
        checkout["checkout_state"]["lineItems"] = [
            {
                "subtotal": {"minorUnitsAmount": 98214},
                "discount": {"minorUnitsAmount": 98214},
            }
        ]
        checkout["amount_due"] = 0

        result, _, elements, _ = _run(checkout=checkout)

        self.assertEqual(result["gcash"]["classification"], "ineligible")
        self.assertEqual(result["gcash"]["decision"], "gcash_amount_missing")
        self.assertIsNone(result["gcash"]["amount_minor"])
        self.assertIsNone(result["gcash"]["zero_payment"])
        self.assertEqual(result["gcash"]["amount_status"], "unavailable")
        self.assertEqual(
            elements.calls[0][2]["params"]["deferred_intent[amount]"],
            "0",
        )

    def test_result_envelope_never_exposes_request_secrets_or_opaque_ids(self):
        result, _, _, _ = _run()

        serialized = repr(result).lower()
        for secret_fragment in (
            "synthetic-access-token",
            "cuss_",
            "pk_",
            "cpmt_",
            "proxy.test",
            "cookie",
            "checkout_session_id",
        ):
            self.assertNotIn(secret_fragment, serialized)

    def test_bound_auth_session_refresh_precedes_accounts_check_and_updates_checkout(self):
        old_token = _jwt()
        fresh_token = _jwt(issued_at=int(time.time()) + 1)
        chatgpt = _Session([
            _Response({"accessToken": fresh_token}),
            _Response(_accounts_check()),
            _Response(_checkout()),
        ])
        chatgpt.cookies = {
            "__Secure-next-auth.session-token": "fresh-session-cookie",
            "oai-did": "11111111-2222-4333-8444-555555555555",
            "unrelated": "must-not-forward",
        }
        elements = _Session([_Response(_stripe())])
        sessions = [chatgpt, elements]

        def factory(**_kwargs):
            return sessions.pop(0)

        result = probe_checkout_eligibility(
            old_token,
            account_id="account-stable",
            device_id="11111111-2222-4333-8444-555555555555",
            cookie_header=(
                "oai-did=11111111-2222-4333-8444-555555555555; "
                "__Secure-next-auth.session-token=old-session-cookie"
            ),
            session_factory=factory,
            checked_at=10.0,
        )

        self.assertEqual(result["gcash"]["classification"], "eligible")
        self.assertEqual(result["gcash"]["auth_refresh_status"], "refreshed")
        self.assertEqual(chatgpt.calls[0][1], "https://chatgpt.com/api/auth/session")
        accounts_call = chatgpt.calls[1]
        checkout_call = chatgpt.calls[2]
        self.assertEqual(accounts_call[0], "GET")
        self.assertEqual(checkout_call[0], "POST")
        self.assertEqual(checkout_call[2]["headers"]["Authorization"], f"Bearer {fresh_token}")
        self.assertIn("fresh-session-cookie", checkout_call[2]["headers"]["Cookie"])
        self.assertNotIn("unrelated", checkout_call[2]["headers"]["Cookie"])
        self.assertEqual(chatgpt.cookies, {})

    def test_chatgpt_requests_include_browser_client_hints_from_the_verified_profile(self):
        result, chatgpt, _, _ = _run()
        self.assertEqual(result["gcash"]["classification"], "eligible")
        for call in chatgpt.calls:
            headers = call[2]["headers"]
            self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
            self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
            self.assertEqual(headers["sec-fetch-site"], "same-origin")
            self.assertIn("Chromium", headers["sec-ch-ua"])


if __name__ == "__main__":
    unittest.main()
