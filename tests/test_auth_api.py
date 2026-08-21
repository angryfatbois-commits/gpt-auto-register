import gc
import tempfile
import unittest
from pathlib import Path


class AuthApiContractTests(unittest.TestCase):
    def test_public_auth_routes_and_protected_route_contract_are_defined(self):
        from webui.app import app

        paths = {route.path for route in app.routes}
        self.assertIn("/api/auth/login", paths)
        self.assertIn("/api/auth/me", paths)
        self.assertIn("/api/auth/logout", paths)
        self.assertIn("/api/admin/users", paths)

    def test_login_returns_csrf_cookie_and_protected_requests_require_session(self):
        from fastapi.testclient import TestClient
        from webui import auth

        old_auth_db = auth.AUTH_DB_PATH
        old_user_dir = auth.USER_DATA_DIR
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            root = Path(temp.name)
            auth.AUTH_DB_PATH = root / "auth.db"
            auth.USER_DATA_DIR = root / "users"
            auth.init_auth_db()
            auth.create_user("admin", "admin-password", role="admin")
            client = TestClient(__import__("webui.app", fromlist=["app"]).app)

            denied = client.get("/api/stats")
            self.assertEqual(denied.status_code, 401)

            logged_in = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "admin-password"},
            )
            self.assertEqual(logged_in.status_code, 200)
            self.assertIn("webui_csrf", logged_in.cookies)
            csrf = logged_in.cookies["webui_csrf"]
            allowed = client.get("/api/stats")
            self.assertEqual(allowed.status_code, 200)
            blocked_write = client.post("/api/accounts/reset_failed")
            self.assertEqual(blocked_write.status_code, 403)
            allowed_write = client.post(
                "/api/accounts/reset_failed",
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(allowed_write.status_code, 200)
        finally:
            auth.AUTH_DB_PATH = old_auth_db
            auth.USER_DATA_DIR = old_user_dir
            gc.collect()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
