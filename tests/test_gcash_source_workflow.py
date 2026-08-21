import os
import unittest
from unittest.mock import patch

from gcash_probe import probe_gcash


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _checkout():
    return {
        "checkout_session_id": "cs_source_workflow",
        "processor_entity": "openai_ie",
        "publishable_key": "pk_source_workflow",
        "customer_session_client_secret": "cuss_source_workflow",
        "billing_details": {"country": "PH", "currency": "PHP"},
    }


def _resolved():
    return {
        "payment_method_types": ["card", "custom_payment_method"],
        "custom_payment_method_data": [
            {"id": "cpmt_dynamic_source_workflow", "display_name": "GCash"}
        ],
        "total_summary": {"due": 125000},
        "currency": "PHP",
    }


class GCashSourceWorkflowTests(unittest.TestCase):
    def test_probe_applies_promotion_and_taxes_before_capability_read(self):
        session = _Session([
            _Response(_checkout()),
            _Response({"discountAmounts": {"total": 0}}),
            _Response({"ok": True}),
            _Response(_resolved()),
            _Response({"custom_payment_method_data": [{
                "id": "cpmt_dynamic_source_workflow",
                "display_name": "GCash",
            }]}),
        ])

        result = probe_gcash(
            "access-token",
            checkout_email="person@example.com",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(
            [(method, url) for method, url, _ in session.calls],
            [
                ("POST", "https://chatgpt.com/backend-api/payments/checkout"),
                ("POST", "https://chatgpt.com/backend-api/payments/checkout/update"),
                ("POST", "https://chatgpt.com/backend-api/payments/checkout/taxes"),
                ("GET", "https://chatgpt.com/backend-api/payments/checkout/openai_ie/cs_source_workflow"),
                ("GET", "https://api.stripe.com/v1/elements/sessions"),
            ],
        )

        checkout_payload = session.calls[0][2]["json"]
        self.assertTrue(checkout_payload["check_card_proxy"])
        self.assertEqual(
            checkout_payload["billing_details"],
            {"country": "PH", "currency": "PHP"},
        )

        update_payload = session.calls[1][2]["json"]
        self.assertEqual(update_payload["checkout_session_id"], "cs_source_workflow")
        self.assertEqual(update_payload["processor_entity"], "openai_ie")
        self.assertEqual(update_payload["plan_name"], "chatgptplusplan")
        self.assertEqual(update_payload["price_interval"], "month")
        self.assertEqual(update_payload["seat_quantity"], 1)
        self.assertEqual(
            update_payload["promo_campaign"],
            {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        )

        taxes_payload = session.calls[2][2]["json"]
        self.assertEqual(taxes_payload["checkout_session_id"], "cs_source_workflow")
        self.assertEqual(taxes_payload["processor_entity"], "openai_ie")
        self.assertEqual(taxes_payload["billing_country"], "PH")
        self.assertEqual(taxes_payload["currency"], "PHP")
        self.assertEqual(taxes_payload["checkout_email"], "person@example.com")

    def test_configured_custom_method_id_is_probed_when_checkout_is_opaque(self):
        checkout = {
            **_checkout(),
            "payment_method_types": ["card", "custom_payment_method"],
        }
        session = _Session([
            _Response(checkout),
            _Response({"discountAmounts": {"total": 0}}),
            _Response({"ok": True}),
            _Response({"payment_method_types": ["card", "custom_payment_method"]}),
            _Response({
                "custom_payment_method_data": [
                    {"id": "cpmt_configured_source_workflow", "display_name": "GCash"}
                ]
            }),
        ])

        with patch.dict(
            os.environ,
            {"GCASH_CUSTOM_PAYMENT_METHOD_ID": "cpmt_configured_source_workflow"},
            clear=False,
        ):
            result = probe_gcash(
                "access-token",
                session_factory=lambda **_: session,
            )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(session.calls[-1][1], "https://api.stripe.com/v1/elements/sessions")
        self.assertEqual(
            session.calls[-1][2]["params"]["custom_payment_methods[0]"],
            "cpmt_configured_source_workflow",
        )

    def test_optional_update_failures_do_not_erase_resolved_method_evidence(self):
        session = _Session([
            _Response(_checkout()),
            _Response({"message": "temporary"}, status_code=503),
            _Response({"message": "temporary"}, status_code=503),
            _Response(_resolved()),
            _Response({"custom_payment_method_data": [{
                "id": "cpmt_dynamic_source_workflow",
                "display_name": "GCash",
            }]}),
        ])

        result = probe_gcash(
            "access-token",
            checkout_email="person@example.com",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["decision"], "gcash_available")
        self.assertFalse(result["retryable"])
        self.assertFalse(any(
            "confirm" in url or "custom_payment_method/start" in url
            for _, url, _ in session.calls
        ))


if __name__ == "__main__":
    unittest.main()
