"""Turn consecutive ApplianceState snapshots into human-readable events.

Pure functions: no I/O, no threads, easy to unit test. The bridge feeds
`detect()` every time the cache changes and forwards whatever comes back
to Pushover (filtered by the configured event set).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .appliance import ApplianceState


@dataclass(frozen=True)
class Event:
    kind: str            # one of config.ALL_EVENTS
    title: str
    message: str
    priority: int | None = None   # None -> use the default priority
    sound: str | None = None      # None -> use the default sound


@dataclass
class CycleTracker:
    """Mutable bookkeeping that spans a cycle: when it started and what
    course it ran, so the finished message can say 'after 1h 12m'."""
    started_at: float | None = None
    course: str | None = None
    initial_remaining_s: int | None = None
    course_names: dict[str, str] = field(default_factory=dict)
    kind: str = "washer"                     # picks the wording of a few messages

    def course_label(self, code: str | None) -> str | None:
        if code is None:
            return None
        return self.course_names.get(code.upper(), f"Course {code}")


def fmt_duration(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, int(round(seconds)))
    h, rest = divmod(seconds, 3600)
    m, _ = divmod(rest, 60)
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


_FINISHED_HEADLINE = {"washer": "Laundry is done", "dryer": "Laundry is dry"}


def _settings_line(s: ApplianceState) -> str:
    bits = []
    if s.water_temp and s.water_temp != "None":
        bits.append(f"{s.water_temp}°" if s.water_temp.isdigit() else s.water_temp)
    if s.spin and s.spin != "None":
        bits.append(f"{s.spin} rpm" if s.spin.isdigit() else s.spin)
    if s.rinse and s.rinse.isdigit():
        n = int(s.rinse)
        bits.append(f"{n} rinse" + ("s" if n != 1 else ""))
    if s.dry_level and s.dry_level != "None":
        bits.append(f"{s.dry_level.lower()} dry")
    if s.dry_time_s:
        bits.append(f"{fmt_duration(s.dry_time_s)} timed")
    return ", ".join(bits)


def detect(prev: ApplianceState | None, cur: ApplianceState, tracker: CycleTracker,
           name: str, now: float | None = None) -> list[Event]:
    """Compare two snapshots and return the events that happened between
    them. `prev is None` means this is the first snapshot after process
    start: establish a baseline and emit nothing."""
    now = time.time() if now is None else now
    if prev is None:
        if cur.in_cycle and tracker.started_at is None:
            # Joined mid-cycle: remember what we can so a later finish
            # still gets a reasonable message.
            tracker.course = cur.course
            tracker.initial_remaining_s = cur.remaining_s
        return []

    events: list[Event] = []

    # ---- power ---------------------------------------------------------
    if prev.power != cur.power and cur.power is not None and prev.power is not None:
        if cur.power == "On":
            events.append(Event("power_on", name, "Powered on"))
        elif cur.power == "Off":
            events.append(Event("power_off", name, "Powered off"))

    # ---- cycle transitions -------------------------------------------
    was, is_ = prev, cur
    if not was.running and is_.running:
        if was.paused:
            events.append(Event("cycle_resumed", name, _resumed_msg(is_)))
        else:
            tracker.started_at = now
            tracker.course = is_.course
            tracker.initial_remaining_s = is_.remaining_s
            events.append(Event("cycle_started", name,
                                _started_msg(is_, tracker)))
    elif was.running and is_.paused:
        events.append(Event("cycle_paused", name, _paused_msg(is_)))
    elif was.finished:
        # Already reported. End/Finish -> Ready (door opened, dial turned)
        # just clears the tracker; Finish -> Finish is a no-op.
        if not is_.in_cycle and not is_.finished:
            _reset(tracker)
    elif was.in_cycle and is_.finished:
        events.append(Event("cycle_finished", name,
                            _finished_msg(is_, tracker, now),
                            sound=None))
        _reset(tracker)
    elif was.in_cycle and not is_.in_cycle and not is_.finished:
        # Run/Pause -> Ready without passing through End: user hit
        # cancel, or power was cut. Distinguish a completed cycle that
        # only reported Finish via progress from a genuine cancel.
        if was.progress == "Finish" or (was.remaining_s is not None and was.remaining_s <= 60):
            events.append(Event("cycle_finished", name,
                                _finished_msg(is_, tracker, now)))
        else:
            events.append(Event("cycle_cancelled", name,
                                _cancelled_msg(was, tracker, now)))
        _reset(tracker)

    # ---- phase (Wash -> Rinse -> Spin) ---------------------------------
    if (is_.running and was.running and prev.progress != cur.progress
            and cur.progress not in (None, "None")
            and not is_.finished and not was.finished):
        events.append(Event("phase_changed", name, _phase_msg(is_)))

    # ---- alarms -------------------------------------------------------
    if prev.alarms != cur.alarms:
        if cur.alarms:
            body = "\n".join(f"{k}: {v}" for k, v in cur.alarms)
            events.append(Event("alarm", f"{name}: alert", body, priority=None))
        # Alarm cleared: silent. A "cleared" message adds little.

    # ---- misc toggles ---------------------------------------------------
    if (prev.remote_control is not None and cur.remote_control is not None
            and prev.remote_control != cur.remote_control):
        state = "enabled" if cur.remote_control else "disabled"
        events.append(Event("remote_control", name, f"Remote control {state}"))
    if (prev.child_lock is not None and cur.child_lock is not None
            and prev.child_lock != cur.child_lock):
        state = "on" if cur.child_lock else "off"
        events.append(Event("child_lock", name, f"Child lock {state}"))

    return events


def _reset(tracker: CycleTracker) -> None:
    tracker.started_at = None
    tracker.course = None
    tracker.initial_remaining_s = None


def _started_msg(s: ApplianceState, t: CycleTracker) -> str:
    parts = ["Cycle started"]
    label = t.course_label(s.course)
    if label:
        parts[0] += f": {label}"
    settings = _settings_line(s)
    if settings:
        parts.append(settings)
    if s.remaining_s:
        parts.append(f"about {fmt_duration(s.remaining_s)} remaining")
    if s.delay_end_s:
        parts.append(f"delayed end in {fmt_duration(s.delay_end_s)}")
    return "\n".join(parts)


def _resumed_msg(s: ApplianceState) -> str:
    msg = "Cycle resumed"
    if s.remaining_s:
        msg += f", about {fmt_duration(s.remaining_s)} remaining"
    return msg


def _paused_msg(s: ApplianceState) -> str:
    msg = "Cycle paused"
    if s.progress and s.progress != "None":
        msg += f" during {s.progress.lower()}"
    if s.remaining_s:
        msg += f", {fmt_duration(s.remaining_s)} remaining"
    return msg


def _finished_msg(s: ApplianceState, t: CycleTracker, now: float) -> str:
    label = t.course_label(t.course or s.course)
    msg = _FINISHED_HEADLINE.get(t.kind, "Cycle finished")
    if label:
        msg += f": {label}"
    if t.started_at is not None:
        msg += f"\nFinished after {fmt_duration(now - t.started_at)}"
    elif t.initial_remaining_s:
        msg += f"\nCycle was about {fmt_duration(t.initial_remaining_s)}"
    return msg


def _cancelled_msg(was: ApplianceState, t: CycleTracker, now: float) -> str:
    label = t.course_label(t.course or was.course)
    msg = "Cycle stopped before finishing"
    if label:
        msg += f": {label}"
    if was.progress and was.progress != "None":
        msg += f"\nWas in {was.progress.lower()}"
        if was.remaining_s:
            msg += f" with {fmt_duration(was.remaining_s)} remaining"
    return msg


_PHASE_VERBS = {"Wash": "washing", "Rinse": "rinsing", "Spin": "spinning",
                "Weightsensing": "weighing the load",
                "Drying": "drying", "Cooling": "cooling down"}


def _phase_msg(s: ApplianceState) -> str:
    verb = _PHASE_VERBS.get(s.progress or "")
    msg = f"Now {verb}" if verb else f"Phase: {s.progress}"
    if s.remaining_s:
        msg += f", {fmt_duration(s.remaining_s)} remaining"
    if s.progress_pct is not None:
        msg += f" ({s.progress_pct}%)"
    return msg
