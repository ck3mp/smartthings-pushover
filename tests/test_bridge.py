"""Bridge tests against a fake DTLS session. Nothing here opens a socket."""

import logging
import re
import threading
import time
from types import SimpleNamespace

import cbor2
import pytest

from smartthings_pushover.bridge import ApplianceBridge
from smartthings_pushover.config import ALL_EVENTS, ApplianceConfig, Config
from smartthings_pushover.persistence import TrackerStore
from smartthings_pushover.pushover import NullSender

from .test_appliance import IDLE_LINKS, with_state

COAP_CONTENT = 0x45


def make_cfg(**over) -> Config:
    base = dict(
        appliances=(ApplianceConfig(kind="washer", ip="192.0.2.10", port=49154, name="Washer",
                                    course_names={"1C": "Eco 40-60"}),),
        cert_path="", key_path="",
        pushover_token="t", pushover_user="u", pushover_device=None, pushover_sound=None,
        pushover_priority=0, pushover_finished_sound="magic", pushover_alarm_priority=1,
        events=frozenset(ALL_EVENTS) - {"startup"},
        alarm_repeat_s=600.0,
    )
    base.update(over)
    return Config(**base)


def tree(links):
    return [{"href": href, "rep": rep} for href, rep in links.items()]


class FakeSession:
    """Just enough of DtlsCoapSession for the bridge and the library's
    worker tasks (PollScheduler, KeepaliveTask, ObserveRefreshTask)."""

    def __init__(self, links, *, hang_connect=False, **kw):
        self.links = links
        self.kw = kw
        self.hang_connect = hang_connect
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.subscribed = []
        self.gets = []

    def connect(self, *, cancel=None, **_):
        self.connected.set()
        if self.hang_connect:
            while not (cancel is not None and cancel.is_set()):
                time.sleep(0.01)
            raise RuntimeError("handshake cancelled")

    def start_reader(self):
        pass

    def subscribe(self, path, **_):
        self.subscribed.append(tuple(path))

    def get(self, path, *_, **__):
        self.gets.append(tuple(path))
        if tuple(path) == ("device", "0"):
            return COAP_CONTENT, cbor2.dumps(tree(self.links))
        href = "/" + "/".join(path)
        return COAP_CONTENT, cbor2.dumps(self.links.get(href, {}))

    def pace(self):
        pass

    def ping(self):
        return True

    def refresh_observes(self, paths, **_):
        pass

    def join(self):
        self.closed.wait()

    def close(self):
        self.closed.set()


def make_bridge(links, cfg=None, *, hang_connect=False, store=None):
    cfg = cfg or make_cfg()
    sender = NullSender()
    sessions = []

    def factory(host, port, **kw):
        s = FakeSession(links, hang_connect=hang_connect, **kw)
        sessions.append(s)
        return s

    bridge = ApplianceBridge(cfg.appliances[0], cfg, sender, logging.getLogger("test"),
                             auth=object(), session_factory=factory, store=store)
    return bridge, sender, sessions


def deliver(bridge, href, rep):
    bridge._on_observe_delivery(SimpleNamespace(payload=cbor2.dumps(rep), href=href,
                                                registration=False))


def plain(m):
    """Strip Pushover HTML so assertions read like the log lines."""
    return re.sub(r"<[^>]+>", "", m["message"])


def messages(sender):
    return [plain(m).splitlines()[0] for m in sender.submitted]


# ---- seed ------------------------------------------------------------------
def test_seed_mid_cycle_is_silent_and_remembers_course():
    """Regression: the seed applied resources one by one with the change
    hook live, so a restart mid-cycle announced 'Cycle started' (and any
    standing alarm) and stamped the start time as 'now'."""
    links = with_state(IDLE_LINKS, "Run", "Wash", "01:10:00")
    links["/alarms/vs/0"] = {"x.com.samsung.da.items": [{"x.com.samsung.da.code": "4C"}]}
    bridge, sender, _ = make_bridge(links)
    bridge.seed(FakeSession(links))
    assert sender.submitted == []
    assert bridge.tracker.course == "1C" and bridge.tracker.started_at is None
    assert bridge.current_state().running


def test_seed_then_observe_start_and_finish():
    bridge, sender, _ = make_bridge(dict(IDLE_LINKS))
    bridge.seed(FakeSession(IDLE_LINKS))
    running = with_state(IDLE_LINKS, "Run", "Wash", "01:10:00")["/operational/state/vs/0"]
    deliver(bridge, "/operational/state/vs/0", running)
    assert messages(sender) == ["Status: Started"]
    assert "Programme: Eco 40-60" in plain(sender.submitted[0])
    assert sender.submitted[0]["html"] is True
    assert sender.submitted[0]["message"].startswith("<b>Status:</b> Started")
    assert sender.submitted[0]["sound"] is None
    done = with_state(IDLE_LINKS, "End", "Finish", "00:00:00")["/operational/state/vs/0"]
    deliver(bridge, "/operational/state/vs/0", done)
    assert messages(sender)[-1] == "Status: Complete"
    assert sender.submitted[-1]["sound"] == "magic"
    assert sender.submitted[-1]["priority"] == 0


def test_reseed_after_long_outage_reports_finished_not_cancelled():
    running = with_state(IDLE_LINKS, "Run", "Rinse", "00:25:00")
    bridge, sender, _ = make_bridge(running)
    bridge.seed(FakeSession(running))
    # Bridge drops, the whole rest of the cycle happens, it reconnects to Ready.
    bridge.seed(FakeSession(IDLE_LINKS), outage_s=1800.0)
    assert messages(sender) == ["Status: Complete"]
    assert "Note: Finished while the bridge was disconnected" in plain(sender.submitted[0])


def test_reseed_after_short_outage_reports_cancelled():
    running = with_state(IDLE_LINKS, "Run", "Rinse", "00:25:00")
    bridge, sender, _ = make_bridge(running)
    bridge.seed(FakeSession(running))
    bridge.seed(FakeSession(IDLE_LINKS), outage_s=30.0)
    assert messages(sender) == ["Status: Cancelled"]
    assert "Programme: Eco 40-60" in plain(sender.submitted[0])


def test_seed_rejects_bad_response():
    bridge, _, _ = make_bridge(dict(IDLE_LINKS))

    class Bad(FakeSession):
        def get(self, path, *a, **k):
            return 0x84, b""

    with pytest.raises(RuntimeError, match=r"4\.04"):
        bridge.seed(Bad(IDLE_LINKS))


def test_startup_event_once_when_enabled():
    cfg = make_cfg(events=frozenset(ALL_EVENTS))
    bridge, sender, _ = make_bridge(dict(IDLE_LINKS), cfg)
    bridge.seed(FakeSession(IDLE_LINKS))
    bridge.seed(FakeSession(IDLE_LINKS))
    assert [plain(m) for m in sender.submitted] == ["Status: Bridge Started\nAppliance State: Ready"]


class RegReplySession(FakeSession):
    """Answers every OBSERVE registration the moment all subscriptions are
    in, in a caller-chosen order. The real reader thread does this within
    ~100 ms, i.e. before seed() has finished its blocking GET."""

    def __init__(self, links, *, reply_order=None, **kw):
        super().__init__(links, **kw)
        self.reply_order = reply_order or (lambda href: 0)
        self.pending = []

    def subscribe(self, path, **_):
        href = "/" + "/".join(path)
        self.pending.append(href)
        self.subscribed.append(tuple(path))
        if len(self.pending) == 9:  # every observe path is registered
            cb = self.kw["on_observe_delivery"]
            for h in sorted(self.pending, key=self.reply_order):
                cb(SimpleNamespace(payload=cbor2.dumps(self.links.get(h, {})), href=h,
                                   registration=True))


def _run_until_connected(bridge):
    t = threading.Thread(target=bridge.run_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 3
    while bridge.connect_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge.connect_count == 1
    time.sleep(0.2)
    return t


@pytest.mark.parametrize("state_reply", ["first", "last"])
def test_registration_replies_before_seed_are_silent(state_reply):
    """Regression: registration replies landed between subscribe() and
    seed() and were diffed against a partial baseline. With the state
    resource answering last, a fresh start mid-cycle sent 'Started'."""
    links = with_state(IDLE_LINKS, "Run", "Wash", "01:10:00")
    links["/alarms/vs/0"] = {"x.com.samsung.da.items": [{"x.com.samsung.da.code": "ErrorCode_DC"}]}
    rank = (lambda h: 0 if "operational" in h else 1) if state_reply == "first" else (
        lambda h: 1 if "operational" in h else 0)
    cfg = make_cfg()
    sender = NullSender()
    bridge = ApplianceBridge(
        cfg.appliances[0], cfg, sender, logging.getLogger("test"), auth=object(),
        session_factory=lambda h, p, **kw: RegReplySession(links, reply_order=rank, **kw))
    t = _run_until_connected(bridge)
    assert sender.submitted == []  # no 'Started', no re-announced standing alarm
    assert bridge.tracker.course == "1C" and bridge.tracker.started_at is None
    # ...and evaluation is live again afterwards.
    done = with_state(IDLE_LINKS, "End", "Finish", "00:00:00")["/operational/state/vs/0"]
    deliver(bridge, "/operational/state/vs/0", done)
    assert messages(sender) == ["Status: Complete"]
    bridge.request_stop()
    t.join(3)


def test_failed_seed_does_not_leave_evaluation_suppressed():
    class BadOnce(RegReplySession):
        calls = 0

        def get(self, path, *a, **k):
            BadOnce.calls += 1
            if BadOnce.calls == 1:
                raise OSError("timeout")
            return super().get(path, *a, **k)

    cfg = make_cfg()
    sender = NullSender()
    bridge = ApplianceBridge(
        cfg.appliances[0], cfg, sender, logging.getLogger("test"), auth=object(),
        session_factory=lambda h, p, **kw: BadOnce(dict(IDLE_LINKS), **kw))
    t = threading.Thread(target=bridge.run_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while bridge.connect_count < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert bridge.connect_count == 2 and bridge.error_count == 1
    time.sleep(0.2)
    deliver(bridge, "/operational/state/vs/0",
            with_state(IDLE_LINKS, "Run", "Wash")["/operational/state/vs/0"])
    assert messages(sender) == ["Status: Started"]
    bridge.request_stop()
    t.join(3)


# ---- dispatch --------------------------------------------------------------
def test_alarm_is_forwarded_once_with_alarm_priority():
    bridge, sender, _ = make_bridge(dict(IDLE_LINKS))
    bridge.seed(FakeSession(IDLE_LINKS))
    alarm = {"x.com.samsung.da.items": [{"x.com.samsung.da.code": "5C"}]}
    deliver(bridge, "/alarms/vs/0", alarm)
    deliver(bridge, "/alarms/vs/0", {})
    deliver(bridge, "/alarms/vs/0", alarm)  # flaps back within the window
    assert len(sender.submitted) == 1
    assert sender.submitted[0]["priority"] == 1
    assert "Code: 5C\nMeaning: Drain problem" in plain(sender.submitted[0])


def test_alarm_reraised_with_new_timestamp_is_one_push():
    """Captured: the washer re-sends the door alarm with a fresh
    triggeredTime each time it re-checks."""
    bridge, sender, _ = make_bridge(dict(IDLE_LINKS))
    bridge.seed(FakeSession(IDLE_LINKS))

    def alarm(ts):
        return {"x.com.samsung.da.items": [{
            "x.com.samsung.da.id": "0", "x.com.samsung.da.description": "Alarm",
            "x.com.samsung.da.alarmType": "Device", "x.com.samsung.da.code": "ErrorCode_DC",
            "x.com.samsung.da.triggeredTime": ts, "x.com.samsung.da.state": "Created"}]}

    deliver(bridge, "/alarms/vs/0", alarm("2026-09-10T12:40:48"))
    deliver(bridge, "/alarms/vs/0", {})
    deliver(bridge, "/alarms/vs/0", alarm("2026-09-10T12:40:49"))
    assert len(sender.submitted) == 1
    assert "Code: DC\nMeaning: Door open" in plain(sender.submitted[0])


def test_stale_sweep_does_not_flap_state():
    """Captured: a /device/0 sweep that began while the door was open landed
    after the OBSERVE push saying Run, producing a phantom pause + resume."""
    running = with_state(IDLE_LINKS, "Run", "Wash", "00:20:00")
    cfg = make_cfg()
    sender = NullSender()
    bridge = ApplianceBridge(cfg.appliances[0], cfg, sender, logging.getLogger("test"),
                             auth=object(), session_factory=lambda *a, **k: None,
                             sweep_settle_s=0.1)
    bridge.seed(FakeSession(running))
    stale_pause = with_state(IDLE_LINKS, "Pause", "Wash", "00:20:00")["/operational/state/vs/0"]
    def settled():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with bridge._sweep_lock:
                if bridge._sweep_timer is None:
                    return
            time.sleep(0.01)
        raise AssertionError("sweep settle timer did not fire")

    bridge.cache.apply_rep("/operational/state/vs/0", stale_pause, source="sweep")
    assert sender.submitted == []  # not judged yet
    # The hot poll corrects it before the settle window closes.
    bridge.cache.apply_rep("/operational/state/vs/0", running["/operational/state/vs/0"],
                           source="poll")
    settled()
    assert sender.submitted == []
    # A sweep-only change that persists is still reported after settling.
    done = with_state(IDLE_LINKS, "End", "Finish", "00:00:00")["/operational/state/vs/0"]
    bridge.cache.apply_rep("/operational/state/vs/0", done, source="sweep")
    settled()
    assert messages(sender) == ["Status: Complete"]


def test_unconfigured_events_are_logged_not_sent(caplog):
    cfg = make_cfg(events=frozenset({"cycle_finished"}))
    bridge, sender, _ = make_bridge(dict(IDLE_LINKS), cfg)
    bridge.seed(FakeSession(IDLE_LINKS))
    with caplog.at_level(logging.INFO, logger="test"):
        deliver(bridge, "/operational/state/vs/0",
                with_state(IDLE_LINKS, "Run", "Wash")["/operational/state/vs/0"])
    assert sender.submitted == []
    assert any("not forwarded" in r.message for r in caplog.records)


# ---- persistence -----------------------------------------------------------
def test_tracker_persists_and_restores(tmp_path):
    store = TrackerStore(tmp_path / "washer.json")
    bridge, _sender, _ = make_bridge(dict(IDLE_LINKS), store=store)
    bridge.seed(FakeSession(IDLE_LINKS))
    deliver(bridge, "/operational/state/vs/0",
            with_state(IDLE_LINKS, "Run", "Wash", "02:00:00")["/operational/state/vs/0"])
    saved = store.load()
    assert saved["course"] == "1C" and saved["started_at"] is not None
    # A new process starts mid-cycle and inherits the start time.
    running = with_state(IDLE_LINKS, "Run", "Rinse", "00:40:00")
    bridge2, sender2, _ = make_bridge(running, store=store)
    assert bridge2.tracker.started_at == saved["started_at"]
    bridge2.seed(FakeSession(running))
    assert sender2.submitted == []
    deliver(bridge2, "/operational/state/vs/0",
            with_state(IDLE_LINKS, "End", "Finish", "00:00:00")["/operational/state/vs/0"])
    assert "Duration:" in plain(sender2.submitted[0])
    assert store.load()["started_at"] is None  # cleared after the finish


# ---- lifecycle -------------------------------------------------------------
def test_stop_during_handshake_returns_promptly_and_is_not_an_error():
    bridge, _, sessions = make_bridge(dict(IDLE_LINKS), hang_connect=True)
    t = threading.Thread(target=bridge.run_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 2
    while not sessions and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sessions and sessions[0].connected.wait(2)
    t0 = time.monotonic()
    bridge.request_stop()
    t.join(3)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 2
    assert bridge.session is None
    assert bridge.error_count == 0  # a cancelled handshake is a clean stop


def test_full_session_runs_workers_and_shuts_down_cleanly():
    bridge, sender, sessions = make_bridge(dict(IDLE_LINKS))
    t = threading.Thread(target=bridge.run_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 3
    while bridge.connect_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge.connect_count == 1
    sess = sessions[0]
    assert ("operational", "state", "vs", "0") in sess.subscribed
    assert ("device", "0") in sess.gets
    # Worker threads start just after the seed; give them a moment to appear.
    expected = {"washer-poll", "washer-ping", "washer-obsref", "washer-health"}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        names = {th.name for th in threading.enumerate()}
        if expected <= names:
            break
        time.sleep(0.01)
    assert expected <= names
    bridge.request_stop()
    t.join(5)
    assert not t.is_alive()
    assert sess.closed.is_set()
    assert sender.submitted == []


def test_session_loss_reconnects_with_outage_tracking():
    bridge, _sender, sessions = make_bridge(dict(IDLE_LINKS))
    t = threading.Thread(target=bridge.run_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 3
    while bridge.connect_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    sessions[0].close()  # socket died
    deadline = time.monotonic() + 5
    while bridge.connect_count < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert bridge.connect_count == 2
    assert bridge.reach.down_since is None
    bridge.request_stop()
    t.join(5)
    assert not t.is_alive()
