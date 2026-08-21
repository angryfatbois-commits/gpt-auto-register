import unittest


from gcash_probe import classify_gcash_evidence, probe_gcash


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        # The source-compatible probe performs best-effort update/tax stages.
        # Keep legacy fixtures focused on their original checkout/resolve
        # responses while still recording those additional calls.
        if url.endswith("/payments/checkout/update"):
            return FakeResponse({"discountAmounts": {"total": 0}})
        if url.endswith("/payments/checkout/taxes"):
            return FakeResponse({"ok": True})
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _gcash_payload(amount=0, currency="php"):
    return {
        "total_summary": {"due": amount},
        "currency": currency,
        "custom_payment_method_data": [
            {"id": "cpmt_live_discovered", "display_name": "GCash"}
        ],
    }


class GCashClassificationTests(unittest.TestCase):
    def test_explicit_gcash_zero_due_php_is_eligible(self):
        result = classify_gcash_evidence([_gcash_payload()], checked_at=10.0)

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")
        self.assertEqual(result["amount_minor"], 0)
        self.assertEqual(result["currency"], "PHP")
        self.assertTrue(result["method_available"])
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["label"], "GCash available")

    def test_explicit_gcash_positive_due_php_is_eligible(self):
        result = classify_gcash_evidence([_gcash_payload(amount=115000)])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")
        self.assertEqual(result["amount_minor"], 115000)

    def test_wrong_currency_does_not_hide_an_explicit_gcash_method(self):
        result = classify_gcash_evidence([_gcash_payload(currency="usd")])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")

    def test_explicit_custom_method_list_without_gcash_is_ineligible(self):
        payload = {
            "total_summary": {"due": 0},
            "currency": "php",
            "custom_payment_method_data": [{"id": "cpmt_other", "display_name": "Other"}],
        }

        result = classify_gcash_evidence([payload])

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "gcash_unavailable")

    def test_nested_explicit_gcash_method_is_available(self):
        result = classify_gcash_evidence([
            {
                "elements_options": {
                    "ordered_payment_method_types": [
                        {"payment_method_type": "GCash"},
                    ],
                },
            }
        ])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["method_available"])

    def test_missing_amount_is_irrelevant_to_method_detection(self):
        payload = {
            "currency": "php",
            "custom_payment_method_data": [
                {"id": "cpmt_discovered", "display_name": "GCash"}
            ],
        }

        result = classify_gcash_evidence([payload])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")
        self.assertTrue(result["conclusive"])
        self.assertFalse(result["retryable"])

    def test_conflicting_amounts_do_not_hide_an_explicit_gcash_method(self):
        result = classify_gcash_evidence([_gcash_payload(0), _gcash_payload(100)])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")

    def test_conflicting_currencies_do_not_hide_an_explicit_gcash_method(self):
        result = classify_gcash_evidence([
            _gcash_payload(0, "php"),
            _gcash_payload(0, "usd"),
        ])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")

    def test_negative_amount_is_irrelevant_to_method_detection(self):
        result = classify_gcash_evidence([_gcash_payload(amount=-1)])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")

    def test_missing_method_evidence_is_ineligible(self):
        result = classify_gcash_evidence([{"currency": "php", "amount_due": 0}])

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["decision"], "gcash_evidence_missing")

    def test_missing_currency_is_ineligible(self):
        payload = {
            "amount_due": 0,
            "custom_payment_method_data": [
                {"id": "cpmt_discovered", "display_name": "GCash"}
            ],
        }

        result = classify_gcash_evidence([payload])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_available")

    def test_live_custom_method_id_without_display_label_is_a_gcash_candidate(self):
        result = classify_gcash_evidence([
            {
                "custom_payment_methods": ["cpmt_live_discovered"],
            }
        ])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["method_available"])
        self.assertTrue(result["custom_method_id_discovered"])

    def test_scalar_custom_method_id_is_a_gcash_candidate(self):
        """Checkout may return the opaque custom method in a scalar field."""
        result = classify_gcash_evidence([
            {"custom_payment_method_type_id": "cpmt_scalar_discovered"}
        ])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["method_available"])
        self.assertTrue(result["custom_method_id_discovered"])

    def test_scalar_custom_method_object_with_non_gcash_label_is_ineligible(self):
        result = classify_gcash_evidence([
            {
                "custom_payment_method": {
                    "id": "cpmt_scalar_discovered",
                    "display_name": "Other wallet",
                }
            }
        ])

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertFalse(result["method_available"])

    def test_unrelated_non_gcash_custom_method_does_not_override_gcash_candidate(self):
        result = classify_gcash_evidence([
            {"custom_payment_methods": ["cpmt_candidate_one"]},
            {
                "custom_payment_method_data": [
                    {"id": "cpmt_other_wallet", "display_name": "Other wallet"}
                ]
            },
        ])

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["method_available"])

    def test_generic_custom_payment_placeholder_without_id_is_ineligible(self):
        result = classify_gcash_evidence([
            {"payment_method_types": ["card", "custom_payment_method"]}
        ])

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["decision"], "gcash_unavailable")

    def test_explicit_non_gcash_custom_label_overrides_an_opaque_candidate(self):
        result = classify_gcash_evidence([
            {"custom_payment_methods": ["cpmt_live_discovered"]},
            {
                "custom_payment_method_data": [
                    {"id": "cpmt_live_discovered", "display_name": "Other wallet"}
                ]
            },
        ])

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["method_available"])

    def test_accepted_configured_gcash_id_overrides_a_nonliteral_display_label(self):
        result = classify_gcash_evidence(
            [{
                "custom_payment_method_data": [{
                    "type": "cpmt_configured_one",
                    "display_name": "Localized wallet label",
                }],
            }],
            trusted_custom_method_ids=["cpmt_configured_one"],
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["method_available"])

    def test_nonliteral_label_without_configured_id_remains_ineligible(self):
        result = classify_gcash_evidence([{
            "custom_payment_method_data": [{
                "type": "cpmt_untrusted_wallet",
                "display_name": "Localized wallet label",
            }],
        }])

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])


class GCashNetworkProbeTests(unittest.TestCase):
    def test_missing_access_token_returns_unavailable_without_network(self):
        factories = []

        result = probe_gcash(
            "",
            session_factory=lambda **kwargs: factories.append(kwargs),
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(result["decision"], "missing_access_token")
        self.assertEqual(result["status"], "no_at")
        self.assertEqual(factories, [])

    def test_rejects_checkout_session_path_injection(self):
        session = FakeSession([
            FakeResponse({
                "checkout_session_id": "cs_valid/../../confirm",
                "processor_entity": "openai_ie",
            })
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(result["decision"], "checkout_session_invalid")
        self.assertEqual(len(session.calls), 1)

    def test_opaque_custom_method_id_is_discovered_then_verified_by_stripe(self):
        checkout = {
            "checkout_session_id": "cs_test_dynamic_method",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_dynamic",
            "customer_session_client_secret": "cuss_dynamic",
            "custom_payment_methods": ["cpmt_dynamic_from_checkout"],
            "total_summary": {"due": 0},
            "currency": "php",
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({
                "custom_payment_methods": ["cpmt_dynamic_from_checkout"],
                "total_summary": {"due": 0},
                "currency": "php",
            }),
            FakeResponse({
                "custom_payment_method_data": [
                    {"id": "cpmt_dynamic_from_checkout", "display_name": "GCash"}
                ],
                "total_summary": {"due": 0},
                "currency": "php",
            }),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(len(session.calls), 5)
        checkout_call = session.calls[0]
        self.assertEqual(
            checkout_call[2]["json"]["promo_campaign"]["promo_campaign_id"],
            "plus-1-month-free",
        )
        self.assertTrue(checkout_call[2]["json"]["check_card_proxy"])
        stripe_call = session.calls[-1]
        self.assertEqual(
            stripe_call[1], "https://api.stripe.com/v1/elements/sessions",
        )
        self.assertEqual(
            stripe_call[2]["params"]["custom_payment_methods[0]"],
            "cpmt_dynamic_from_checkout",
        )

    def test_checkout_method_roundtrip_accepts_a_nonliteral_elements_label(self):
        """Stripe acceptance is stronger evidence than a merchant-defined label."""
        method_id = "cpmt_dynamic_roundtrip"
        checkout = {
            "checkout_session_id": "cs_test_dynamic_roundtrip",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_dynamic_roundtrip",
            "customer_session_client_secret": "cuss_dynamic_roundtrip",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "custom_payment_methods": [method_id],
            "total_summary": {"due": 125000},
            "currency": "PHP",
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({
                "billing_details": {"country": "PH", "currency": "PHP"},
                "custom_payment_methods": [method_id],
            }),
            FakeResponse({
                "merchant_country": "PH",
                "merchant_currency": "php",
                "custom_payment_method_data": [{
                    "type": method_id,
                    "display_name": "Localized wallet label",
                }],
            }),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["method_available"])
        self.assertTrue(result["trusted_custom_method_matched"])
        self.assertNotIn(method_id, repr(result))
        self.assertFalse(any(
            "confirm" in url or "custom_payment_method/start" in url
            for _, url, _ in session.calls
        ))

    def test_non_ph_roundtrip_does_not_mark_gcash_available(self):
        method_id = "cpmt_non_ph_roundtrip"
        checkout = {
            "checkout_session_id": "cs_test_non_ph_roundtrip",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_non_ph_roundtrip",
            "customer_session_client_secret": "cuss_non_ph_roundtrip",
            "billing_details": {"country": "US", "currency": "USD"},
            "custom_payment_methods": [method_id],
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({
                "billing_details": {"country": "US", "currency": "USD"},
                "custom_payment_methods": [method_id],
            }),
            FakeResponse({
                "merchant_country": "US",
                "merchant_currency": "usd",
                "custom_payment_method_data": [{
                    "type": method_id,
                    "display_name": "Localized wallet label",
                }],
            }),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["method_available"])
        self.assertFalse(result["trusted_custom_method_matched"])

    def test_elements_must_echo_the_checkout_id_for_a_nonliteral_label(self):
        checkout_method_id = "cpmt_checkout_candidate"
        elements_method_id = "cpmt_different_elements_method"
        checkout = {
            "checkout_session_id": "cs_test_mismatched_roundtrip",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_mismatched_roundtrip",
            "customer_session_client_secret": "cuss_mismatched_roundtrip",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "custom_payment_methods": [checkout_method_id],
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({
                "billing_details": {"country": "PH", "currency": "PHP"},
                "custom_payment_methods": [checkout_method_id],
            }),
            FakeResponse({
                "merchant_country": "PH",
                "merchant_currency": "php",
                "custom_payment_method_data": [{
                    "type": elements_method_id,
                    "display_name": "Localized wallet label",
                }],
            }),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["method_available"])
        self.assertFalse(result["trusted_custom_method_matched"])
        self.assertNotIn(checkout_method_id, repr(result))
        self.assertNotIn(elements_method_id, repr(result))

    def test_elements_failure_exposes_only_a_stable_stage_code(self):
        method_id = "cpmt_elements_failure"
        checkout = {
            "checkout_session_id": "cs_test_elements_failure",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_elements_failure",
            "customer_session_client_secret": "cuss_elements_failure",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "custom_payment_methods": [method_id],
        }

        class ElementsFailureSession(FakeSession):
            def get(self, url, **kwargs):
                if url.endswith("/v1/elements/sessions"):
                    raise RuntimeError("simulated transport failure")
                return super().get(url, **kwargs)

        session = ElementsFailureSession([
            FakeResponse(checkout),
            FakeResponse({"custom_payment_methods": [method_id]}),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["custom_method_probe_status"], "failed")
        self.assertEqual(
            result["custom_method_probe_failure"],
            "stripe_custom_capability_transport_error",
        )
        self.assertEqual(result["custom_method_probe_exception"], "RuntimeError")
        self.assertNotIn("simulated transport failure", repr(result))

    def test_elements_capability_uses_a_separate_stripe_session(self):
        method_id = "cpmt_separate_stripe_session"
        checkout = {
            "checkout_session_id": "cs_test_separate_stripe_session",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_separate_stripe_session",
            "customer_session_client_secret": "cuss_separate_stripe_session",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "custom_payment_methods": [method_id],
        }
        chatgpt_session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"custom_payment_methods": [method_id]}),
        ])
        stripe_session = FakeSession([
            FakeResponse({
                "custom_payment_method_data": [{
                    "type": method_id,
                    "display_name": "Localized wallet label",
                }],
            }),
        ])
        factories = []

        def factory(**kwargs):
            factories.append(kwargs)
            return chatgpt_session if len(factories) == 1 else stripe_session

        result = probe_gcash("access-token", session_factory=factory)

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(
            [item["impersonate"] for item in factories],
            ["chrome110", "firefox144"],
        )
        self.assertTrue(stripe_session.closed)
        self.assertTrue(chatgpt_session.closed)

    def test_elements_request_user_agent_matches_the_firefox_fingerprint(self):
        method_id = "cpmt_firefox_user_agent"
        checkout = {
            "checkout_session_id": "cs_test_firefox_user_agent",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_firefox_user_agent",
            "customer_session_client_secret": "cuss_firefox_user_agent",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "custom_payment_methods": [method_id],
        }
        chatgpt_session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"custom_payment_methods": [method_id]}),
        ])
        stripe_session = FakeSession([
            FakeResponse({
                "custom_payment_method_data": [{
                    "type": method_id,
                    "display_name": "Localized wallet label",
                }],
            }),
        ])
        sessions = [chatgpt_session, stripe_session]

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: sessions.pop(0),
        )

        self.assertEqual(result["classification"], "eligible")
        stripe_headers = stripe_session.calls[0][2]["headers"]
        self.assertIn("Firefox/144.0", stripe_headers["User-Agent"])
        self.assertNotIn("Chrome/145.0.0.0", stripe_headers["User-Agent"])

    def test_elements_transport_retries_with_an_alternate_fingerprint(self):
        method_id = "cpmt_elements_retry"
        checkout = {
            "checkout_session_id": "cs_test_elements_retry",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_elements_retry",
            "customer_session_client_secret": "cuss_elements_retry",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "custom_payment_methods": [method_id],
        }

        class FlakyElementsSession(FakeSession):
            def get(self, url, **kwargs):
                if url.endswith("/v1/elements/sessions"):
                    raise ConnectionError("simulated transient transport failure")
                return super().get(url, **kwargs)

        chatgpt_session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"custom_payment_methods": [method_id]}),
        ])
        flaky_stripe = FlakyElementsSession([])
        recovered_stripe = FakeSession([
            FakeResponse({
                "custom_payment_method_data": [{
                    "type": method_id,
                    "display_name": "Localized wallet label",
                }],
            }),
        ])
        factories = []

        def factory(**kwargs):
            factories.append(kwargs)
            return [chatgpt_session, flaky_stripe, recovered_stripe][len(factories) - 1]

        result = probe_gcash("access-token", session_factory=factory)

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(
            [item["impersonate"] for item in factories],
            ["chrome110", "firefox144", "chrome110"],
        )
        self.assertEqual(result["custom_method_probe_status"], "accepted")

    def test_probe_sends_source_promotion_and_tax_requests(self):
        checkout = {
            "checkout_session_id": "cs_test_method_only",
            "processor_entity": "openai_ie",
            **_gcash_payload(),
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse(_gcash_payload()),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(session.calls[0][1], "https://chatgpt.com/backend-api/payments/checkout")
        self.assertTrue(session.calls[0][2]["json"]["check_card_proxy"])
        self.assertEqual(
            session.calls[0][2]["json"]["promo_campaign"]["promo_campaign_id"],
            "plus-1-month-free",
        )
        urls = [url for _, url, _ in session.calls]
        self.assertTrue(any(url.endswith("/payments/checkout/update") for url in urls))
        self.assertTrue(any(url.endswith("/payments/checkout/taxes") for url in urls))

    def test_checkout_uses_the_source_ph_payment_contract(self):
        """GCash capability is evaluated against the Philippines checkout."""
        checkout = {
            "checkout_session_id": "cs_test_inferred_country",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_inferred_country",
            "billing_details": {"country": "PH", "currency": "PHP"},
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"payment_method_types": ["card"]}),
            FakeResponse({"payment_method_types": ["card"]}),
        ])

        result = probe_gcash(
            "access-token",
            proxy="http://proxy.example:8080",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        checkout_payload = session.calls[0][2]["json"]
        self.assertEqual(
            checkout_payload["billing_details"],
            {"country": "PH", "currency": "PHP"},
        )
        self.assertIn("promo_campaign", checkout_payload)
        self.assertEqual(result["checkout_country"], "PH")
        self.assertEqual(result["currency"], "PHP")
        self.assertEqual(
            session.calls[-1][2]["data"]["browser_timezone"],
            "Asia/Manila",
        )

    def test_incomplete_capability_response_is_unavailable_not_unknown(self):
        checkout = {
            "checkout_session_id": "cs_test_incomplete_capability",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_incomplete_capability",
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({}, status_code=503),
            FakeResponse({}, status_code=503),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertTrue(result["conclusive"])
        self.assertEqual(result["decision"], "gcash_evidence_incomplete")

    def test_resolve_auth_failure_is_not_hidden_by_later_stripe_failure(self):
        checkout = {
            "checkout_session_id": "cs_test_resolve_auth_failure",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_resolve_auth_failure",
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({}, status_code=401),
            FakeResponse({}, status_code=503),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(result["status"], "token_invalid")
        self.assertEqual(result["decision"], "resolve_http_401")

    def test_billing_country_mismatch_is_unavailable_not_unknown(self):
        session = FakeSession([
            FakeResponse(
                {"detail": "Billing country must match request country."},
                status_code=400,
            )
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertTrue(result["conclusive"])
        self.assertEqual(result["decision"], "checkout_billing_country_mismatch")

    def test_explicit_checkout_evidence_survives_resolve_failure(self):
        checkout = {
            "checkout_session_id": "cs_test_resolve_failure",
            "processor_entity": "openai_ie",
            **_gcash_payload(),
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"message": "temporary"}, status_code=503),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(len(session.calls), 4)

    def test_probe_stops_before_any_payment_execution_endpoint(self):
        checkout = {
            "checkout_session_id": "cs_test_safe_probe",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_safe",
            "customer_session_client_secret": "cuss_secret_not_returned",
            **_gcash_payload(),
        }
        resolved = _gcash_payload()
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse(resolved),
        ])
        factory_calls = []

        def factory(*, proxy=None, impersonate=""):
            factory_calls.append({"proxy": proxy, "impersonate": impersonate})
            return session

        result = probe_gcash(
            "access-token-secret",
            account_id="account-a",
            device_id="device-a",
            cookie_header="session=secret-cookie",
            proxy="socks5://proxy-user:proxy-pass@example.test:1080",
            session_factory=factory,
            checked_at=99.0,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(factory_calls[0]["proxy"], "socks5://proxy-user:proxy-pass@example.test:1080")
        self.assertTrue(session.closed)
        urls = [url for _, url, _ in session.calls]
        # A transport failure on the isolated Stripe capability session may
        # trigger one bounded retry; neither attempt may enter a payment stage.
        self.assertGreaterEqual(len(urls), 5)
        self.assertLessEqual(len(urls), 6)
        self.assertTrue(any(url.endswith("/payments/checkout") for url in urls))
        self.assertTrue(any("/payments/checkout/openai_ie/cs_test_safe_probe" in url for url in urls))
        self.assertFalse(any("confirm" in url or "custom_payment_method/start" in url for url in urls))
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))
        serialized = repr(result)
        self.assertNotIn("access-token-secret", serialized)
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("cuss_secret_not_returned", serialized)
        self.assertNotIn("cs_test_safe_probe", serialized)
        self.assertNotIn("proxy-pass", serialized)

    def test_payment_pages_init_is_used_when_no_custom_id_is_returned(self):
        checkout = {
            "checkout_session_id": "cs_test_standard_methods",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_standard",
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"payment_method_types": ["card"]}),
            FakeResponse({"payment_method_types": ["card", "gcash"]}),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(session.calls[-1][0], "POST")
        self.assertEqual(
            session.calls[-1][1],
            "https://api.stripe.com/v1/payment_pages/cs_test_standard_methods/init",
        )

    def test_transport_error_is_unavailable_and_redacted(self):
        class BrokenSession(FakeSession):
            def post(self, url, **kwargs):
                raise RuntimeError(
                    "connect failed via http://user:password@proxy.example and Bearer token-secret"
                )

        session = BrokenSession([])
        result = probe_gcash(
            "token-secret",
            proxy="http://user:password@proxy.example",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(result["decision"], "checkout_transport_error")
        self.assertTrue(result["retryable"])
        self.assertNotIn("password", repr(result))
        self.assertNotIn("token-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
