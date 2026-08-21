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
            self.assertEqual(denied.headers["x-frame-options"], "DENY")
            self.assertIn("script-src 'self'", denied.headers["content-security-policy"])

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

    def test_admin_can_create_users_but_each_user_reads_only_their_database(self):
        from fastapi.testclient import TestClient
        from webui import auth, db
        from webui.app import app

        old_auth_db = auth.AUTH_DB_PATH
        old_user_dir = auth.USER_DATA_DIR
        old_db_path = db.DB_PATH
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            root = Path(temp.name)
            auth.AUTH_DB_PATH = root / "auth.db"
            auth.USER_DATA_DIR = root / "users"
            db.DB_PATH = root / "legacy.db"
            auth.init_auth_db()
            admin = auth.create_user("admin", "admin-password", role="admin")

            admin_client = TestClient(app)
            login = admin_client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
            csrf = login.cookies[auth.CSRF_COOKIE]
            for username in ("alice", "bob"):
                created = admin_client.post(
                    "/api/admin/users",
                    headers={"X-CSRF-Token": csrf},
                    json={"username": username, "password": f"{username}-secure-password", "role": "user"},
                )
                self.assertEqual(created.status_code, 200)

            users = {item["username"]: item for item in auth.list_users()}
            with db.use_database_path(users["alice"]["db_path"]):
                db.save_registered({"email": "alice@example.com", "access_token": "alice-token"})
            with db.use_database_path(users["bob"]["db_path"]):
                db.save_registered({"email": "bob@example.com", "access_token": "bob-token"})

            alice_client = TestClient(app)
            alice_login = alice_client.post("/api/auth/login", json={"username": "alice", "password": "alice-secure-password"})
            self.assertEqual(alice_login.status_code, 200)
            alice_rows = alice_client.get("/api/registered").json()["items"]
            self.assertEqual([row["email"] for row in alice_rows], ["alice@example.com"])
            self.assertEqual(alice_client.get("/api/admin/users").status_code, 403)

            bob_client = TestClient(app)
            bob_login = bob_client.post("/api/auth/login", json={"username": "bob", "password": "bob-secure-password"})
            self.assertEqual(bob_login.status_code, 200)
            bob_rows = bob_client.get("/api/registered").json()["items"]
            self.assertEqual([row["email"] for row in bob_rows], ["bob@example.com"])
        finally:
            auth.AUTH_DB_PATH = old_auth_db
            auth.USER_DATA_DIR = old_user_dir
            db.DB_PATH = old_db_path
            gc.collect()
            temp.cleanup()

    def test_first_setup_is_loopback_only_and_sets_secure_cookie_contract(self):
        from fastapi.testclient import TestClient
        from webui import auth, db
        from webui.app import app

        old_auth_db = auth.AUTH_DB_PATH
        old_user_dir = auth.USER_DATA_DIR
        old_db_path = db.DB_PATH
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            root = Path(temp.name)
            auth.AUTH_DB_PATH = root / "auth.db"
            auth.USER_DATA_DIR = root / "users"
            db.DB_PATH = root / "legacy.db"
            auth.init_auth_db()
            remote = TestClient(app, base_url="http://192.0.2.10")
            denied = remote.post("/api/auth/setup", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(denied.status_code, 403)

            local = TestClient(app, base_url="http://127.0.0.1")
            cross_origin = local.post(
                "/api/auth/setup",
                headers={"Origin": "https://attacker.example"},
                json={"username": "admin", "password": "admin-password"},
            )
            self.assertEqual(cross_origin.status_code, 403)
            created = local.post("/api/auth/setup", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(created.status_code, 200)
            cookies = {name.lower(): value for name, value in created.headers.items() if name.lower() == "set-cookie"}
            set_cookie = created.headers.get("set-cookie", "")
            self.assertIn("webui_session=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=strict", set_cookie)
            self.assertIn("webui_csrf=", set_cookie)
            self.assertEqual(local.post("/api/auth/setup", json={"username": "other", "password": "another-password"}).status_code, 409)
        finally:
            auth.AUTH_DB_PATH = old_auth_db
            auth.USER_DATA_DIR = old_user_dir
            db.DB_PATH = old_db_path
            gc.collect()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
