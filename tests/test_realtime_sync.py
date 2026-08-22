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
                mock.patch.object(controller, "_wait_run_finish", return_value=(True, "")), \
                mock.patch.object(
                    controller,
                    "_check_plus_after_registration",
                    create=True,
                ) as check_plus:
            controller._worker_loop_scoped(0)

        event_sink = start.call_args.kwargs.get("event_sink")
        self.assertTrue(callable(event_sink))
        check_plus.assert_called_once_with(
            "run-two",
            fallback_email="two@example.com",
            proxy="",
        )

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

    def test_post_registration_plus_check_uses_final_email_and_persists_result(self):
        controller = AutoLoopController(self._database_path)
        run_id = "run-generated-address"
        controller._publish_run_event(
            run_id,
            "status",
            {
                "kind": "done",
                "email": "final@example.com",
                "access_token_len": 100,
            },
        )
        with db.use_database_path(self._database_path):
            db.save_registered({
                "email": "final@example.com",
                "access_token": "stored-access-token",
                "device_id": "stored-device",
            })

        eligible = {
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "plus_1_month_free_available",
            "status": "plus_eligible",
            "label": "Plus trial eligible",
            "checked_at": 10.0,
        }
        with db.use_database_path(self._database_path), \
                mock.patch(
                    "webui.auto_loop.probe_plus_eligibility",
                    return_value=eligible,
                    create=True,
                ) as probe, \
                mock.patch.object(db, "update_plus_check") as persist:
            result = controller._check_plus_after_registration(
                run_id,
                fallback_email="placeholder@placeholder.local",
                proxy="http://ph-proxy.example:8080",
            )

        self.assertEqual(result, eligible)
        probe.assert_called_once_with(
            "stored-access-token",
            email="final@example.com",
            device_id="stored-device",
            proxy="http://ph-proxy.example:8080",
        )
        persist.assert_called_once_with("final@example.com", eligible)

    def test_post_registration_plus_failure_does_not_expose_or_raise(self):
        controller = AutoLoopController(self._database_path)
        run_id = "run-safe-failure"
        with db.use_database_path(self._database_path):
            db.save_registered({
                "email": "safe@example.com",
                "access_token": "stored-access-token",
            })

        with db.use_database_path(self._database_path), \
                mock.patch(
                    "webui.auto_loop.probe_plus_eligibility",
                    side_effect=RuntimeError("token-secret from upstream"),
                    create=True,
                ), \
                mock.patch.object(db, "update_plus_check") as persist:
            result = controller._check_plus_after_registration(
                run_id,
                fallback_email="safe@example.com",
                proxy="",
            )

        self.assertEqual(result["decision"], "automatic_check_failed")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("token-secret", repr(result))
        self.assertNotIn("token-secret", repr(controller._run_event_history))
        persist.assert_not_called()

    def test_automatic_plus_hook_crash_does_not_change_registration_success(self):
        controller = AutoLoopController(self._database_path)
        controller._state = AutoLoopState.RUNNING
        controller._target_count = 1
        controller._options = {"target_count": 1, "cool_down_seconds": 0}
        provider = type("Provider", (), {"pooled": True})
        account = {
            "email": "survives@example.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "refresh",
            "kind": "outlook",
        }

        with db.use_database_path(self._database_path), \
                mock.patch("webui.auto_loop.get_provider_class", return_value=provider), \
                mock.patch.object(db, "claim_next", return_value=account), \
                mock.patch.object(registrar, "start_registration", return_value="run-safe"), \
                mock.patch.object(controller, "_wait_run_finish", return_value=(True, "")), \
                mock.patch.object(
                    controller,
                    "_check_plus_after_registration",
                    side_effect=RuntimeError("sensitive post-check failure"),
                    create=True,
                ) as check_plus:
            controller._worker_loop_scoped(0)

        check_plus.assert_called_once()
        self.assertEqual(controller._registered_ok, 1)
        self.assertEqual(controller._registered_fail, 0)
        self.assertNotIn("sensitive post-check failure", repr(controller._run_event_history))

    def test_failed_registration_never_runs_plus_check(self):
        controller = AutoLoopController(self._database_path)
        controller._state = AutoLoopState.RUNNING
        controller._options = {"cool_down_seconds": 0}
        provider = type("Provider", (), {"pooled": True})
        account = {
            "email": "failed@example.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "refresh",
            "kind": "outlook",
        }

        def finish_with_failure(_run_id):
            controller._stop_event.set()
            return False, "account"

        with db.use_database_path(self._database_path), \
                mock.patch("webui.auto_loop.get_provider_class", return_value=provider), \
                mock.patch.object(db, "claim_next", return_value=account), \
                mock.patch.object(registrar, "start_registration", return_value="run-failed"), \
                mock.patch.object(
                    controller,
                    "_wait_run_finish",
                    side_effect=finish_with_failure,
                ), \
                mock.patch.object(
                    controller,
                    "_check_plus_after_registration",
                ) as check_plus:
            controller._worker_loop_scoped(0)

        check_plus.assert_not_called()
        self.assertEqual(controller._registered_ok, 0)
        self.assertEqual(controller._registered_fail, 1)


if __name__ == "__main__":
    unittest.main()
