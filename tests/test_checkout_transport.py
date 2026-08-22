"""Synthetic tests for the dynamic HAR checkout transport metadata.

No value in this file is copied from the supplied HAR.  The fixtures only
exercise shape validation and header isolation.
"""

from __future__ import annotations

import base64
import json
import time
import unittest

from chatgpt_checkout_transport import (
    CheckoutTransportMetadata,
    build_checkout_header_overrides,
    build_oai_telemetry,
    parse_bootstrap_metadata,
)
from gcash_probe import probe_checkout_eligibility


METHOD_ID = "cpmt_synthetic_gcash"


def _attestation(*, expires_at: int | None = None) -> str:
    payload = {
        "version": 1,
        "track": "stable",
        "deployId": "prod-synthetic-build",
        "subject": "synthetic-subject",
        "issuedAt": int(time.time()) - 10,
        "expiresAt": int(time.time() + 600) if expires_at is None else expires_at,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encoded}.synthetic-signature"


class CheckoutTransportMetadataTests(unittest.TestCase):
    def test_bootstrap_metadata_extracts_current_build_and_dynamic_attestation(self):
        html = (
            '<html data-build="prod-synthetic-build" data-seq="12345">'
            '<script src="https://chatgpt.com/sentinel/synthetic-v1/sdk.js"></script>'
            f'<script>window.webDeploymentAttestation={json.dumps(_attestation())}</script>'
            '<script>window.sessionId="11111111-2222-4333-8444-555555555555"</script>'
        )

        metadata = parse_bootstrap_metadata(
            html,
            response_headers={"x-oai-is-client-observation": "v1.r.p.synthetic"},
        )

        self.assertEqual(metadata["client_version"], "prod-synthetic-build")
        self.assertEqual(metadata["client_build"], "12345")
        self.assertEqual(metadata["sentinel_script_url"], "https://chatgpt.com/sentinel/synthetic-v1/sdk.js")
        self.assertEqual(metadata["session_id"], "11111111-2222-4333-8444-555555555555")
        self.assertEqual(metadata["client_observation"], "v1.r.p.synthetic")
        self.assertEqual(metadata["attestation"], _attestation())

    def test_bootstrap_metadata_rejects_stale_or_cross_origin_attestation(self):
        html = (
            '<html data-build="prod-synthetic-build" data-seq="12345">'
            '<script src="https://evil.example/sentinel/v1/sdk.js"></script>'
            f'<script>window.webDeploymentAttestation={json.dumps(_attestation(expires_at=int(time.time()) - 1))}</script>'
        )

        metadata = parse_bootstrap_metadata(html)

        self.assertEqual(metadata["sentinel_script_url"], "")
        self.assertEqual(metadata["attestation"], "")

    def test_dynamic_telemetry_is_bounded_json_and_not_a_copied_har_value(self):
        telemetry = build_oai_telemetry(started_monotonic=100.0, now_monotonic=100.25)

        parsed = json.loads(telemetry)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0], 1)
        self.assertEqual(parsed[1], 250)
        self.assertNotEqual(telemetry, "[1,586.4000000003725,12,94,23,2,0,594]")

    def test_checkout_overrides_are_allowlisted_and_strict_mode_isolated(self):
        metadata = CheckoutTransportMetadata(
            sentinel_token="sentinel-token-value",
            telemetry="[1,250]",
            attestation=_attestation(),
            client_observation="v1.r.p.synthetic",
            strict_har=True,
        )

        overrides = build_checkout_header_overrides(metadata)

        self.assertEqual(overrides["openai-sentinel-token"], "sentinel-token-value")
        self.assertEqual(overrides["oai-telemetry"], "[1,250]")
        self.assertEqual(overrides["oai-web-deployment-attestation"], metadata.attestation)
        self.assertEqual(overrides["x-oai-is-client-observation"], "v1.r.p.synthetic")
        self.assertNotIn("Authorization", overrides)
        self.assertNotIn("Cookie", overrides)


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
        return None


def _accounts():
    return {
        "accounts": {
            "default": {
                "account": {"plan_type": "free", "is_deactivated": False},
                "entitlement": {"has_active_subscription": False},
                "eligible_promo_campaigns": {},
            }
        }
    }


def _checkout():
    return {
        "publishable_key": "pk_synthetic",
        "customer_session_client_secret": "cuss_synthetic",
        "billing_details": {"country": "PH", "currency": "PHP"},
        "payment_method_types": ["card", "link"],
        "custom_payment_methods": [{"id": METHOD_ID}],
        "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
    }


def _stripe():
    return {"custom_payment_method_data": [{"type": METHOD_ID, "display_name": "GCash"}]}


class CheckoutHeaderIntegrationTests(unittest.TestCase):
    def test_injected_dynamic_metadata_is_attached_only_to_checkout(self):
        chatgpt = _Session([_Response(_accounts()), _Response(_checkout())])
        stripe = _Session([_Response(_stripe())])
        sessions = [chatgpt, stripe]
        metadata = CheckoutTransportMetadata(
            sentinel_token="sentinel-token-value",
            telemetry="[1,250]",
            attestation=_attestation(),
            client_observation="v1.r.p.synthetic",
            strict_har=True,
        )

        result = probe_checkout_eligibility(
            "access-token",
            device_id="11111111-2222-4333-8444-555555555555",
            session_factory=lambda **_: sessions.pop(0),
            checkout_transport_factory=lambda **_: metadata,
        )

        self.assertEqual(result["gcash"]["classification"], "eligible")
        checkout_headers = chatgpt.calls[1][2]["headers"]
        self.assertEqual(checkout_headers["openai-sentinel-token"], "sentinel-token-value")
        self.assertEqual(checkout_headers["oai-telemetry"], "[1,250]")
        self.assertIn("oai-web-deployment-attestation", checkout_headers)
        self.assertEqual(checkout_headers["x-oai-is-client-observation"], "v1.r.p.synthetic")
        self.assertNotIn("Authorization", checkout_headers)
        self.assertNotIn("Cookie", checkout_headers)
        self.assertNotIn("openai-sentinel-token", repr(result))
        self.assertNotIn("synthetic-signature", repr(result))
        self.assertFalse(any(
            key.lower().startswith("oai-") or key.lower().startswith("openai-")
            for key in stripe.calls[0][2]["headers"]
        ))


if __name__ == "__main__":
    unittest.main()
