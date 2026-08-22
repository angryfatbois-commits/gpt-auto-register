"""Focused unit tests for the strict HAR-backed capability probe."""

from __future__ import annotations

import unittest

from gcash_probe import (
    CHECKOUT_URL,
    STRIPE_ELEMENTS_URL,
    classify_gcash_evidence,
    normalize_gcash_result,
    probe_checkout_eligibility,
    probe_gcash,
)


METHOD_ID = "cpmt_synthetic_gcash"


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.cookies = {}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def post_isolated(self, url, **kwargs):
        return self.post(url, **kwargs)

    def close(self):
        self.closed = True


def accounts_payload(campaign=True):
    return {
        "accounts": {
            "default": {
                "account": {"plan_type": "free", "is_deactivated": False},
                "entitlement": {
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
                "eligible_promo_campaigns": (
                    {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "discount": {"percentage": 100},
                                "duration": {"num_periods": 1, "period": "month"},
                            },
                        }
                    }
                    if campaign
                    else {}
                ),
            }
        }
    }


def checkout_payload(amount=0, method_id=METHOD_ID):
    return {
        "checkout_session_id": "cs_synthetic",
        "publishable_key": "pk_synthetic",
        "processor_entity": "openai_llc",
        "customer_session_client_secret": "cuss_synthetic",
        "billing_details": {"country": "PH", "currency": "PHP"},
        "payment_method_types": ["card", "link"],
        "custom_payment_methods": [{"id": method_id}],
        "checkout_state": {
            "currency": "php",
            "total": {"total": {"minorUnitsAmount": amount}},
        },
        "one_click_trial_eligible": False,
    }


def stripe_payload(method_id=METHOD_ID, label="GCash"):
    return {
        "payment_method_preference": {"country_code": "VN"},
        "custom_payment_method_data": [
            {"type": method_id, "display_name": label}
        ],
    }


def run_probe(*, checkout=None, stripe=None, accounts=None, proxy="http://proxy.test:8080",
              chatgpt_responses=None):
    chatgpt = _Session(
        list(chatgpt_responses)
        if chatgpt_responses is not None
        else [
            _Response(accounts if accounts is not None else accounts_payload()),
            _Response(checkout if checkout is not None else checkout_payload()),
        ]
    )
    stripe_session = _Session([
        _Response(stripe if stripe is not None else stripe_payload())
    ])
    sessions = [chatgpt, stripe_session]
    factories = []

    def factory(**kwargs):
        factories.append(kwargs)
        return sessions.pop(0)

    result = probe_gcash(
        "access-token",
        proxy=proxy,
        device_id="device-stable",
        session_factory=factory,
        checked_at=10,
    )
    return result, chatgpt, stripe_session, factories


class GCashClassificationTests(unittest.TestCase):
    def test_har_shaped_payload_is_available_with_zero_due(self):
        result = classify_gcash_evidence(
            [checkout_payload(), stripe_payload()],
            checked_at=10,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["amount_minor"], 0)
        self.assertIs(result["zero_payment"], True)

    def test_positive_due_does_not_change_method_eligibility(self):
        result = classify_gcash_evidence(
            [checkout_payload(amount=98214), stripe_payload()]
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["amount_minor"], 98214)
        self.assertIs(result["zero_payment"], False)

    def test_id_mismatch_label_mismatch_and_missing_metadata_fail_closed(self):
        cases = (
            [checkout_payload(), stripe_payload("cpmt_other")],
            [checkout_payload(), stripe_payload(label="Other wallet")],
            [checkout_payload(), {}],
        )
        decisions = (
            "gcash_method_id_mismatch",
            "gcash_label_missing",
            "gcash_metadata_missing",
        )
        for payloads, decision in zip(cases, decisions):
            with self.subTest(decision=decision):
                result = classify_gcash_evidence(payloads)
                self.assertEqual(result["classification"], "ineligible")
                self.assertEqual(result["decision"], decision)

    def test_malformed_total_never_becomes_zero(self):
        checkout = checkout_payload()
        checkout["checkout_state"]["total"]["total"]["minorUnitsAmount"] = "not-a-number"

        result = classify_gcash_evidence([checkout, stripe_payload()])

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "gcash_amount_missing")
        self.assertIsNone(result["amount_minor"])
        self.assertIsNone(result["zero_payment"])

    def test_negative_total_is_incomplete_evidence(self):
        checkout = checkout_payload(amount=-1)

        result = classify_gcash_evidence([checkout, stripe_payload()])

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "gcash_amount_missing")
        self.assertIsNone(result["amount_minor"])

    def test_legacy_unknown_is_normalized_to_binary_unavailable(self):
        result = normalize_gcash_result({
            "classification": "unknown",
            "eligible": None,
            "status": "unknown",
            "decision": "checkout_timeout",
        })

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["label"], "GCash unavailable")

    def test_opaque_method_flag_without_a_verified_label_fails_closed(self):
        result = normalize_gcash_result({
            "method_available": True,
            "custom_method_id_discovered": True,
        })

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])

    def test_untrusted_diagnostics_are_removed_from_the_result(self):
        result = normalize_gcash_result({
            "classification": "ineligible",
            "custom_method_probe_status": "failed",
            "custom_method_probe_failure": "Bearer secret-token",
            "custom_method_probe_exception": "ConnectionError",
            "auth_refresh_status": "cookie-secret",
            "currency": "PHP<script>",
            "checkout_country": "PH\r\nInjected",
        })

        self.assertEqual(result["custom_method_probe_status"], "failed")
        self.assertEqual(result["custom_method_probe_failure"], "")
        self.assertEqual(result["custom_method_probe_exception"], "connectionerror")
        self.assertEqual(result["auth_refresh_status"], "")
        self.assertEqual(result["currency"], "")
        self.assertEqual(result["checkout_country"], "")
        self.assertNotIn("secret-token", repr(result))

    def test_malformed_timestamp_does_not_break_normalization(self):
        result = normalize_gcash_result({"checked_at": "not-a-timestamp"})

        self.assertIsInstance(result["checked_at"], float)

    def test_negative_amount_cannot_survive_normalization_as_eligible(self):
        result = normalize_gcash_result({
            "classification": "eligible",
            "eligible": True,
            "amount_minor": -1,
        })

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["amount_minor"])
        self.assertEqual(result["amount_status"], "unavailable")


class GCashNetworkProbeTests(unittest.TestCase):
    def test_missing_or_invalid_token_never_creates_a_session(self):
        factories = []
        missing = probe_gcash("", session_factory=lambda **kwargs: factories.append(kwargs))
        invalid = probe_gcash(
            "token\r\nInjected: value",
            session_factory=lambda **kwargs: factories.append(kwargs),
        )

        self.assertEqual(missing["status"], "no_at")
        self.assertEqual(invalid["status"], "token_invalid")
        self.assertEqual(factories, [])

    def test_exact_three_requests_use_one_route_and_same_browser_identity(self):
        result, chatgpt, stripe, factories = run_probe()

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(
            [(method, url) for method, url, _ in chatgpt.calls],
            [
                ("GET", chatgpt.calls[0][1]),
                ("POST", CHECKOUT_URL),
            ],
        )
        self.assertEqual(
            [(method, url) for method, url, _ in stripe.calls],
            [("GET", STRIPE_ELEMENTS_URL)],
        )
        self.assertEqual(factories[0], factories[1])
        self.assertEqual(factories[0]["proxy"], "http://proxy.test:8080")
        self.assertEqual(
            chatgpt.calls[1][2]["headers"]["User-Agent"],
            stripe.calls[0][2]["headers"]["User-Agent"],
        )

    def test_checkout_payload_is_exact_and_promo_is_retained_on_transient_retry(self):
        checkout = checkout_payload()
        first = _Response({"temporary": True}, status_code=503)
        result, chatgpt, _, _ = run_probe(
            chatgpt_responses=[_Response(accounts_payload()), first, _Response(checkout)]
        )

        self.assertEqual(result["classification"], "eligible")
        payloads = [call[2]["json"] for call in chatgpt.calls if call[0] == "POST"]
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0], {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        })
        self.assertNotIn("check_card_proxy", payloads[0])

    def test_vn_stripe_preference_and_non_ph_proxy_do_not_invalidate_gcash(self):
        result, _, stripe, factories = run_probe(proxy="http://proxy.vn:8080")

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["checkout_country"], "PH")
        self.assertEqual(result["currency"], "PHP")
        self.assertEqual(factories[0]["proxy"], "http://proxy.vn:8080")
        self.assertEqual(factories[1]["proxy"], "http://proxy.vn:8080")
        self.assertEqual(
            stripe.calls[0][2]["params"]["locale"],
            "en-US",
        )
        self.assertEqual(
            stripe.calls[0][2]["params"]["_stripe_version"],
            "2025-03-31.basil",
        )
        self.assertNotIn("browser_timezone", stripe.calls[0][2]["params"])
        self.assertFalse(any(
            "client_betas" in key for key in stripe.calls[0][2]["params"]
        ))

    def test_forbidden_payment_stages_are_never_called(self):
        result, chatgpt, stripe, _ = run_probe()
        urls = [url for _, url, _ in [*chatgpt.calls, *stripe.calls]]

        self.assertEqual(result["classification"], "eligible")
        self.assertFalse(any(
            any(fragment in url for fragment in (
                "/checkout/update", "/checkout/taxes", "/payment_pages/",
                "/confirm", "/subscribe", "/payment_intents",
                "/custom_payment_method/start", "/resolve",
            ))
            for url in urls
        ))

    def test_result_does_not_expose_tokens_secrets_ids_or_proxy(self):
        result, _, _, _ = run_probe()
        serialized = repr(result).lower()

        for secret in (
            "access-token", "pk_synthetic", "cuss_synthetic",
            "cpmt_", "proxy.test", "checkout_session_id",
        ):
            self.assertNotIn(secret, serialized)

    def test_accounts_check_failure_keeps_plus_and_gcash_results_safe(self):
        chatgpt = _Session([
            _Response({}, status_code=503),
            _Response(checkout_payload()),
        ])
        stripe = _Session([_Response(stripe_payload())])
        sessions = [chatgpt, stripe]

        def factory(**kwargs):
            return sessions.pop(0)

        workflow = probe_checkout_eligibility(
            "access-token",
            proxy="http://proxy.test:8080",
            session_factory=factory,
        )

        self.assertEqual(workflow["gcash"]["classification"], "eligible")
        self.assertEqual(workflow["plus"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
