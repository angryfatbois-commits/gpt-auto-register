import gc
import tempfile
import unittest
import queue
from pathlib import Path
from unittest import mock

from webui import auth, db


class AuthAndTenantTests(unittest.TestCase):
    def setUp(self):
        self._auth_db = auth.AUTH_DB_PATH
        self._user_dir = auth.USER_DATA_DIR
        self._db_path = db.DB_PATH
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._temp_dir.name)
        auth.AUTH_DB_PATH = root / "auth.db"
        auth.USER_DATA_DIR = root / "users"
        db.DB_PATH = root / "legacy.db"
        auth.init_auth_db()

    def tearDown(self):
        auth.AUTH_DB_PATH = self._auth_db
        auth.USER_DATA_DIR = self._user_dir
        db.DB_PATH = self._db_path
        gc.collect()
        self._temp_dir.cleanup()

    def test_passwords_are_hashed_and_sessions_are_revocable(self):
        user = auth.create_user("admin", "correct horse battery staple", role="admin")
        self.assertNotIn("correct horse battery staple", repr(user))
        self.assertTrue(auth.verify_password("correct horse battery staple", user))
        self.assertFalse(auth.verify_password("wrong password", user))

        session = auth.create_session(user["id"])
        found = auth.get_user_by_session(session["token"])
        self.assertEqual(found["id"], user["id"])
        auth.revoke_session(session["token"])
        self.assertIsNone(auth.get_user_by_session(session["token"]))

    def test_users_get_distinct_database_files_and_data_isolated(self):
        first = auth.create_user("alice", "alice-password")
        second = auth.create_user("bob", "bob-password")
        self.assertNotEqual(first["db_path"], second["db_path"])

        with db.use_database_path(first["db_path"]):
            db.init_db()
            db.save_registered({"email": "alice@example.com", "access_token": "alice-token"})
            self.assertEqual(len(db.list_registered()), 1)

        with db.use_database_path(second["db_path"]):
            db.init_db()
            self.assertEqual(db.list_registered(), [])
            db.save_registered({"email": "bob@example.com", "access_token": "bob-token"})
            self.assertEqual([row["email"] for row in db.list_registered()], ["bob@example.com"])

        with db.use_database_path(first["db_path"]):
            self.assertEqual([row["email"] for row in db.list_registered()], ["alice@example.com"])

    def test_only_admin_can_create_users_and_last_admin_cannot_be_deleted(self):
        admin = auth.create_user("admin", "admin-password", role="admin")
        ordinary = auth.create_user("user", "user-password", role="user")
        self.assertTrue(auth.can_manage_users(admin))
        self.assertFalse(auth.can_manage_users(ordinary))
        with self.assertRaises(auth.AuthorizationError):
            auth.delete_user(ordinary["id"], actor=ordinary)
        with self.assertRaises(auth.ValidationError):
            auth.create_user("admin", "another-password", role="user")
        with self.assertRaises(auth.AuthorizationError):
            auth.delete_user(admin["id"], actor=admin)

    def test_runtime_queues_and_auto_loop_controllers_are_tenant_scoped(self):
        from webui import registrar
        from webui.auto_loop import AutoLoopRegistry

        first = auth.create_user("alice", "alice-password")
        second = auth.create_user("bob", "bob-password")
        first_queue = queue.Queue()
        second_queue = queue.Queue()
        try:
            with db.use_database_path(first["db_path"]):
                registrar._run_queues[registrar._queue_key("same-run")] = first_queue
            with db.use_database_path(second["db_path"]):
                registrar._run_queues[registrar._queue_key("same-run")] = second_queue
            with db.use_database_path(first["db_path"]):
                self.assertIs(registrar.get_run_queue("same-run"), first_queue)
            with db.use_database_path(second["db_path"]):
                self.assertIs(registrar.get_run_queue("same-run"), second_queue)

            registry = AutoLoopRegistry()
            self.assertIsNot(registry.get(first["db_path"]), registry.get(second["db_path"]))
            self.assertIs(registry.get(first["db_path"]), registry.get(first["db_path"]))
        finally:
            registrar._run_queues.clear()

    def test_auto_loop_closes_each_status_poll_connection(self):
        from webui.auto_loop import AutoLoopController

        class Cursor:
            @staticmethod
            def fetchone():
                return {"status": "done", "error_category": None}

        class Connection:
            closed = False

            @staticmethod
            def execute(*_args, **_kwargs):
                return Cursor()

            def close(self):
                self.closed = True

        connection = Connection()
        controller = AutoLoopController(self._temp_dir.name)
        with mock.patch.object(db, "_conn", return_value=connection):
            self.assertEqual(controller._wait_run_finish("run-id"), (True, ""))
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
