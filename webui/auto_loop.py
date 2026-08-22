"""Concurrent auto-loop controller with one independent proxy per worker.

Design:
  - manage_loop observes stop/pause and scales workers to concurrency.
  - Workers repeat claim_next -> register -> finish.
  - Proxies are assigned round-robin to avoid multiple accounts on one IP.
  - State transitions are stopped -> running -> paused -> running/stopped.
  - Pause and stop let active workers finish rather than killing them.
  - Each account uses registrar.start_registration and its worker waits.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from . import db, registrar
from eligibility import plus_probe_error
from mail_providers import MailProviderError, get_provider_class
from plus_probe import (
    probe_plus_eligibility,
    safe_plus_result_label,
    should_persist_plus_result,
)

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    """Split newline-delimited proxies, skipping blanks and # comments."""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class AutoLoopController:
    """Multi-worker auto-loop controller.

    Important options:
      proxy:                legacy single proxy when concurrency=1
      proxy_pool:           one proxy per line, assigned by worker index
      concurrency:          worker count (1-20)
      cool_down_seconds:    delay after each worker run (default 3)
      remaining values pass through to registrar.start_registration
    """

    def __init__(self, database_path: str | Path | None = None):
        self._database_path = Path(
            database_path if database_path is not None else db.current_db_path()
        ).resolve()
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # Set means paused.
        # Progress counters.
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # Active work by worker_id -> email.
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # Circuit-breaker state.
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE subscribers.
        self._subscribers: list[queue.Queue] = []
        # Bounded replay lets a freshly loaded or reconnecting browser recover
        # recent logs for workers that are still active.
        self._run_event_history: deque[dict] = deque(maxlen=2000)
        # Proxy pool and concurrency.
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        # Successful target: 0 is unlimited; >0 stops when reached.
        self._target_count: int = 0

    # -------------------------------- Public API --------------------------------

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"Auto-loop is already active (state={self._state})"}
            # Reset state.
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._run_event_history.clear()
            self._consecutive_network_fails = 0
            self._last_message = "Auto-loop started"
            # Parse concurrency settings.
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            # Successful target; 0 means unlimited.
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            # Start the management thread.
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"Cannot pause while state={self._state}"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "Pause requested; active workers will finish first"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"Cannot resume while state={self._state}"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "Auto-loop resumed"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "Auto-loop is not running"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "Stop requested; active workers will finish first"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2500)
        with self._lock:
            self._subscribers.append(q)
            active_run_ids = {
                str(info.get("run_id") or "")
                for info in self._worker_status.values()
                if info.get("run_id")
            }
            replay = [
                item for item in self._run_event_history
                if str(item.get("data", {}).get("run_id") or "") in active_run_ids
            ]
            try:
                q.put_nowait({"kind": "state", "data": self._snapshot()})
                for item in replay:
                    q.put_nowait(item)
            except queue.Full:
                pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    def _publish_run_event(self, run_id: str, event: str, data: dict):
        """Multiplex one worker's log/status event onto auto subscribers."""
        packet = {
            "run_id": run_id,
            "event": event,
            "data": data,
        }
        with self._lock:
            self._run_event_history.append({"kind": "run_event", "data": packet})
        self._broadcast("run_event", packet)

    # -------------------------------- Internals --------------------------------

    def _snapshot(self) -> dict:
        with db.use_database_path(self._database_path), self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _proxy_for_worker(self, worker_id: int) -> str:
        """Select by worker_id, falling back to options.proxy for an empty pool."""
        if self._proxy_pool:
            return self._proxy_pool[worker_id % len(self._proxy_pool)]
        return self._options.get("proxy", "") or ""

    def _registered_email_for_run(self, run_id: str, fallback_email: str) -> str:
        """Resolve a generated provider's final address from its done event."""
        with self._lock:
            history = list(self._run_event_history)
        for item in reversed(history):
            if item.get("kind") != "run_event":
                continue
            packet = item.get("data")
            if not isinstance(packet, dict) or packet.get("run_id") != run_id:
                continue
            if packet.get("event") != "status":
                continue
            data = packet.get("data")
            if not isinstance(data, dict) or data.get("kind") != "done":
                continue
            email = str(data.get("email") or "").strip().lower()
            if email:
                return email
        return str(fallback_email or "").strip().lower()

    def _check_plus_after_registration(
        self,
        run_id: str,
        *,
        fallback_email: str,
        proxy: str,
    ) -> dict:
        """Run a best-effort Plus check without changing registration success."""
        email = self._registered_email_for_run(run_id, fallback_email)
        self._publish_run_event(
            run_id,
            "status",
            {"kind": "phase", "phase": "checking_plus_trial", "email": email},
        )
        try:
            credential = db.get_registered(email)
            if not credential:
                result = plus_probe_error(
                    "account_not_found",
                    retryable=False,
                    status="not_found",
                    label="Not found",
                )
            else:
                result = probe_plus_eligibility(
                    credential.get("access_token", ""),
                    email=email,
                    device_id=credential.get("device_id", ""),
                    proxy=proxy,
                )
            if should_persist_plus_result(result):
                db.update_plus_check(email, result)
        except Exception as error:
            logger.warning(
                "[plus-check] Automatic eligibility check failed safely (%s)",
                type(error).__name__,
            )
            result = plus_probe_error(
                "automatic_check_failed",
                retryable=True,
                status="error",
                label="Check failed",
            )

        self._publish_run_event(
            run_id,
            "log",
            {"line": f"[plus-check] {safe_plus_result_label(result)}"},
        )
        self._publish_run_event(
            run_id,
            "status",
            {
                "kind": "phase",
                "phase": "plus_trial_checked",
                "email": email,
                "plus_status": str(result.get("status") or "error"),
            },
        )
        return result

    def _record_finish(self, ok: bool, category: str):
        """Update counters and circuit-breaker state after a worker run."""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"Totals: ok={self._registered_ok} fail={self._registered_fail}"
            )
            # Reaching the target sets an idempotent stop event, safe across workers.
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 Target of {self._target_count} reached; stopping automatically "
                    f"(successful {self._registered_ok} / failed {self._registered_fail})"
                )
            logger.info(
                "Target of %s successful registrations reached; stopping automatically",
                self._target_count,
            )
            self._broadcast("state", self._snapshot())
            return

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"Paused automatically after {self._consecutive_network_fails} "
                    "consecutive network/environment errors. Accounts were released; "
                    "check the proxy before resuming."
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """Start workers, wait for them, and publish the final state."""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # Stagger workers by one second to avoid simultaneous requests.
                time.sleep(1.0)
            self._workers = workers
            # Wait for every worker to exit.
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop error: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = (
                    f"Stopped (successful {self._registered_ok} / "
                    f"failed {self._registered_fail})"
                )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """Bind this worker to its owner's database for its entire lifetime."""
        with db.use_database_path(self._database_path):
            self._worker_loop_scoped(worker_id)

    def _worker_loop_scoped(self, worker_id: int):
        """Run one worker's claim -> start -> wait -> repeat loop."""
        idle_round = 0
        proxy = self._proxy_for_worker(worker_id)
        logger.info(f"[worker-{worker_id}] Started (proxy={proxy or 'direct'})")

        while True:
            # Check for stop.
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] Stopped")
                return

            # Check for pause.
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # Successful plus active workers gates new claims at the target. Reuse
            # locked _worker_status rather than adding a leak-prone counter.
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] Target {self._target_count} "
                        "is fully allocated; exiting"
                    )
                    return

            # Pooled providers claim the next account; non-pooled providers use a
            # virtual placeholder while creating their own address.
            mail_source = db.get_setting("mail_source", "outlook")
            try:
                pooled = get_provider_class(mail_source).pooled
            except MailProviderError as e:
                logger.error(f"[worker-{worker_id}] {e}; stopping")
                self._set_message(str(e))
                return
            if pooled:
                account = db.claim_next(kind=mail_source)
            else:
                account = {
                    "email": f"{mail_source}_placeholder_"
                             f"{int(time.time())}_{worker_id}@placeholder.local",
                    "password": "", "client_id": "", "refresh_token": "",
                    "relay_url": "", "kind": mail_source,
                }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id}: account pool is empty; "
                        "waiting for new accounts..."
                    )
                # Stop this worker after ten empty rounds (about 30 seconds).
                if idle_round >= 10:
                    logger.info(
                        f"[worker-{worker_id}] Account pool remained empty for 30s; stopping"
                    )
                    return
                # Wait three seconds before retrying.
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # Inject this worker's proxy into the run.
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy

            # Start one run.
            try:
                run_id = registrar.start_registration(
                    account,
                    run_options,
                    event_sink=self._publish_run_event,
                )
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] Failed to start registration: {e}")
                if pooled:
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": proxy,
            })

            # Wait for the current run.
            ok, category = self._wait_run_finish(run_id)

            if ok:
                try:
                    self._check_plus_after_registration(
                        run_id,
                        fallback_email=account["email"],
                        proxy=proxy,
                    )
                except Exception as error:
                    # Eligibility is supplementary. Never turn a completed
                    # registration into a failure if the post-check itself has
                    # an unexpected implementation or storage error.
                    logger.warning(
                        "[plus-check] Post-registration hook failed safely (%s)",
                        type(error).__name__,
                    )

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._record_finish(ok, category)
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            # Cool down independently per worker.
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str]:
        """Poll the runs table until the run finishes."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False, ""
            con = db._conn()
            try:
                cur = con.execute(
                    "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
                )
                row = cur.fetchone()
            finally:
                con.close()
            if row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")
            time.sleep(1)
        logger.warning(f"run {run_id} did not finish within {timeout}s; giving up")
        return False, ""


class AutoLoopRegistry:
    """Return one independent controller for each physical tenant database."""

    def __init__(self):
        self._lock = threading.RLock()
        self._controllers: dict[str, AutoLoopController] = {}

    def get(self, database_path: str | Path | None = None) -> AutoLoopController:
        selected = Path(
            database_path if database_path is not None else db.current_db_path()
        ).resolve()
        key = str(selected)
        with self._lock:
            controller = self._controllers.get(key)
            if controller is None:
                controller = AutoLoopController(selected)
                self._controllers[key] = controller
            return controller


REGISTRY = AutoLoopRegistry()


def get_controller() -> AutoLoopController:
    return REGISTRY.get()


# Compatibility object for direct legacy imports. Authenticated HTTP routes use
# get_controller() and never share this instance across users.
CONTROLLER = REGISTRY.get(db.DB_PATH)
