import base64
import json
import unittest
import uuid


from plus_probe import (
    plus_operator_note,
    probe_plus_eligibility,
    safe_plus_result_label,
    should_persist_plus_result,
)


def _jwt(account_id="account-stable"):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return (
        f"{encode({'alg': 'RS256', 'typ': 'JWT'})}."
        f"{encode({'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
        "signature"
    )


def _eligible_payload(account_id="account-stable"):
    return {
        "accounts": {
            account_id: {
                "account": {"plan_type": "free", "is_deactivated": False},
                "entitlement": {
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
                "eligible_promo_campaigns": {
                    "plus": {
                        "id": "plus-1-month-free",
                        "metadata": {
                            "title": "One month free",
                            "discount": {"percentage": 100},
                            "duration": {"num_periods": 1, "period": "month"},
                        },
                    },
                },
            },
        },
    }


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class _BrokenJsonResponse(_Response):
    def json(self):
        raise ValueError("private invalid response")


class PlusProbeTests(unittest.TestCase):
    def test_eligible_probe_reuses_selected_proxy_and_blocks_redirects(self):
        session = _Session(_Response(_eligible_payload()))
        factories = []

        def factory(**kwargs):
            factories.append(kwargs)
            return session

        result = probe_plus_eligibility(
            _jwt(),
            email="person@example.com",
            device_id="device-stable",
            proxy="http://ph-proxy.example:8080",
            session_factory=factory,
            checked_at=10.0,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["decision"], "plus_1_month_free_available")
        self.assertEqual(result["checked_at"], 10.0)
        self.assertEqual(
            factories,
            [{"proxy": "http://ph-proxy.example:8080", "impersonate": "chrome110"}],
        )
        self.assertEqual(len(session.calls), 1)
        _, request = session.calls[0]
        self.assertIs(request["allow_redirects"], False)
        self.assertEqual(request["headers"]["ChatGPT-Account-ID"], "account-stable")
        self.assertEqual(request["headers"]["OAI-Device-Id"], "device-stable")
        self.assertNotIn(_jwt(), repr(result))
        self.assertTrue(session.closed)

    def test_proxy_failure_never_retries_direct_or_exposes_error_text(self):
        session = _Session(error=RuntimeError(
            "proxy http://user:secret@proxy.example failed with token-secret (97)"
        ))
        factories = []

        def factory(**kwargs):
            factories.append(kwargs)
            return session

        result = probe_plus_eligibility(
            _jwt(),
            email="person@example.com",
            proxy="socks5://user:secret@proxy.example:1080",
            session_factory=factory,
        )

        self.assertEqual(result["decision"], "proxy_auth_rejected")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["retryable"])
        self.assertEqual(len(factories), 1)
        self.assertEqual(factories[0]["proxy"], "socks5://user:secret@proxy.example:1080")
        self.assertNotIn("secret", repr(result))
        self.assertNotIn("proxy.example", repr(result))
        self.assertTrue(session.closed)

    def test_invalid_access_token_header_is_rejected_without_network(self):
        factories = []

        result = probe_plus_eligibility(
            "token\r\nInjected: value",
            email="person@example.com",
            session_factory=lambda **kwargs: factories.append(kwargs),
        )

        self.assertEqual(result["decision"], "invalid_access_token")
        self.assertEqual(result["status"], "token_invalid")
        self.assertEqual(factories, [])

    def test_deactivated_forbidden_response_is_conclusive_and_safe(self):
        session = _Session(_Response(
            {"error": "private upstream value"},
            status_code=403,
            text="This account has been deactivated: private upstream value",
        ))

        result = probe_plus_eligibility(
            _jwt(),
            email="person@example.com",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "account_deactivated")
        self.assertEqual(result["status"], "banned")
        self.assertTrue(result["conclusive"])
        self.assertNotIn("private upstream value", repr(result))

    def test_only_meaningful_results_are_persisted(self):
        self.assertTrue(should_persist_plus_result({"status": "plus_eligible"}))
        self.assertTrue(should_persist_plus_result({"status": "token_invalid"}))
        self.assertFalse(should_persist_plus_result({"status": "error"}))
        self.assertFalse(should_persist_plus_result({"status": "no_at"}))
        self.assertFalse(should_persist_plus_result({"status": "not_found"}))

    def test_missing_token_and_oversized_proxy_fail_before_network(self):
        factories = []

        missing = probe_plus_eligibility(
            "",
            email="person@example.com",
            session_factory=lambda **kwargs: factories.append(kwargs),
        )
        oversized = probe_plus_eligibility(
            "access-token",
            email="person@example.com",
            proxy="p" * 2049,
            session_factory=lambda **kwargs: factories.append(kwargs),
        )

        self.assertEqual(missing["decision"], "missing_access_token")
        self.assertEqual(oversized["decision"], "proxy_url_too_long")
        self.assertEqual(factories, [])

    def test_http_and_json_failures_have_stable_safe_codes(self):
        cases = (
            (_Response({}, status_code=401, text="expired"), "token_invalid", False),
            (_Response({}, status_code=403, text="forbidden"), "http_403", False),
            (_Response({}, status_code=503), "http_503", True),
            (_BrokenJsonResponse({}, status_code=200), "invalid_json", True),
        )

        for response, decision, retryable in cases:
            with self.subTest(decision=decision):
                session = _Session(response)
                result = probe_plus_eligibility(
                    "access-token",
                    email="person@example.com",
                    session_factory=lambda **_: session,
                )
                self.assertEqual(result["decision"], decision)
                self.assertIs(result["retryable"], retryable)
                self.assertNotIn("private", repr(result))
                self.assertTrue(session.closed)

    def test_network_decisions_and_operator_notes_are_allowlisted(self):
        cases = (
            (
                "http://proxy.example:8080",
                RuntimeError("connect failed (7) with private detail"),
                "proxy_unreachable",
                "The proxy is unreachable (curl error 7)",
            ),
            (
                "http://proxy.example:8080",
                RuntimeError("generic private proxy failure"),
                "proxy_network_error",
                "The proxy request failed; direct fallback was not used",
            ),
            (
                "",
                RuntimeError("generic private direct failure"),
                "network_error",
                "",
            ),
        )

        for proxy, error, decision, note in cases:
            with self.subTest(decision=decision):
                session = _Session(error=error)
                result = probe_plus_eligibility(
                    "access-token",
                    email="person@example.com",
                    proxy=proxy,
                    session_factory=lambda **_: session,
                )
                self.assertEqual(result["decision"], decision)
                self.assertEqual(plus_operator_note(result), note)
                self.assertNotIn("private", repr(result))

    def test_invalid_identity_headers_fall_back_to_a_stable_device(self):
        session = _Session(_Response({
            "accounts": {
                "default": {
                    "account": {"plan_type": "free", "is_deactivated": False},
                    "entitlement": {
                        "subscription_plan": "chatgptfreeplan",
                        "has_active_subscription": False,
                    },
                    "eligible_promo_campaigns": {},
                },
            },
        }))

        result = probe_plus_eligibility(
            "access-token",
            email="person@example.com",
            device_id="bad\r\nInjected: device",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["decision"], "campaign_not_available")
        headers = session.calls[0][1]["headers"]
        self.assertNotIn("ChatGPT-Account-ID", headers)
        self.assertEqual(str(uuid.UUID(headers["OAI-Device-Id"])), headers["OAI-Device-Id"])
        self.assertNotIn("Injected", repr(headers))

    def test_operator_log_label_fails_closed(self):
        self.assertEqual(
            safe_plus_result_label({"status": "plus_eligible", "label": "attacker"}),
            "Plus trial eligible",
        )
        self.assertEqual(
            safe_plus_result_label({"status": "untrusted", "label": "private secret"}),
            "Plus trial check failed",
        )


if __name__ == "__main__":
    unittest.main()
