"""One persistent DTLS session per appliance, kept alive and reconnected,
feeding state changes through the event detector to Pushover.

Session anatomy mirrors the SmartThings-Local reference bridge:
StateCache (single source of truth) + PollScheduler (tiered polling,
carries the UX when OBSERVE goes quiet) + KeepaliveTask (half-open
detection) + ObserveRefreshTask (periodic re-subscribe), all retired
together when the session drops.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from types import SimpleNamespace

import cbor2
from smartthings_local.ocf.keepalive import KeepaliveTask
from smartthings_local.ocf.observe_refresh import ObserveRefreshTask
from smartthings_local.ocf.poll_scheduler import PollScheduler
from smartthings_local.ocf.state_cache import StateCache
from smartthings_local.protocol.auth import CertificateAuth
from smartthings_local.protocol.coap import fmt_code
from smartthings_local.protocol.dtls_probe import probe_dtls_ports
from smartthings_local.protocol.dtls_session import DtlsCoapSession

from . import appliance
from .config import OCF_PORT_CANDIDATES, ApplianceConfig, Config
from .events import CycleTracker, detect
from .pushover import PushoverSender

COAP_CONTENT = 0x45  # 2.05
PREFERRED_PORT = 49154
OBSERVE_REFRESH_INTERVAL_S = 6 * 3600.0
LIVENESS_WINDOW_S = 60.0
WORKER_JOIN_TIMEOUT_S = 10.0
MAX_BACKOFF_S = 30.0


class ApplianceBridge:

    def __init__(self, app: ApplianceConfig, cfg: Config, sender: PushoverSender,
                 logger: logging.Logger | None = None):
        self.app = app
        self.cfg = cfg
        self.sender = sender
        self.log = logger or logging.getLogger(app.kind)
        self.stop = threading.Event()

        self.auth = CertificateAuth.from_files(cfg.cert_path, cfg.key_path)
        # StateCache only needs `.on_observation`; we have no per-rep hook.
        self.cache = StateCache(SimpleNamespace(on_observation=None))
        self.cache.set_on_change(self._on_cache_change)

        self.tracker = CycleTracker(course_names=dict(app.course_names), kind=app.kind)
        self._state_lock = threading.Lock()
        self._last_state: appliance.ApplianceState | None = None
        self._seeded_once = False

        self.session: DtlsCoapSession | None = None
        self._session_lock = threading.Lock()
        self._discovered_port: int | None = None
        self._serial: str | None = None

        # Reachability bookkeeping for the offline/online events.
        self._down_since: float | None = None
        self._offline_notified = False

        # Counters for the periodic health line.
        self.connect_count = 0
        self.error_count = 0
        self.notification_count = 0
        self.event_count = 0
        self._scheduler: PollScheduler | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        self.stop.set()
        self._close_session()

    def run_forever(self) -> None:
        threading.Thread(target=self._health_loop, daemon=True,
                         name="health").start()
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self._session_once()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - any session failure reconnects
                self.error_count += 1
                self.log.warning("session error: %s", e)
            self._close_session()
            if self._down_since is None:
                self._down_since = time.time()
            if self.stop.is_set():
                break
            wait = min(backoff, MAX_BACKOFF_S) * random.uniform(0.7, 1.3)
            self.log.info("reconnect in %.1fs", wait)
            if self.stop.wait(wait):
                break
            backoff = min(backoff * 2, MAX_BACKOFF_S)
        self.log.info("bridge stopped")

    def _close_session(self) -> None:
        with self._session_lock:
            sess, self.session = self.session, None
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # port resolution
    # ------------------------------------------------------------------
    def _resolve_port(self) -> int:
        if self.app.port is not None:
            return self.app.port
        if self._discovered_port is not None:
            res = probe_dtls_ports(self.app.ip, (self._discovered_port,),
                                   timeout=2.0)
            if res.selected_port == self._discovered_port:
                return self._discovered_port
            self.log.info("port %d no longer answers; re-probing",
                          self._discovered_port)
            self._discovered_port = None
        res = probe_dtls_ports(self.app.ip, OCF_PORT_CANDIDATES,
                               preferred_port=PREFERRED_PORT, timeout=3.0)
        if res.selected_port is None:
            raise ConnectionError(
                f"no DTLS listener found on {self.app.ip} "
                f"(outcome={res.outcome}); is the {self.app.kind} on Wi-Fi?")
        self.log.info("discovered DTLS port %d", res.selected_port)
        self._discovered_port = res.selected_port
        return res.selected_port

    # ------------------------------------------------------------------
    # one session
    # ------------------------------------------------------------------
    def _session_once(self) -> None:
        port = self._resolve_port()
        sess = DtlsCoapSession(
            self.app.ip, port,
            auth=self.auth,
            on_observe_delivery=self._on_observe_delivery,
            local_port=self.app.dtls_local_port,
        )
        sess.connect()
        with self._session_lock:
            if self.stop.is_set():
                sess.close()
                return
            self.session = sess
        self.connect_count += 1
        self.log.info("DTLS connected to %s:%d", self.app.ip, port)
        sess.start_reader()

        for path in appliance.observe_paths(self.app.kind):
            sess.subscribe(list(path))

        self._seed(sess)
        self._mark_reachable()

        scheduler = PollScheduler(
            sess, self.cache,
            tiers=appliance.poll_tiers(self.app.kind, self.cfg.hot_poll_s,
                                       self.cfg.hot_poll_active_s),
            is_active_fn=appliance.is_active,
            logger=self.log,
        )
        keepalive = KeepaliveTask(
            sess,
            interval_s=self.cfg.ping_interval_s,
            fail_threshold=3,
            on_unreachable=lambda: self._on_unreachable(sess),
            logger=self.log,
            liveness_fn=lambda: (time.monotonic() - scheduler.last_success_ts)
            < LIVENESS_WINDOW_S,
        )
        refresh = ObserveRefreshTask(sess, paths=appliance.observe_paths(self.app.kind),
                                     interval_s=OBSERVE_REFRESH_INTERVAL_S,
                                     logger=self.log)
        self._scheduler = scheduler

        session_stop = threading.Event()
        workers = [
            threading.Thread(target=scheduler.run_forever, args=(session_stop,),
                             daemon=True, name="poll"),
            threading.Thread(target=keepalive.run_forever, args=(session_stop,),
                             daemon=True, name="ping"),
            threading.Thread(target=refresh.run_forever, args=(session_stop,),
                             daemon=True, name="obsref"),
        ]
        try:
            for w in workers:
                w.start()
            sess.join()   # returns when the reader exits (socket died / closed)
            self.log.info("DTLS session ended")
        finally:
            keepalive.on_unreachable = None
            keepalive.on_reachable = None
            session_stop.set()
            deadline = time.monotonic() + WORKER_JOIN_TIMEOUT_S
            for w in workers:
                w.join(max(0.0, deadline - time.monotonic()))
            self._scheduler = None

    def _seed(self, sess: DtlsCoapSession) -> None:
        code, payload = sess.get(list(appliance.SEED_PATH), timeout=15.0)
        if code != COAP_CONTENT:
            raise RuntimeError(f"/device/0 -> {fmt_code(code)}")
        body = cbor2.loads(payload)
        links = StateCache.index_device_tree(body)
        if not links:
            raise RuntimeError("/device/0 returned no resources")
        for href, rep in links.items():
            self.cache.apply_rep(href, rep, source="seed")
        info = self.cache.get("/information/vs/0") or {}
        serial = info.get("x.com.samsung.da.serialNum")
        desc = info.get("x.com.samsung.da.description")
        if serial and serial != self._serial:
            self._serial = serial
            self.log.info("identified %s serial=%s", desc or self.app.kind, serial)
        state = self.current_state()
        self.log.info("seeded %d resources; state=%s progress=%s remaining=%s",
                      len(links), state.machine_state, state.progress,
                      state.remaining_hms())
        if not self._seeded_once:
            self._seeded_once = True
            if self.cfg.startup_notify:
                self._notify("startup", self.app.name,
                             f"Bridge started. {self.app.name} is "
                             f"{state.machine_state or 'unknown'}.")

    # ------------------------------------------------------------------
    # data path
    # ------------------------------------------------------------------
    def _on_observe_delivery(self, delivery) -> None:
        try:
            rep = cbor2.loads(delivery.payload)
        except Exception as e:  # noqa: BLE001
            self.log.debug("notify %s: undecodable payload: %s", delivery.href, e)
            return
        if not isinstance(rep, dict):
            return
        source = "observe-reg" if delivery.registration else "observe"
        self.log.debug("%s %s %s", source, delivery.href, rep)
        self.cache.apply_rep(delivery.href, rep, source=source)

    def _on_cache_change(self, changed: bool, source: str) -> None:
        if not changed:
            return
        self._evaluate(source)

    def current_state(self) -> appliance.ApplianceState:
        return appliance.flatten(self.cache.snapshot())

    def _evaluate(self, source: str) -> None:
        with self._state_lock:
            cur = self.current_state()
            prev = self._last_state
            if prev == cur:
                return
            events = detect(prev, cur, self.tracker, self.app.name)
            self._last_state = cur
            if prev is not None and (prev.machine_state != cur.machine_state
                                     or prev.progress != cur.progress):
                self.log.info("state %s/%s -> %s/%s (%s, remaining %s)",
                              prev.machine_state, prev.progress,
                              cur.machine_state, cur.progress, source,
                              cur.remaining_hms())
        for ev in events:
            self.event_count += 1
            if ev.kind not in self.cfg.events:
                self.log.info("event %s (not forwarded): %s",
                              ev.kind, ev.message.replace("\n", " | "))
                continue
            priority = ev.priority
            sound = ev.sound
            if ev.kind == "alarm":
                priority = self.cfg.pushover_alarm_priority
            if ev.kind == "cycle_finished" and self.cfg.pushover_finished_sound:
                sound = self.cfg.pushover_finished_sound
            self._notify(ev.kind, ev.title, ev.message, priority=priority, sound=sound)

    def _notify(self, kind: str, title: str, message: str, *,
                priority: int | None = None, sound: str | None = None) -> None:
        self.notification_count += 1
        self.log.info("notify %s: %s", kind, message.replace("\n", " | "))
        self.sender.submit(message, title=title, priority=priority, sound=sound)

    # ------------------------------------------------------------------
    # reachability
    # ------------------------------------------------------------------
    def _mark_reachable(self) -> None:
        if self._down_since is not None:
            outage = time.time() - self._down_since
            self.log.info("%s reachable again after %.0fs", self.app.kind, outage)
        self._down_since = None
        if self._offline_notified:
            self._offline_notified = False
            if "online" in self.cfg.events:
                self._notify("online", self.app.name,
                             f"{self.app.name} is reachable again")

    def _on_unreachable(self, sess: DtlsCoapSession) -> None:
        # Keepalive says the session is half-open: no 2.05 in the liveness
        # window and/or ping sends failing. Tear it down so the outer loop
        # reconnects instead of sitting on a dead socket.
        self.log.warning("%s unreachable on current session; reconnecting", self.app.kind)
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass

    def _health_loop(self) -> None:
        interval = max(30.0, self.cfg.health_interval_s)
        tick = 0.0
        while not self.stop.wait(15.0):
            now = time.time()
            if (self._down_since is not None and not self._offline_notified
                    and now - self._down_since >= self.cfg.offline_after_s):
                self._offline_notified = True
                if "offline" in self.cfg.events:
                    self._notify("offline", self.app.name,
                                 f"{self.app.name} unreachable for "
                                 f"{int(self.cfg.offline_after_s // 60)} minutes")
            tick += 15.0
            if tick >= interval:
                tick = 0.0
                self._log_health()

    def _log_health(self) -> None:
        state = self.current_state()
        sched = self._scheduler
        polls = sched.poll_count if sched else 0
        poll_errs = sched.poll_error_count if sched else 0
        stale = self.cache.stalest()
        stale_s = f"{stale[1]:.0f}s" if stale else "n/a"
        self.log.info(
            "health: connected=%s state=%s/%s remaining=%s connects=%d "
            "errors=%d polls=%d poll_errors=%d stalest=%s events=%d "
            "notified=%d sent=%d failed=%d",
            self.session is not None, state.machine_state, state.progress,
            state.remaining_hms(), self.connect_count, self.error_count,
            polls, poll_errs, stale_s, self.event_count,
            self.notification_count, self.sender.sent_count,
            self.sender.failed_count)
