"""One persistent DTLS session per appliance, kept alive and reconnected,
feeding state changes through the event detector to Pushover.

Session anatomy mirrors the SmartThings-Local reference bridge:
StateCache (single source of truth) + PollScheduler (tiered polling,
carries the UX when OBSERVE goes quiet) + KeepaliveTask (half-open
detection) + ObserveRefreshTask (periodic re-subscribe), all retired
together when the session drops.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cbor2
from smartthings_local.ocf.keepalive import KeepaliveTask
from smartthings_local.ocf.observe_refresh import ObserveRefreshTask
from smartthings_local.ocf.poll_scheduler import PollScheduler
from smartthings_local.ocf.state_cache import StateCache
from smartthings_local.protocol.auth import CertificateAuth
from smartthings_local.protocol.coap import fmt_code
from smartthings_local.protocol.dtls_probe import probe_dtls_ports
from smartthings_local.protocol.dtls_session import ConnectCancellation, DtlsCoapSession

from . import appliance
from .alarms import AlarmThrottle
from .config import OCF_PORT_CANDIDATES, ApplianceConfig, Config
from .events import CycleTracker, Event, detect, fmt_duration, make_fields
from .health import Reachability
from .persistence import TrackerStore
from .pushover import Sender

COAP_CONTENT = 0x45  # 2.05
PREFERRED_PORT = 49154
OBSERVE_REFRESH_INTERVAL_S = 6 * 3600.0
LIVENESS_WINDOW_S = 60.0
WORKER_JOIN_TIMEOUT_S = 10.0
MAX_BACKOFF_S = 30.0
HEALTH_TICK_S = 15.0
# A /device/0 sweep takes ~2s and can land *after* a fresher OBSERVE push
# or hot poll, briefly writing stale state into the cache (seen as a
# phantom Pause/Run flap). Let the hot tier overwrite it before judging.
SWEEP_SETTLE_S = 3.0

SessionFactory = Callable[..., Any]
ProbeFn = Callable[..., Any]


class ApplianceBridge:
    def __init__(
        self,
        app: ApplianceConfig,
        cfg: Config,
        sender: Sender,
        logger: logging.Logger | None = None,
        *,
        auth: Any | None = None,
        session_factory: SessionFactory = DtlsCoapSession,
        probe: ProbeFn = probe_dtls_ports,
        store: TrackerStore | None = None,
        persist: bool = True,
        sweep_settle_s: float = SWEEP_SETTLE_S,
    ) -> None:
        self.app = app
        self.cfg = cfg
        self.sender = sender
        self.log = logger or logging.getLogger(app.kind)
        self.stop = threading.Event()

        self.auth = auth if auth is not None else CertificateAuth.from_files(
            cfg.cert_path, cfg.key_path
        )
        self._session_factory = session_factory
        self._probe = probe

        # StateCache only needs `.on_observation`; we have no per-rep hook.
        self.cache = StateCache(SimpleNamespace(on_observation=None))
        self.cache.set_on_change(self._on_cache_change)
        self._suppress_changes = False  # True while a seed applies the whole tree
        self._sweep_settle_s = sweep_settle_s
        self._sweep_timer: threading.Timer | None = None
        self._sweep_lock = threading.Lock()

        self.tracker = CycleTracker(course_names=dict(app.course_names), kind=app.kind)
        self._store = store
        if store is None and persist and cfg.state_dir:
            self._store = TrackerStore(Path(cfg.state_dir) / f"{app.kind}.json", self.log)
        self._saved_tracker: dict[str, Any] | None = None
        if self._store is not None:
            saved = self._store.load()
            if saved:
                self.tracker.restore(saved)
                self._saved_tracker = self.tracker.to_dict()
                self.log.info("restored cycle state: %s", self._saved_tracker)

        self._state_lock = threading.Lock()
        self._last_state: appliance.ApplianceState | None = None
        self._seeded_once = False

        self.session: Any | None = None
        self._session_lock = threading.Lock()
        self._cancel = ConnectCancellation()
        self._discovered_port: int | None = None
        self._serial: str | None = None

        self.reach = Reachability(cfg.offline_after_s)
        self.alarm_throttle = AlarmThrottle(cfg.alarm_repeat_s)

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
        self._cancel.set()  # aborts a handshake in progress
        self._close_session()

    def run_forever(self) -> None:
        threading.Thread(
            target=self._health_loop, daemon=True, name=f"{self.app.kind}-health"
        ).start()
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self._session_once()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - any session failure reconnects
                if not self.stop.is_set():  # a cancelled handshake is not an error
                    self.error_count += 1
                    self.log.warning("session error: %s", e)
            self._close_session()
            self._suppress_changes = False  # in case a seed never got to run
            self.reach.mark_down(time.time())
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
            with contextlib.suppress(Exception):
                sess.close()

    # ------------------------------------------------------------------
    # port resolution and connection
    # ------------------------------------------------------------------
    def resolve_port(self) -> int:
        if self.app.port is not None:
            return self.app.port
        if self._discovered_port is not None:
            res = self._probe(self.app.ip, (self._discovered_port,), timeout=2.0)
            if res.selected_port == self._discovered_port:
                return int(self._discovered_port)
            self.log.info("port %d no longer answers; re-probing", self._discovered_port)
            self._discovered_port = None
        if self.stop.is_set():
            raise ConnectionError("stop requested")
        res = self._probe(
            self.app.ip, OCF_PORT_CANDIDATES, preferred_port=PREFERRED_PORT, timeout=3.0
        )
        if res.selected_port is None:
            raise ConnectionError(
                f"no DTLS listener found on {self.app.ip} "
                f"(outcome={res.outcome}); is the {self.app.kind} on Wi-Fi?"
            )
        port = int(res.selected_port)
        self.log.info("discovered DTLS port %d", port)
        self._discovered_port = port
        return port

    def open_session(self) -> Any:
        """Resolve the port, handshake, start the reader. Shared by the
        session loop and `--dump`. The caller owns the returned session."""
        port = self.resolve_port()
        sess = self._session_factory(
            self.app.ip,
            port,
            auth=self.auth,
            on_observe_delivery=self._on_observe_delivery,
            local_port=self.app.dtls_local_port,
        )
        sess.connect(cancel=self._cancel)
        self.log.info("DTLS connected to %s:%d", self.app.ip, port)
        sess.start_reader()
        return sess

    # ------------------------------------------------------------------
    # one session
    # ------------------------------------------------------------------
    def _session_once(self) -> None:
        sess = self.open_session()
        with self._session_lock:
            if self.stop.is_set():
                sess.close()
                return
            self.session = sess
        self.connect_count += 1

        # Registration replies start landing on the reader thread as soon as
        # we subscribe. Hold evaluation until the seed has the whole tree, or
        # a fresh start mid-cycle diffs a partial baseline and announces a
        # cycle (or re-announces a standing alarm). seed() lifts the hold.
        self._suppress_changes = True
        for path in appliance.observe_paths(self.app.kind):
            sess.subscribe(list(path))

        now = time.time()
        self.seed(sess, outage_s=self.reach.outage_s(now))
        self._mark_reachable(now)

        scheduler = PollScheduler(
            sess,
            self.cache,
            tiers=appliance.poll_tiers(
                self.app.kind, self.cfg.hot_poll_s, self.cfg.hot_poll_active_s
            ),
            is_active_fn=appliance.is_active,
            logger=self.log,
        )
        keepalive = KeepaliveTask(
            sess,
            interval_s=self.cfg.ping_interval_s,
            fail_threshold=3,
            on_unreachable=lambda: self._on_unreachable(sess),
            logger=self.log,
            liveness_fn=lambda: (time.monotonic() - scheduler.last_success_ts) < LIVENESS_WINDOW_S,
        )
        refresh = ObserveRefreshTask(
            sess,
            paths=appliance.observe_paths(self.app.kind),
            interval_s=OBSERVE_REFRESH_INTERVAL_S,
            logger=self.log,
        )
        self._scheduler = scheduler

        session_stop = threading.Event()
        k = self.app.kind
        workers = [
            threading.Thread(
                target=scheduler.run_forever, args=(session_stop,), daemon=True, name=f"{k}-poll"
            ),
            threading.Thread(
                target=keepalive.run_forever, args=(session_stop,), daemon=True, name=f"{k}-ping"
            ),
            threading.Thread(
                target=refresh.run_forever, args=(session_stop,), daemon=True, name=f"{k}-obsref"
            ),
        ]
        try:
            for w in workers:
                w.start()
            sess.join()  # returns when the reader exits (socket died / closed)
            self.log.info("DTLS session ended")
        finally:
            keepalive.on_unreachable = None
            session_stop.set()
            deadline = time.monotonic() + WORKER_JOIN_TIMEOUT_S
            for w in workers:
                w.join(max(0.0, deadline - time.monotonic()))
            self._scheduler = None

    def seed(self, sess: Any, *, outage_s: float | None = None) -> None:
        """Read /device/0 and load the whole tree into the cache as one
        atomic step, then evaluate once. Applying resource by resource
        with the change hook live would diff against a half-built state
        and announce a cycle that was merely already running."""
        code, payload = sess.get(list(appliance.SEED_PATH), timeout=15.0)
        if code != COAP_CONTENT:
            raise RuntimeError(f"/device/0 -> {fmt_code(code)}")
        body = cbor2.loads(payload)
        links = StateCache.index_device_tree(body)
        if not links:
            raise RuntimeError("/device/0 returned no resources")
        self._suppress_changes = True
        try:
            for href, rep in links.items():
                self.cache.apply_rep(href, rep, source="seed")
        finally:
            self._suppress_changes = False
        self._evaluate("seed", outage_s=outage_s)

        info = self.cache.get("/information/vs/0") or {}
        serial = info.get("x.com.samsung.da.serialNum")
        desc = info.get("x.com.samsung.da.description")
        if serial and serial != self._serial:
            self._serial = serial
            self.log.info("identified %s serial=%s", desc or self.app.kind, serial)
        state = self.current_state()
        self.log.info(
            "seeded %d resources; state=%s progress=%s remaining=%s",
            len(links),
            state.machine_state,
            state.progress,
            state.remaining_hms(),
        )
        if not self._seeded_once:
            self._seeded_once = True
            self._dispatch(
                Event(
                    "startup",
                    self.app.name,
                    make_fields(
                        ("Status", "Bridge Started"),
                        ("Appliance State", state.machine_state or "Unknown"),
                    ),
                )
            )

    # ------------------------------------------------------------------
    # data path
    # ------------------------------------------------------------------
    def _on_observe_delivery(self, delivery: Any) -> None:
        try:
            rep = cbor2.loads(delivery.payload)
        except Exception as e:  # noqa: BLE001 - garbage payloads are logged and dropped
            self.log.debug("notify %s: undecodable payload: %s", delivery.href, e)
            return
        if not isinstance(rep, dict):
            return
        source = "observe-reg" if delivery.registration else "observe"
        self.log.debug("%s %s %s", source, delivery.href, rep)
        self.cache.apply_rep(delivery.href, rep, source=source)

    def _on_cache_change(self, changed: bool, source: str) -> None:
        if not changed or self._suppress_changes:
            return
        if source == "sweep":
            self._evaluate_after_settle()
            return
        self._evaluate(source)

    def _evaluate_after_settle(self) -> None:
        """Coalesce all changes from one sweep into a single evaluation a
        few seconds later, by which time the hot poll has had its say."""
        with self._sweep_lock:
            if self._sweep_timer is not None and self._sweep_timer.is_alive():
                return
            timer = threading.Timer(self._sweep_settle_s, self._sweep_settled)
            timer.daemon = True
            timer.name = f"{self.app.kind}-sweep-settle"
            self._sweep_timer = timer
            timer.start()

    def _sweep_settled(self) -> None:
        with self._sweep_lock:
            self._sweep_timer = None
        if not self.stop.is_set():
            self._evaluate("sweep")

    def current_state(self) -> appliance.ApplianceState:
        return appliance.flatten(self.cache.snapshot())

    def _evaluate(self, source: str, *, outage_s: float | None = None) -> None:
        with self._state_lock:
            cur = self.current_state()
            prev = self._last_state
            if prev == cur:
                return
            events = detect(prev, cur, self.tracker, self.app.name, outage_s=outage_s)
            self._last_state = cur
            if prev is not None and (
                prev.machine_state != cur.machine_state or prev.progress != cur.progress
            ):
                self.log.info(
                    "state %s/%s -> %s/%s (%s, remaining %s)",
                    prev.machine_state,
                    prev.progress,
                    cur.machine_state,
                    cur.progress,
                    source,
                    cur.remaining_hms(),
                )
            self._persist_tracker()
            if any(ev.kind == "alarm" for ev in events):
                raw = self.cache.get(appliance.ALARMS_HREF)
                self.log.info("raw %s: %r", appliance.ALARMS_HREF, raw)
        for ev in events:
            self.event_count += 1
            self._dispatch(ev)

    def _persist_tracker(self) -> None:
        if self._store is None:
            return
        snap = self.tracker.to_dict()
        if snap != self._saved_tracker and self._store.save(snap):
            self._saved_tracker = snap

    def _dispatch(self, ev: Event) -> None:
        flat = ev.message.replace("\n", " | ")
        if ev.kind not in self.cfg.events:
            self.log.info("event %s (not forwarded): %s", ev.kind, flat)
            return
        if ev.kind == "alarm":
            ok, reason = self.alarm_throttle.allow(ev.dedupe_key or ev.message, time.time())
            if not ok:
                self.log.info("event alarm suppressed (%s): %s", reason, flat)
                return
        priority = self.cfg.priority_for(ev.kind, ev.priority, datetime.now())
        sound = ev.sound
        if ev.kind == "cycle_finished" and self.cfg.pushover_finished_sound:
            sound = self.cfg.pushover_finished_sound
        self.notification_count += 1
        self.log.info("notify %s (priority %d): %s", ev.kind, priority, flat)
        self.sender.submit(ev.html(), title=ev.title, priority=priority, sound=sound, html=True)

    # ------------------------------------------------------------------
    # reachability
    # ------------------------------------------------------------------
    def _mark_reachable(self, now: float) -> None:
        outage, was_offline = self.reach.mark_up(now)
        if outage is not None:
            self.log.info("%s reachable again after %.0fs", self.app.kind, outage)
        if was_offline:
            self._dispatch(
                Event(
                    "online",
                    self.app.name,
                    make_fields(
                        ("Status", "Reachable"),
                        ("Unreachable For", fmt_duration(outage) if outage else None),
                    ),
                )
            )

    def _on_unreachable(self, sess: Any) -> None:
        # Keepalive says the session is half-open: no 2.05 in the liveness
        # window and/or ping sends failing. Tear it down so the outer loop
        # reconnects instead of sitting on a dead socket.
        self.log.warning("%s unreachable on current session; reconnecting", self.app.kind)
        with contextlib.suppress(Exception):
            sess.close()

    def _health_loop(self) -> None:
        interval = max(30.0, self.cfg.health_interval_s)
        tick = 0.0
        while not self.stop.wait(HEALTH_TICK_S):
            if self.reach.crossed_offline(time.time()):
                self._dispatch(
                    Event(
                        "offline",
                        self.app.name,
                        make_fields(
                            ("Status", "Unreachable"),
                            ("Unreachable For", fmt_duration(self.cfg.offline_after_s)),
                        ),
                    )
                )
            tick += HEALTH_TICK_S
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
            self.session is not None,
            state.machine_state,
            state.progress,
            state.remaining_hms(),
            self.connect_count,
            self.error_count,
            polls,
            poll_errs,
            stale_s,
            self.event_count,
            self.notification_count,
            self.sender.sent_count,
            self.sender.failed_count,
        )
