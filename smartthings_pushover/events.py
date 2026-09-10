"""Turn consecutive ApplianceState snapshots into notification events.

Pure functions: no I/O, no threads, easy to unit test. The bridge feeds
`detect()` every time the cache changes and forwards whatever comes back
to Pushover (filtered by the configured event set).

Every event is a list of `Label: value` fields. `Event.message` renders
them as plain text for logs; `Event.html()` renders them with bold labels
for Pushover (which accepts a small HTML subset when `html=1` is sent).
"""

from __future__ import annotations

import html as _html
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import alarms
from .appliance import ApplianceState
from .config import EventKind
from .kinds import spec

Field = tuple[str, str]


def fields(*pairs: tuple[str, str | None]) -> tuple[Field, ...]:
    """Drop pairs whose value is None or empty, keep order."""
    return tuple((label, value) for label, value in pairs if value)


@dataclass(frozen=True)
class Event:
    kind: EventKind
    title: str
    fields: tuple[Field, ...]
    priority: int | None = None  # None -> use the default priority
    sound: str | None = None  # None -> use the default sound
    dedupe_key: str | None = None  # alarms: what "the same alarm" means for throttling

    @property
    def message(self) -> str:
        """Plain text, one `Label: value` per line."""
        return "\n".join(f"{label}: {value}" for label, value in self.fields)

    def html(self) -> str:
        """Pushover HTML: bold labels, escaped values, one per line."""
        return "\n".join(
            f"<b>{_html.escape(label)}:</b> {_html.escape(value)}" for label, value in self.fields
        )


@dataclass
class CycleTracker:
    """Mutable bookkeeping that spans a cycle: when it started and what
    course it ran, so the finished message can report the duration.

    `started_at`, `course`, `initial_remaining_s` and `scheduled` are the
    persisted part (see `to_dict` / `restore`); the rest is static
    configuration for message wording."""

    started_at: float | None = None
    course: str | None = None
    initial_remaining_s: int | None = None
    scheduled: bool = False  # Delay End is armed; the cycle proper hasn't begun
    course_names: Mapping[str, str] = field(default_factory=dict)
    kind: str = "washer"  # picks the phase wording

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
    return "under 1m" if seconds else "0m"


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
            events.append(Event("power_on", name, fields(("Status", "Powered On"))))
        elif is_.power == "Off":
            events.append(Event("power_off", name, fields(("Status", "Powered Off"))))

    # ---- cycle transitions -------------------------------------------
    started_now = False
    if not was.running and is_.running:
        if is_.delay_waiting:
            if not tracker.scheduled:
                tracker._begin(is_, None)
                tracker.scheduled = True
                events.append(Event("cycle_scheduled", name, _scheduled(is_, tracker)))
            # else: door closed again during the wait; still just waiting.
        elif was.paused and tracker.active and not tracker.scheduled:
            events.append(Event("cycle_resumed", name, _resumed(is_, tracker)))
        else:
            tracker._begin(is_, now)
            started_now = True
            events.append(Event("cycle_started", name, _started(is_, tracker)))
    elif was.running and is_.running and tracker.scheduled and not is_.delay_waiting:
        # The Delay End wait is over and the drum has started.
        tracker._begin(is_, now)
        started_now = True
        events.append(Event("cycle_started", name, _started(is_, tracker)))
    elif was.running and is_.paused:
        if not tracker.scheduled:  # a pause during the Delay End wait is just the door
            events.append(Event("cycle_paused", name, _paused(is_, tracker)))
    elif was.finished:
        # Already reported. End/Finish -> Ready (door opened, dial turned)
        # just clears the tracker; Finish -> Finish is a no-op.
        if not is_.in_cycle and not is_.finished:
            tracker._reset()
    elif was.in_cycle and is_.finished:
        events.append(Event("cycle_finished", name, _finished(is_, tracker, now)))
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
                    _finished(is_, tracker, now, while_offline=outage_covered),
                )
            )
        else:
            events.append(Event("cycle_cancelled", name, _cancelled(was, tracker)))
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
        events.append(Event("phase_changed", name, _phase(is_, tracker)))

    # ---- alarms -------------------------------------------------------
    if was.alarms != is_.alarms and is_.alarms:
        events.append(
            Event(
                "alarm",
                f"{name}: Error",
                alarms.alarm_fields(is_.alarms),
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
        state = "Enabled" if is_.remote_control else "Disabled"
        events.append(Event("remote_control", name, fields(("Remote Control", state))))
    if (
        was.child_lock is not None
        and is_.child_lock is not None
        and was.child_lock != is_.child_lock
    ):
        state = "On" if is_.child_lock else "Off"
        events.append(Event("child_lock", name, fields(("Child Lock", state))))

    return events


# ---------------------------------------------------------------------------
# field builders
# ---------------------------------------------------------------------------
def _phase_label(s: ApplianceState, t: CycleTracker) -> str | None:
    if s.progress in (None, "None"):
        return None
    return spec(t.kind).phase_labels.get(s.progress or "", s.progress)


def _settings(s: ApplianceState) -> list[tuple[str, str | None]]:
    def opt(v: str | None) -> str | None:
        return None if v in (None, "None") else v

    temp = opt(s.water_temp)
    spin = opt(s.spin)
    return [
        ("Temperature", f"{temp}°" if temp and temp.isdigit() else temp),
        ("Spin", f"{spin} rpm" if spin and spin.isdigit() else spin),
        ("Rinses", s.rinse if s.rinse and s.rinse.isdigit() else None),
        ("Dry Level", opt(s.dry_level)),
        ("Dry Time", fmt_duration(s.dry_time_s) if s.dry_time_s else None),
    ]


def _started(s: ApplianceState, t: CycleTracker) -> tuple[Field, ...]:
    return fields(
        ("Status", "Started"),
        ("Programme", t.course_label(s.course)),
        *_settings(s),
        ("Time Remaining", fmt_duration(s.remaining_s) if s.remaining_s else None),
    )


def _scheduled(s: ApplianceState, t: CycleTracker) -> tuple[Field, ...]:
    # The WW80 reports remainingTime == delayEndTime while waiting, so the
    # cycle length (and hence the start time) is not knowable.
    return fields(
        ("Status", "Delayed Start Armed"),
        ("Programme", t.course_label(s.course)),
        *_settings(s),
        ("Finishes In", fmt_duration(s.delay_end_s) if s.delay_end_s else None),
    )


def _resumed(s: ApplianceState, t: CycleTracker) -> tuple[Field, ...]:
    return fields(
        ("Status", "Resumed"),
        ("Phase", _phase_label(s, t)),
        ("Time Remaining", fmt_duration(s.remaining_s) if s.remaining_s else None),
    )


def _paused(s: ApplianceState, t: CycleTracker) -> tuple[Field, ...]:
    return fields(
        ("Status", "Paused"),
        ("Phase", _phase_label(s, t)),
        ("Time Remaining", fmt_duration(s.remaining_s) if s.remaining_s else None),
    )


def _finished(
    s: ApplianceState, t: CycleTracker, now: float, *, while_offline: bool = False
) -> tuple[Field, ...]:
    duration = None
    length = None
    if while_offline:
        pass
    elif t.started_at is not None:
        duration = fmt_duration(now - t.started_at)
    elif t.initial_remaining_s:
        length = f"About {fmt_duration(t.initial_remaining_s)}"
    return fields(
        ("Status", "Complete"),
        ("Programme", t.course_label(t.course or s.course)),
        ("Duration", duration),
        ("Cycle Length", length),
        ("Note", "Finished while the bridge was disconnected" if while_offline else None),
    )


def _cancelled(was: ApplianceState, t: CycleTracker) -> tuple[Field, ...]:
    return fields(
        ("Status", "Delayed Start Cancelled" if t.scheduled else "Cancelled"),
        ("Programme", t.course_label(t.course or was.course)),
        ("Phase", None if t.scheduled else _phase_label(was, t)),
        (
            "Time Remaining",
            fmt_duration(was.remaining_s) if was.remaining_s and not t.scheduled else None,
        ),
    )


def _phase(s: ApplianceState, t: CycleTracker) -> tuple[Field, ...]:
    return fields(
        ("Status", _phase_label(s, t)),
        ("Time Remaining", fmt_duration(s.remaining_s) if s.remaining_s else None),
        ("Percentage Complete", f"{s.progress_pct}%" if s.progress_pct is not None else None),
    )
