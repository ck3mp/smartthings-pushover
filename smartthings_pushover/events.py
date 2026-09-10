"""Turn consecutive ApplianceState snapshots into human-readable events.

Pure functions: no I/O, no threads, easy to unit test. The bridge feeds
`detect()` every time the cache changes and forwards whatever comes back
to Pushover (filtered by the configured event set).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import alarms
from .appliance import ApplianceState
from .config import EventKind
from .kinds import spec


@dataclass(frozen=True)
class Event:
    kind: EventKind
    title: str
    message: str
    priority: int | None = None  # None -> use the default priority
    sound: str | None = None  # None -> use the default sound
    dedupe_key: str | None = None  # alarms: what "the same alarm" means for throttling


@dataclass
class CycleTracker:
    """Mutable bookkeeping that spans a cycle: when it started and what
    course it ran, so the finished message can say 'after 1h 12m'.

    `started_at`, `course`, `initial_remaining_s` and `scheduled` are the
    persisted part (see `to_dict` / `restore`); the rest is static
    configuration for message wording."""

    started_at: float | None = None
    course: str | None = None
    initial_remaining_s: int | None = None
    scheduled: bool = False  # Delay End is armed; the cycle proper hasn't begun
    course_names: Mapping[str, str] = field(default_factory=dict)
    kind: str = "washer"  # picks the wording of a few messages

    @property
    def active(self) -> bool:
        """We know about a cycle (seen it start, joined it mid-way, or it
        is armed with Delay End). A Pause -> Run with no active cycle is
        a start, not a resume: the WW80 reports Ready -> Pause when Start
        is pressed with the door open."""
        return self.started_at is not None or self.initial_remaining_s is not None or self.scheduled

    def course_label(self, code: str | None) -> str | None:
        if code is None:
            return None
        return self.course_names.get(code.upper(), f"Course {code}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "course": self.course,
            "initial_remaining_s": self.initial_remaining_s,
            "scheduled": self.scheduled,
        }

    def restore(self, data: Mapping[str, Any]) -> None:
        started = data.get("started_at")
        self.started_at = float(started) if isinstance(started, int | float) else None
        course = data.get("course")
        self.course = str(course) if isinstance(course, str) and course else None
        remaining = data.get("initial_remaining_s")
        self.initial_remaining_s = int(remaining) if isinstance(remaining, int | float) else None
        self.scheduled = bool(data.get("scheduled", False))

    def _reset(self) -> None:
        self.started_at = None
        self.course = None
        self.initial_remaining_s = None
        self.scheduled = False

    def _begin(self, s: ApplianceState, now: float | None) -> None:
        self.started_at = now
        self.course = s.course
        self.initial_remaining_s = s.remaining_s
        self.scheduled = False


def fmt_duration(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, round(seconds))
    h, rest = divmod(seconds, 3600)
    m, _ = divmod(rest, 60)
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h"
    if m:
        return f"{m}m"
    return "under a minute" if seconds else "0m"


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


def _baseline(cur: ApplianceState, tracker: CycleTracker) -> None:
    """First snapshot after process start. Keep a persisted tracker only
    if it plausibly describes the cycle that is running right now."""
    if not cur.in_cycle:
        tracker._reset()
        return
    same_course = tracker.course is None or cur.course is None or tracker.course == cur.course
    plausible = (
        tracker.started_at is not None
        and same_course
        and (
            tracker.initial_remaining_s is None
            or cur.remaining_s is None
            or cur.remaining_s <= tracker.initial_remaining_s
        )
    )
    if not plausible:
        # Joined mid-cycle with nothing usable: remember what we can so a
        # later finish still gets a reasonable message.
        tracker._reset()
        tracker.course = cur.course
        tracker.initial_remaining_s = cur.remaining_s
        tracker.scheduled = cur.delay_waiting


def detect(
    prev: ApplianceState | None,
    cur: ApplianceState,
    tracker: CycleTracker,
    name: str,
    now: float | None = None,
    outage_s: float | None = None,
) -> list[Event]:
    """Compare two snapshots and return the events that happened between
    them. `prev is None` means this is the first snapshot after process
    start: establish a baseline and emit nothing. `outage_s` is how long
    the bridge was disconnected between the two snapshots, if it was."""
    now = time.time() if now is None else now
    if prev is None:
        _baseline(cur, tracker)
        return []

    events: list[Event] = []
    was, is_ = prev, cur

    # ---- power ---------------------------------------------------------
    if was.power != is_.power and is_.power is not None and was.power is not None:
        if is_.power == "On":
            events.append(Event("power_on", name, "Powered on"))
        elif is_.power == "Off":
            events.append(Event("power_off", name, "Powered off"))

    # ---- cycle transitions -------------------------------------------
    started_now = False
    if not was.running and is_.running:
        if is_.delay_waiting:
            if not tracker.scheduled:
                tracker._begin(is_, None)
                tracker.scheduled = True
                events.append(Event("cycle_scheduled", name, _scheduled_msg(is_, tracker)))
            # else: door closed again during the wait; still just waiting.
        elif was.paused and tracker.active and not tracker.scheduled:
            events.append(Event("cycle_resumed", name, _resumed_msg(is_)))
        else:
            tracker._begin(is_, now)
            started_now = True
            events.append(Event("cycle_started", name, _started_msg(is_, tracker)))
    elif was.running and is_.running and tracker.scheduled and not is_.delay_waiting:
        # The Delay End wait is over and the drum has started.
        tracker._begin(is_, now)
        started_now = True
        events.append(Event("cycle_started", name, _started_msg(is_, tracker)))
    elif was.running and is_.paused:
        if not tracker.scheduled:  # a pause during the Delay End wait is just the door
            events.append(Event("cycle_paused", name, _paused_msg(is_)))
    elif was.finished:
        # Already reported. End/Finish -> Ready (door opened, dial turned)
        # just clears the tracker; Finish -> Finish is a no-op.
        if not is_.in_cycle and not is_.finished:
            tracker._reset()
    elif was.in_cycle and is_.finished:
        events.append(Event("cycle_finished", name, _finished_msg(is_, tracker, now)))
        tracker._reset()
    elif was.in_cycle and not is_.in_cycle and not is_.finished:
        # Run/Pause -> Ready without passing through End: user hit
        # cancel, power was cut, or the whole rest of the cycle happened
        # while we were disconnected. Distinguish a completed cycle from
        # a genuine cancel.
        nearly_done = was.remaining_s is not None and was.remaining_s <= 60
        outage_covered = (
            outage_s is not None and was.remaining_s is not None and outage_s >= was.remaining_s
        )
        if was.progress == "Finish" or nearly_done or outage_covered:
            events.append(
                Event(
                    "cycle_finished",
                    name,
                    _finished_msg(is_, tracker, now, while_offline=outage_covered),
                )
            )
        else:
            events.append(Event("cycle_cancelled", name, _cancelled_msg(was, tracker)))
        tracker._reset()

    # ---- phase (Wash -> Rinse -> Spin) ---------------------------------
    if (
        is_.running
        and was.running
        and not started_now
        and not tracker.scheduled
        and was.progress != is_.progress
        and is_.progress not in (None, "None")
        and not is_.finished
        and not was.finished
    ):
        events.append(Event("phase_changed", name, _phase_msg(is_, tracker)))

    # ---- alarms -------------------------------------------------------
    if was.alarms != is_.alarms and is_.alarms:
        events.append(
            Event(
                "alarm",
                f"{name}: alert",
                alarms.describe(is_.alarms),
                dedupe_key=alarms.throttle_key(is_.alarms),
            )
        )
        # Alarm cleared: silent. A "cleared" message adds little.

    # ---- misc toggles ---------------------------------------------------
    if (
        was.remote_control is not None
        and is_.remote_control is not None
        and was.remote_control != is_.remote_control
    ):
        state = "enabled" if is_.remote_control else "disabled"
        events.append(Event("remote_control", name, f"Remote control {state}"))
    if (
        was.child_lock is not None
        and is_.child_lock is not None
        and was.child_lock != is_.child_lock
    ):
        state = "on" if is_.child_lock else "off"
        events.append(Event("child_lock", name, f"Child lock {state}"))

    return events


# ---------------------------------------------------------------------------
# message wording
# ---------------------------------------------------------------------------
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
    return "\n".join(parts)


def _scheduled_msg(s: ApplianceState, t: CycleTracker) -> str:
    label = t.course_label(s.course)
    parts = ["Delayed start set" + (f": {label}" if label else "")]
    settings = _settings_line(s)
    if settings:
        parts.append(settings)
    if s.delay_end_s:
        # The WW80 reports remainingTime == delayEndTime while waiting, so
        # the cycle length (and hence the start time) is not knowable.
        parts.append(f"finishes in about {fmt_duration(s.delay_end_s)}")
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


def _finished_msg(
    s: ApplianceState, t: CycleTracker, now: float, *, while_offline: bool = False
) -> str:
    label = t.course_label(t.course or s.course)
    msg = spec(t.kind).finished_headline
    if label:
        msg += f": {label}"
    if while_offline:
        msg += "\nFinished while the bridge was disconnected"
    elif t.started_at is not None:
        msg += f"\nFinished after {fmt_duration(now - t.started_at)}"
    elif t.initial_remaining_s:
        msg += f"\nCycle was about {fmt_duration(t.initial_remaining_s)}"
    return msg


def _cancelled_msg(was: ApplianceState, t: CycleTracker) -> str:
    label = t.course_label(t.course or was.course)
    msg = "Delayed start cancelled" if t.scheduled else "Cycle stopped before finishing"
    if label:
        msg += f": {label}"
    if not t.scheduled and was.progress and was.progress != "None":
        msg += f"\nWas in {was.progress.lower()}"
        if was.remaining_s:
            msg += f" with {fmt_duration(was.remaining_s)} remaining"
    return msg


def _phase_msg(s: ApplianceState, t: CycleTracker) -> str:
    verb = spec(t.kind).phase_verbs.get(s.progress or "")
    msg = f"Now {verb}" if verb else f"Phase: {s.progress}"
    if s.remaining_s:
        msg += f", {fmt_duration(s.remaining_s)} remaining"
    if s.progress_pct is not None:
        msg += f" ({s.progress_pct}%)"
    return msg
