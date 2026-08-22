import gc
import json
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


from webui import db


class EligibilityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.DB_PATH = Path(self._temp_dir.name) / "test.db"
        db.init_db()
        db.save_registered({
            "email": "person@example.com",
            "access_token": "sensitive-access-token",
        })

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        gc.collect()
        self._temp_dir.cleanup()

    def test_list_registered_exposes_plus_and_gcash_checks(self):
        db.update_eligibility_check(
            "person@example.com",
            "plus_check",
            {"classification": "eligible", "conclusive": True, "status": "plus_eligible"},
        )
        db.update_eligibility_check(
            "person@example.com",
            "gcash_check",
            {
                "classification": "eligible",
                "conclusive": True,
                "status": "eligible",
                "trusted_custom_method_matched": True,
            },
        )

        row = db.list_registered()[0]

        self.assertEqual(row["plus_check"]["classification"], "eligible")
        self.assertEqual(row["gcash_check"]["classification"], "eligible")
        self.assertTrue(row["gcash_check"]["trusted_custom_method_matched"])

    def test_unknown_attempt_is_normalized_to_binary_unavailable(self):
        eligible = {
            "classification": "eligible",
            "conclusive": True,
            "eligible": True,
            "decision": "gcash_available",
        }
        unknown = {
            "classification": "unknown",
            "conclusive": False,
            "eligible": None,
            "decision": "checkout_timeout",
        }
        db.update_eligibility_check("person@example.com", "gcash_check", eligible)
        db.update_eligibility_check("person@example.com", "gcash_check", unknown)

        check = db.list_registered()[0]["gcash_check"]

        self.assertEqual(check["classification"], "ineligible")
        self.assertFalse(check["eligible"])
        self.assertEqual(check["label"], "GCash unavailable")
        self.assertEqual(check["last_conclusive"]["classification"], "ineligible")
        self.assertNotIn("sensitive-access-token", repr(check))

    def test_rejects_unknown_extra_json_key(self):
        with self.assertRaises(ValueError):
            db.update_eligibility_check(
                "person@example.com",
                "attacker_controlled_key",
                {"classification": "eligible", "conclusive": True},
            )

    def test_persists_safe_auth_refresh_status_without_raw_diagnostics(self):
        db.update_eligibility_check(
            "person@example.com",
            "gcash_check",
            {
                "classification": "ineligible",
                "conclusive": True,
                "decision": "checkout_http_400",
                "auth_refresh_status": "refreshed",
                "raw_response": "must-never-be-persisted",
            },
        )

        check = db.list_registered()[0]["gcash_check"]

        self.assertEqual(check["auth_refresh_status"], "refreshed")
        self.assertNotIn("raw_response", check)
        self.assertNotIn("must-never-be-persisted", repr(check))

    def test_read_paths_resanitize_tampered_eligibility_results(self):
        tampered = {
            "raw_response": "top-level-secret-response",
            "metadata": {
                "safe_marker": "kept",
                "response_body": "nested-secret-response",
                "accessToken": "camel-access-secret",
                "api_key": "snake-api-secret",
                "apiKey": "camel-api-secret",
                "bearer": "bearer-secret",
                "secret": "generic-secret",
                "token": "generic-token-secret",
                "nested": [
                    {
                        "refreshToken": "nested-refresh-secret",
                        "safe_marker": "nested-kept",
                    },
                ],
            },
            "plus_check": {
                "classification": "eligible",
                "conclusive": True,
                "raw_response": "plus-secret-response",
            },
            "gcash_check": {
                "classification": "ineligible",
                "conclusive": True,
                "decision": "checkout_http_400",
                "auth_refresh_status": "refreshed",
                "raw_response": "gcash-secret-response",
                "last_conclusive": {
                    "classification": "eligible",
                    "decision": "gcash_available",
                    "raw_response": "older-secret-response",
                },
            },
        }
        with db._connection() as con:
            con.execute(
                "UPDATE registered SET extra_json=? WHERE email=?",
                (json.dumps(tampered), "person@example.com"),
            )
            con.commit()

        surfaces = (
            db.list_registered()[0],
            db.get_registered("person@example.com")["extra"],
            db.list_registered_full()[0]["extra"],
            db.list_registered_by_emails(["person@example.com"])[0]["extra"],
        )

        for surface in surfaces:
            self.assertNotIn("raw_response", repr(surface))
            self.assertNotIn("response_body", repr(surface))
            self.assertNotIn("secret-response", repr(surface))
            self.assertNotIn("accessToken", repr(surface))
            self.assertNotIn("api_key", repr(surface))
            self.assertNotIn("apiKey", repr(surface))
            self.assertNotIn("bearer", repr(surface))
            self.assertNotIn("generic-secret", repr(surface))
            self.assertNotIn("generic-token-secret", repr(surface))
            self.assertNotIn("refreshToken", repr(surface))
        self.assertEqual(surfaces[1]["metadata"]["safe_marker"], "kept")
        self.assertEqual(
            surfaces[1]["metadata"]["nested"][0]["safe_marker"],
            "nested-kept",
        )
        gcash = surfaces[0]["gcash_check"]
        self.assertEqual(gcash["auth_refresh_status"], "refreshed")
        self.assertEqual(gcash["last_conclusive"]["classification"], "eligible")

    def test_concurrent_plus_and_gcash_updates_do_not_overwrite_each_other(self):
        barrier = threading.Barrier(2)
        original_sanitizer = db._safe_eligibility_result

        def synchronized_sanitizer(value):
            safe = original_sanitizer(value)
            try:
                barrier.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                # Once the database lock covers the whole read/merge/write cycle,
                # the second worker cannot reach this barrier concurrently.
                pass
            return safe

        with patch("webui.db._safe_eligibility_result", side_effect=synchronized_sanitizer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        db.update_eligibility_check,
                        "person@example.com",
                        "plus_check",
                        {"classification": "eligible", "conclusive": True},
                    ),
                    pool.submit(
                        db.update_eligibility_check,
                        "person@example.com",
                        "gcash_check",
                        {"classification": "ineligible", "conclusive": True},
                    ),
                ]
                for future in futures:
                    future.result(timeout=10)

        row = db.list_registered()[0]
        self.assertEqual(row["plus_check"]["classification"], "eligible")
        self.assertEqual(row["gcash_check"]["classification"], "ineligible")


if __name__ == "__main__":
    unittest.main()
