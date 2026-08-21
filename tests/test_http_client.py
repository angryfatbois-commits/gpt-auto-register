import unittest
from unittest.mock import patch

import http_client


class HttpClientIsolationTests(unittest.TestCase):
    @unittest.skipUnless(http_client._HAS_CFFI, "curl_cffi is required")
    def test_isolated_post_matches_tls_profile_and_keeps_proxy(self):
        marker = object()
        session = http_client.create_http_session(
            proxy="socks5://proxy.example:1080",
            impersonate="chrome146",
        )

        try:
            with patch("curl_cffi.requests.post", return_value=marker) as post:
                response = session.post_isolated(
                    "https://chatgpt.com/backend-api/payments/checkout",
                    json={"billing_details": {"country": "PH", "currency": "PHP"}},
                    headers={"User-Agent": "Chrome/146"},
                    timeout=30,
                    allow_redirects=False,
                )
        finally:
            session.close()

        self.assertIs(response, marker)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["impersonate"], "chrome146")
        self.assertEqual(
            kwargs["proxies"],
            {
                "http": "socks5h://proxy.example:1080",
                "https": "socks5h://proxy.example:1080",
            },
        )
        self.assertFalse(kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
