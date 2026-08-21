import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webui import db, registrar
from webui.auto_loop import AutoLoopController, AutoLoopState


class RealtimeRunEventTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._database_path = Path(self._temp_dir.name) / "tenant.db"
        with db.use_database_path(self._database_path):
            db.init_db()

    def tearDown(self):
        registrar._run_queues.clear()
        if hasattr(registrar, "_run_event_sinks"):
            registrar._run_event_sinks.clear()
        self._temp_dir.cleanup()

    def test_event_sink_receives_run_events_without_allocating_an_sse_queue(self):
        received = []
        account = {
            "email": "one@example.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "refresh",
            "kind": "outlook",
        }

        with db.use_database_path(self._database_path), \
                mock.patch.object(registrar.threading, "Thread"):
            run_id = registrar.start_registration(
                account,
                {},
                event_sink=lambda emitted_run_id, kind, data: received.append(
                    (emitted_run_id, kind, data)
                ),
            )
            self.assertIsNone(registrar.get_run_queue(run_id))
            registrar._publish_run_event(run_id, "log", {"line": "live line"})

        self.assertEqual(received, [(run_id, "log", {"line": "live line"})])

    def test_auto_loop_passes_a_multiplexed_event_sink_to_registration(self):
        controller = AutoLoopController(self._database_path)
        controller._state = AutoLoopState.RUNNING
        controller._target_count = 1
        controller._options = {"target_count": 1, "cool_down_seconds": 0}
        provider = type("Provider", (), {"pooled": True})
        account = {
            "email": "two@example.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "refresh",
            "kind": "outlook",
        }

        with db.use_database_path(self._database_path), \
                mock.patch("webui.auto_loop.get_provider_class", return_value=provider), \
                mock.patch.object(db, "claim_next", return_value=account), \
                mock.patch.object(registrar, "start_registration", return_value="run-two") as start, \
                mock.patch.object(controller, "_wait_run_finish", return_value=(True, "")):
            controller._worker_loop_scoped(0)

        event_sink = start.call_args.kwargs.get("event_sink")
        self.assertTrue(callable(event_sink))

        subscriber = controller.subscribe()
        subscriber.get_nowait()  # Initial state snapshot.
        event_sink("run-two", "log", {"line": "multiplexed line"})
        self.assertEqual(
            subscriber.get_nowait(),
            {
                "kind": "run_event",
                "data": {
                    "run_id": "run-two",
                    "event": "log",
                    "data": {"line": "multiplexed line"},
                },
            },
        )

    def test_subscriber_replays_events_emitted_before_the_browser_connected(self):
        controller = AutoLoopController(self._database_path)
        controller._worker_status[0] = {
            "run_id": "run-before-connect",
            "email": "early@example.com",
        }
        controller._publish_run_event(
            "run-before-connect",
            "log",
            {"line": "early live line"},
        )

        subscriber = controller.subscribe()
        subscriber.get_nowait()  # Initial state snapshot.
        self.assertEqual(
            subscriber.get_nowait()["data"]["data"]["line"],
            "early live line",
        )


if __name__ == "__main__":
    unittest.main()
