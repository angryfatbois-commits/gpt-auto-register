import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


from gcash_probe import probe_gcash
from webui import db
from webui.app import CheckGCashReq, api_check_gcash


def _confirmed_loopback_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={
            "x-gcash-probe-confirmation": "checkout-side-effects-acknowledged",
        },
    )


class _BrokenSession:
    def post(self, _url, **_kwargs):
        raise RuntimeError("sensitive transport detail")

    def close(self):
        pass


class GCashBinaryPolicyTests(unittest.TestCase):
    def test_missing_token_is_reported_as_unavailable_without_network(self):
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

    def test_transport_failure_is_binary_unavailable_with_diagnostics(self):
        result = probe_gcash(
            "access-token",
            session_factory=lambda **_kwargs: _BrokenSession(),
        )

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(result["decision"], "checkout_transport_error")
        self.assertTrue(result["retryable"])
        self.assertNotIn("sensitive transport detail", repr(result))

    def test_api_returns_and_persists_binary_result_for_missing_token(self):
        with patch(
            "webui.app.db.get_registered",
            return_value={"email": "person@example.com"},
        ), patch("webui.app.probe_gcash") as probe, patch(
            "webui.app.db.update_eligibility_check"
        ) as persist:
            response = api_check_gcash(
                CheckGCashReq(emails=["person@example.com"]),
                _confirmed_loopback_request(),
            )

        result = response["results"]["person@example.com"]
        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(response["summary"], {"eligible": 0, "ineligible": 1})
        probe.assert_not_called()
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(persist.call_args_list[0].args[:2], (
            "person@example.com", "plus_check",
        ))
        self.assertEqual(persist.call_args_list[1].args, (
            "person@example.com", "gcash_check", result,
        ))


class GCashLegacyResultTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.DB_PATH = Path(self._temp_dir.name) / "test.db"
        db.init_db()
        db.save_registered({
            "email": "legacy@example.com",
            "access_token": "access-token",
        })

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        gc.collect()
        self._temp_dir.cleanup()

    def test_saved_legacy_unknown_is_read_as_binary_unavailable(self):
        legacy = {
            "gcash_check": {
                "classification": "unknown",
                "eligible": None,
                "conclusive": False,
                "decision": "checkout_transport_error",
                "status": "unknown",
                "label": "GCash status unknown",
            }
        }
        con = db._conn()
        try:
            con.execute(
                "UPDATE registered SET extra_json=? WHERE email=?",
                (json.dumps(legacy), "legacy@example.com"),
            )
            con.commit()
        finally:
            con.close()

        result = db.list_registered()[0]["gcash_check"]

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertTrue(result["conclusive"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["label"], "GCash unavailable")
        self.assertEqual(result["decision"], "checkout_transport_error")

        detail = db.get_registered("legacy@example.com")["extra"]["gcash_check"]
        self.assertEqual(detail["classification"], "ineligible")
        self.assertEqual(detail["label"], "GCash unavailable")


if __name__ == "__main__":
    unittest.main()
