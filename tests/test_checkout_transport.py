"""Synthetic tests for the dynamic HAR checkout transport metadata.

No value in this file is copied from the supplied HAR.  The fixtures only
exercise shape validation and header isolation.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from chatgpt_checkout_transport import (
    CheckoutTransportMetadata,
    build_checkout_header_overrides,
    build_oai_telemetry,
    parse_bootstrap_metadata,
    prepare_checkout_transport,
)
from gcash_probe import probe_checkout_eligibility
from sentinel_quickjs import _ensure_sdk_file, _fetch_sentinel_challenge


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
            f'<script>window.webDeploymentAttestation={json.dumps(_attestation(expires_at=int(time.time()) - 120))}</script>'
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
    def __init__(self, payload, status_code=200, *, text="", headers=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.isolated_calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def post_isolated(self, url, **kwargs):
        self.isolated_calls.append(("POST", url, kwargs))
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
    def test_sentinel_sdk_download_disables_redirects_before_execution(self):
        response = _Response({})
        response.content = b"synthetic-sdk"
        session = _Session([response])
        with tempfile.TemporaryDirectory() as cache_root, patch(
            "sentinel_quickjs.tempfile.gettempdir",
            return_value=cache_root,
        ), patch("sentinel_quickjs._sdk_file_cache", {}):
            sdk_file, sdk_url = _ensure_sdk_file(
                session,
                30_000,
                sdk_url="https://chatgpt.com/sentinel/synthetic-v1/sdk.js",
            )
            sdk_contents = sdk_file.read_bytes()

        self.assertEqual(sdk_url, "https://chatgpt.com/sentinel/synthetic-v1/sdk.js")
        self.assertEqual(sdk_contents, b"synthetic-sdk")
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_legacy_sentinel_challenge_keeps_the_session_post_path(self):
        session = _Session([_Response({"token": "legacy-challenge"})])

        result = _fetch_sentinel_challenge(
            session,
            device_id="11111111-2222-4333-8444-555555555555",
            flow="authorize_continue",
            request_p="synthetic-request-proof",
            timeout_ms=30_000,
        )

        self.assertEqual(result["token"], "legacy-challenge")
        self.assertEqual(len(session.isolated_calls), 0)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertIn("sentinel.openai.com/backend-api/sentinel/req", session.calls[0][1])
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_checkout_sentinel_challenge_uses_isolated_post_and_client_hints(self):
        session = _Session([_Response({"token": "checkout-challenge"})])

        result = _fetch_sentinel_challenge(
            session,
            device_id="11111111-2222-4333-8444-555555555555",
            flow="chatgpt_checkout",
            request_p="synthetic-request-proof",
            timeout_ms=30_000,
            request_url="https://chatgpt.com/backend-api/sentinel/req",
            sdk_url="https://chatgpt.com/sentinel/synthetic-v1/sdk.js",
            user_agent="Synthetic Chrome/146",
            sec_ch_ua='"Chromium";v="146"',
            sec_ch_ua_mobile="?0",
            sec_ch_ua_platform='"Windows"',
            sec_ch_ua_full_version_list='"Chromium";v="146.0.0.0"',
            sec_ch_ua_arch='"x86"',
            sec_ch_ua_bitness='"64"',
            sec_ch_ua_model='""',
            sec_ch_ua_platform_version='"10.0.0"',
        )

        self.assertEqual(result["token"], "checkout-challenge")
        self.assertEqual(len(session.isolated_calls), 1)
        headers = session.isolated_calls[0][2]["headers"]
        self.assertFalse(session.isolated_calls[0][2]["allow_redirects"])
        self.assertEqual(headers["sec-ch-ua"], '"Chromium";v="146"')
        self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(headers["sec-ch-ua-full-version-list"], '"Chromium";v="146.0.0.0"')
        self.assertEqual(headers["sec-ch-ua-arch"], '"x86"')
        self.assertEqual(headers["sec-ch-ua-bitness"], '"64"')
        self.assertEqual(headers["sec-ch-ua-model"], '""')
        self.assertEqual(headers["sec-ch-ua-platform-version"], '"10.0.0"')

    def test_prepare_transport_generates_a_fresh_checkout_sentinel_token(self):
        html = (
            '<html data-build="prod-synthetic-build" data-seq="12345">'
            f'<script>window.webDeploymentAttestation={json.dumps(_attestation())}</script>'
            '</html>'
        )
        loader = (
            "(function(){var script=document.createElement('script');"
            "script.src='https://chatgpt.com/sentinel/synthetic-v1/sdk.js';})();"
        )
        session = _Session([
            _Response({}, text=html, headers={"content-type": "text/html"}),
            _Response({}, text=loader, headers={"content-type": "text/javascript"}),
            _Response({"status": "ok"}),
        ])

        with patch(
            "sentinel.get_sentinel_token",
            return_value=("fresh-sentinel-token", ""),
        ) as sentinel:
            metadata = prepare_checkout_transport(
                session=session,
                access_token="access-token",
                account_id="account-stable",
                device_id="11111111-2222-4333-8444-555555555555",
                cookie_header="safe=value",
                session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                timeout=30,
                user_agent="Synthetic Chrome/146",
                sec_ch_ua='"Chromium";v="146"',
                sec_ch_ua_full_version_list='"Chromium";v="146.0.0.0"',
            )

        self.assertTrue(metadata.strict_har)
        self.assertEqual(metadata.sentinel_token, "fresh-sentinel-token")
        self.assertEqual(metadata.client_version, "prod-synthetic-build")
        self.assertEqual(metadata.client_build, "12345")
        self.assertEqual(metadata.sentinel_script_url, "https://chatgpt.com/sentinel/synthetic-v1/sdk.js")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "POST"])
        bootstrap_headers = session.calls[0][2]["headers"]
        self.assertEqual(bootstrap_headers["Authorization"], "Bearer access-token")
        self.assertEqual(bootstrap_headers["ChatGPT-Account-ID"], "account-stable")
        self.assertEqual(bootstrap_headers["Cookie"], "safe=value")
        self.assertEqual(
            session.calls[-1][1],
            "https://chatgpt.com/backend-api/sentinel/ping",
        )
        self.assertNotIn("Authorization", session.calls[-1][2]["headers"])
        self.assertNotIn("Cookie", session.calls[-1][2]["headers"])
        sentinel.assert_called_once()
        self.assertEqual(sentinel.call_args.kwargs["flow"], "chatgpt_checkout")
        self.assertEqual(
            sentinel.call_args.kwargs["sentinel_sdk_url"],
            "https://chatgpt.com/sentinel/synthetic-v1/sdk.js",
        )
        self.assertEqual(
            sentinel.call_args.kwargs["sentinel_req_url"],
            "https://chatgpt.com/backend-api/sentinel/req",
        )

    def test_missing_attestation_keeps_strict_sentinel_checkout_fallback(self):
        html = (
            '<html data-build="prod-synthetic-build" data-seq="12345">'
            '<script src="https://chatgpt.com/sentinel/synthetic-v1/sdk.js"></script>'
            '</html>'
        )
        session = _Session([
            _Response({}, text=html, headers={"content-type": "text/html"}),
            _Response({"status": "ok"}),
        ])

        with patch(
            "sentinel.get_sentinel_token",
            return_value=("fresh-sentinel-token", ""),
        ):
            metadata = prepare_checkout_transport(
                session=session,
                access_token="access-token",
                account_id="account-stable",
                device_id="11111111-2222-4333-8444-555555555555",
                cookie_header="safe=value",
                session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                timeout=30,
                user_agent="Synthetic Chrome/146",
                sec_ch_ua='"Chromium";v="146"',
                sec_ch_ua_full_version_list='"Chromium";v="146.0.0.0"',
            )

        self.assertEqual(metadata.attestation, "")
        self.assertTrue(metadata.strict_har)

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

    def test_strict_checkout_rejection_gets_one_authenticated_same_identity_fallback(self):
        chatgpt = _Session([
            _Response(_accounts()),
            _Response({"detail": "synthetic rejection"}, status_code=503),
            _Response(_checkout()),
        ])
        stripe = _Session([_Response(_stripe())])
        sessions = [chatgpt, stripe]
        metadata = CheckoutTransportMetadata(
            sentinel_token="sentinel-token-value",
            telemetry="[1,250]",
            attestation=_attestation(),
            strict_har=True,
        )

        result = probe_checkout_eligibility(
            "access-token",
            account_id="account-stable",
            device_id="11111111-2222-4333-8444-555555555555",
            cookie_header="safe=value",
            session_factory=lambda **_: sessions.pop(0),
            checkout_transport_factory=lambda **_: metadata,
        )

        self.assertEqual(result["gcash"]["classification"], "eligible")
        checkout_calls = [call for call in chatgpt.calls if call[1].endswith("/payments/checkout")]
        self.assertEqual(len(checkout_calls), 2)
        first_headers = checkout_calls[0][2]["headers"]
        fallback_headers = checkout_calls[1][2]["headers"]
        self.assertNotIn("Authorization", first_headers)
        self.assertNotIn("Cookie", first_headers)
        self.assertEqual(fallback_headers["Authorization"], "Bearer access-token")
        self.assertEqual(fallback_headers["Cookie"], "safe=value; oai-did=11111111-2222-4333-8444-555555555555")
        self.assertEqual(
            first_headers["openai-sentinel-token"],
            fallback_headers["openai-sentinel-token"],
        )
        self.assertEqual(checkout_calls[0][2]["json"], checkout_calls[1][2]["json"])

    def test_strict_checkout_http_400_gets_one_authenticated_same_identity_fallback(self):
        chatgpt = _Session([
            _Response(_accounts()),
            _Response({"detail": "synthetic contract rejection"}, status_code=400),
            _Response(_checkout()),
        ])
        stripe = _Session([_Response(_stripe())])
        sessions = [chatgpt, stripe]
        metadata = CheckoutTransportMetadata(
            sentinel_token="sentinel-token-value",
            telemetry="[1,250]",
            attestation=_attestation(),
            strict_har=True,
        )

        result = probe_checkout_eligibility(
            "access-token",
            account_id="account-stable",
            device_id="11111111-2222-4333-8444-555555555555",
            cookie_header="safe=value",
            session_factory=lambda **_: sessions.pop(0),
            checkout_transport_factory=lambda **_: metadata,
        )

        self.assertEqual(result["gcash"]["classification"], "eligible")
        checkout_calls = [call for call in chatgpt.calls if call[1].endswith("/payments/checkout")]
        self.assertEqual(len(checkout_calls), 2)
        self.assertNotIn("Authorization", checkout_calls[0][2]["headers"])
        self.assertEqual(checkout_calls[1][2]["headers"]["Authorization"], "Bearer access-token")


if __name__ == "__main__":
    unittest.main()
