from smartthings_pushover.appliance import ApplianceState
from smartthings_pushover.events import CycleTracker, detect, fmt_duration

NAME = "Washer"


def st(**kw):
    base = dict(
        power="On",
        machine_state="Ready",
        progress="None",
        remaining_s=9360,
        delay_end_s=0,
        course="1C",
        water_temp="40",
        spin="1400",
        rinse="2",
        remote_control=False,
        child_lock=False,
        alarms=(),
    )
    base.update(kw)
    return ApplianceState(**base)


def kinds(events):
    return [e.kind for e in events]


def test_first_snapshot_is_baseline_only():
    t = CycleTracker()
    assert detect(None, st(machine_state="Run", progress="Wash"), t, NAME) == []
    # joined mid-cycle: remembers the course for the eventual finish
    assert t.course == "1C"
    assert t.started_at is None


def test_baseline_from_partial_tree_then_full_tree_is_still_silent():
    """The bridge used to feed the detector resource by resource during a
    seed; a partial baseline must never look like a transition. This is
    the detector-level half of that guarantee: None -> Run with no prior
    knowledge is what a first full snapshot looks like."""
    t = CycleTracker()
    assert detect(None, ApplianceState(), t, NAME) == []
    assert t.course is None


def test_baseline_clears_stale_persisted_tracker_when_idle():
    t = CycleTracker(started_at=100.0, course="1C", initial_remaining_s=5000)
    detect(None, st(), t, NAME)
    assert t.started_at is None and t.course is None


def test_baseline_keeps_plausible_persisted_tracker():
    t = CycleTracker(started_at=100.0, course="1C", initial_remaining_s=5000)
    detect(None, st(machine_state="Run", progress="Rinse", remaining_s=1200), t, NAME)
    assert t.started_at == 100.0
    evs = detect(
        st(machine_state="Run", progress="Rinse", remaining_s=1200),
        st(machine_state="End", progress="Finish", remaining_s=0),
        t, NAME, now=4600.0,
    )
    assert "Finished after 1h 15m" in evs[0].message


def test_baseline_rejects_persisted_tracker_for_a_different_cycle():
    t = CycleTracker(started_at=100.0, course="1B", initial_remaining_s=5000)
    detect(None, st(machine_state="Run", progress="Wash", remaining_s=9000), t, NAME)
    assert t.started_at is None and t.course == "1C"
    # same course but more time remaining than the old cycle ever had -> new cycle
    t = CycleTracker(started_at=100.0, course="1C", initial_remaining_s=5000)
    detect(None, st(machine_state="Run", progress="Wash", remaining_s=9000), t, NAME)
    assert t.started_at is None


def test_no_change_no_events():
    t = CycleTracker()
    assert detect(st(), st(), t, NAME) == []


def test_cycle_started_message():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    evs = detect(
        st(),
        st(machine_state="Run", progress="Weightsensing", remaining_s=4200),
        t, NAME, now=1000.0,
    )
    assert kinds(evs) == ["cycle_started"]
    msg = evs[0].message
    assert "Cycle started: Eco 40-60" in msg
    assert "40°, 1400 rpm, 2 rinses" in msg
    assert "about 1h 10m remaining" in msg
    assert t.started_at == 1000.0


def test_unknown_course_shows_code():
    t = CycleTracker()
    evs = detect(st(), st(machine_state="Run"), t, NAME)
    assert "Course 1C" in evs[0].message


def test_pause_resume():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME)
    evs = detect(
        st(machine_state="Run", progress="Wash"),
        st(machine_state="Pause", progress="Wash", remaining_s=1800), t, NAME,
    )
    assert kinds(evs) == ["cycle_paused"]
    assert "during wash" in evs[0].message and "30m remaining" in evs[0].message
    evs = detect(
        st(machine_state="Pause", progress="Wash"),
        st(machine_state="Run", progress="Wash"), t, NAME,
    )
    assert kinds(evs) == ["cycle_resumed"]
    # resume must not reset the start time
    assert t.started_at is not None


def test_finished_via_end_state_reports_elapsed():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(
        st(machine_state="Run", progress="Spin", remaining_s=60),
        st(machine_state="End", progress="Finish", remaining_s=0),
        t, NAME, now=4500.0,
    )
    assert kinds(evs) == ["cycle_finished"]
    assert "Laundry is done: Eco 40-60" in evs[0].message
    assert "Finished after 1h 15m" in evs[0].message
    assert t.started_at is None


def test_finished_via_progress_finish_while_state_still_run():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(
        st(machine_state="Run", progress="Spin"),
        st(machine_state="Run", progress="Finish"), t, NAME, now=100.0,
    )
    assert kinds(evs) == ["cycle_finished"]
    # Subsequent Finish -> Finish ticks and Finish -> Ready must not emit.
    evs = detect(
        st(machine_state="Run", progress="Finish", remaining_s=10),
        st(machine_state="Run", progress="Finish", remaining_s=0), t, NAME,
    )
    assert evs == []
    evs = detect(
        st(machine_state="Run", progress="Finish"),
        st(machine_state="Ready", progress="None"), t, NAME,
    )
    assert evs == []
    # End -> Ready after a reported End likewise stays silent.
    evs = detect(
        st(machine_state="End", progress="Finish"),
        st(machine_state="Ready", progress="None"), t, NAME,
    )
    assert evs == []
    # ...and the next cycle still starts cleanly.
    evs = detect(
        st(machine_state="End", progress="Finish"),
        st(machine_state="Run", progress="Weightsensing"), t, NAME,
    )
    assert kinds(evs) == ["cycle_started"]


def test_run_to_ready_is_cancel_unless_nearly_done():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(
        st(machine_state="Run", progress="Rinse", remaining_s=1500),
        st(machine_state="Ready", progress="None"), t, NAME, now=10.0,
    )
    assert kinds(evs) == ["cycle_cancelled"]
    assert "Was in rinse with 25m remaining" in evs[0].message

    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(
        st(machine_state="Run", progress="Spin", remaining_s=30),
        st(machine_state="Ready", progress="None"), t, NAME, now=10.0,
    )
    assert kinds(evs) == ["cycle_finished"]


def test_run_to_ready_after_outage_longer_than_remaining_is_finished():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    was = st(machine_state="Run", progress="Rinse", remaining_s=1500)
    evs = detect(was, st(), t, NAME, now=2000.0, outage_s=1800.0)
    assert kinds(evs) == ["cycle_finished"]
    assert "Laundry is done: Eco 40-60" in evs[0].message
    assert "while the bridge was disconnected" in evs[0].message
    # A short outage still reads as a cancel.
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(was, st(), t, NAME, now=100.0, outage_s=60.0)
    assert kinds(evs) == ["cycle_cancelled"]


def test_finish_after_reconnect_without_seen_start():
    """Bridge was down when the cycle started; first snapshot is mid-cycle."""
    t = CycleTracker()
    detect(None, st(machine_state="Run", progress="Wash", remaining_s=3600), t, NAME)
    evs = detect(
        st(machine_state="Run", progress="Wash", remaining_s=3600),
        st(machine_state="End", progress="Finish", remaining_s=0), t, NAME,
    )
    assert kinds(evs) == ["cycle_finished"]
    assert "Cycle was about 1h" in evs[0].message


def test_phase_changes_only_while_running():
    t = CycleTracker()
    evs = detect(
        st(machine_state="Run", progress="Wash"),
        st(machine_state="Run", progress="Rinse", remaining_s=1200, progress_pct=60), t, NAME,
    )
    assert kinds(evs) == ["phase_changed"]
    assert evs[0].message == "Now rinsing, 20m remaining (60%)"
    # Ready -> Run also changes progress, but that's the start event, not a phase.
    evs = detect(st(), st(machine_state="Run", progress="Wash"), t, NAME)
    assert kinds(evs) == ["cycle_started"]


def test_alarm_raised_then_cleared():
    t = CycleTracker()
    alarm = (("items", "{code=4C, alarmType=Water}"),)
    evs = detect(st(), st(alarms=alarm), t, NAME)
    assert kinds(evs) == ["alarm"]
    assert evs[0].title == "Washer: alert"
    assert evs[0].message.startswith("Error 4C: water supply problem")
    assert "items: {code=4C, alarmType=Water}" in evs[0].message
    assert evs[0].dedupe_key == "4C"
    assert detect(st(alarms=alarm), st(), t, NAME) == []


def test_alarm_real_door_open_shape():
    """Verbatim flattened shape captured from the WW80 on 2026-09-10."""
    t = CycleTracker()
    alarm = (("items", "{id=0, description=Alarm, alarmType=Device, code=ErrorCode_DC, "
                       "triggeredTime=2026-09-10T12:40:48, state=Created}"),)
    evs = detect(st(), st(alarms=alarm), t, NAME)
    assert evs[0].message.splitlines()[0] == "Error DC: door open or not latched"
    assert evs[0].dedupe_key == "DC"


def test_power_and_toggles():
    t = CycleTracker()
    assert kinds(detect(st(power="Off"), st(power="On"), t, NAME)) == ["power_on"]
    assert kinds(detect(st(power="On"), st(power="Off"), t, NAME)) == ["power_off"]
    assert kinds(detect(st(), st(remote_control=True), t, NAME)) == ["remote_control"]
    assert kinds(detect(st(), st(child_lock=True), t, NAME)) == ["child_lock"]
    # None on either side (resource not yet read) must never fire.
    assert detect(st(power=None), st(power="On"), t, NAME) == []


def test_remaining_time_ticks_do_not_emit():
    t = CycleTracker()
    evs = detect(
        st(machine_state="Run", progress="Wash", remaining_s=3600),
        st(machine_state="Run", progress="Wash", remaining_s=3540), t, NAME,
    )
    assert evs == []


def test_fmt_duration():
    assert fmt_duration(None) is None
    assert fmt_duration(0) == "0m"
    assert fmt_duration(59) == "under a minute"
    assert fmt_duration(60) == "1m"
    assert fmt_duration(3600) == "1h"
    assert fmt_duration(3600 + 5 * 60) == "1h 05m"


def test_dryer_wording():
    t = CycleTracker(course_names={"16": "Cotton"}, kind="dryer")
    idle = st(course="16", water_temp=None, spin=None, rinse=None,
              dry_level="Normal", dry_time_s=0, remaining_s=7200)
    running = st(course="16", machine_state="Run", progress="Drying",
                 water_temp=None, spin=None, rinse=None, dry_level="Normal",
                 dry_time_s=0, remaining_s=7200)
    detect(None, idle, t, "Dryer", now=0)
    evs = detect(idle, running, t, "Dryer", now=0)
    assert kinds(evs) == ["cycle_started"]
    assert "Cotton" in evs[0].message and "normal dry" in evs[0].message
    cooling = st(course="16", machine_state="Run", progress="Cooling",
                 water_temp=None, spin=None, rinse=None, remaining_s=300)
    evs = detect(running, cooling, t, "Dryer", now=100)
    assert kinds(evs) == ["phase_changed"] and "cooling down" in evs[0].message
    done = st(course="16", machine_state="End", progress="Finish",
              water_temp=None, spin=None, rinse=None, remaining_s=0)
    evs = detect(cooling, done, t, "Dryer", now=7300)
    assert kinds(evs) == ["cycle_finished"]
    assert evs[0].message.startswith("Laundry is dry: Cotton")


def test_timed_dry_settings_line():
    t = CycleTracker(kind="dryer")
    idle = st(course="27", water_temp=None, spin=None, rinse=None,
              dry_level="None", dry_time_s=5400)
    running = st(course="27", machine_state="Run", progress="Drying",
                 water_temp=None, spin=None, rinse=None, dry_level="None",
                 dry_time_s=5400)
    detect(None, idle, t, "Dryer", now=0)
    evs = detect(idle, running, t, "Dryer", now=0)
    assert "1h 30m timed" in evs[0].message and "dry," not in evs[0].message


# ---- Delay End (shapes captured on the WW80, 2026-09-10) -------------------
# Armed, not started:  Ready/None  remaining=04:00:00 delayEnd=04:00:00
# Waiting:             Run/None    remaining=04:00:00 delayEnd=04:00:00
# Normal start:        Run/None    delayEnd=00:00:00
def test_delay_end_scheduled_then_started():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    armed = st(remaining_s=4 * 3600, delay_end_s=4 * 3600)
    assert detect(st(), armed, t, NAME, now=0.0) == []
    waiting = st(machine_state="Run", progress="None", remaining_s=4 * 3600,
                 delay_end_s=4 * 3600)
    evs = detect(armed, waiting, t, NAME, now=0.0)
    assert kinds(evs) == ["cycle_scheduled"]
    assert "Delayed start set: Eco 40-60" in evs[0].message
    assert "finishes in about 4h" in evs[0].message
    assert t.scheduled and t.started_at is None
    # Countdown ticks: no events.
    later = st(machine_state="Run", progress="None", remaining_s=3 * 3600, delay_end_s=3 * 3600)
    assert detect(waiting, later, t, NAME, now=3600.0) == []
    # The wait ends and washing begins (whatever delayEnd then reports).
    started = st(machine_state="Run", progress="Weightsensing", remaining_s=9360,
                 delay_end_s=9360)
    evs = detect(later, started, t, NAME, now=5040.0)
    assert kinds(evs) == ["cycle_started"]  # not phase_changed as well
    assert not t.scheduled and t.started_at == 5040.0
    done = st(machine_state="End", progress="Finish", remaining_s=0)
    evs = detect(started, done, t, NAME, now=5040.0 + 9300)
    assert "Finished after 2h 35m" in evs[0].message


def test_delay_end_cancelled():
    t = CycleTracker()
    waiting = st(machine_state="Run", progress="None", remaining_s=4 * 3600, delay_end_s=4 * 3600)
    detect(st(), waiting, t, NAME, now=0.0)
    evs = detect(waiting, st(), t, NAME, now=60.0)
    assert kinds(evs) == ["cycle_cancelled"]
    assert evs[0].message.startswith("Delayed start cancelled")
    assert not t.scheduled


def test_delay_end_door_opened_during_wait_is_silent():
    t = CycleTracker()
    waiting = st(machine_state="Run", progress="None", remaining_s=4 * 3600, delay_end_s=4 * 3600)
    detect(st(), waiting, t, NAME, now=0.0)
    door_open = st(machine_state="Pause", progress="None", remaining_s=4 * 3600,
                   delay_end_s=4 * 3600)
    assert detect(waiting, door_open, t, NAME, now=10.0) == []
    assert detect(door_open, waiting, t, NAME, now=20.0) == []
    assert t.scheduled


def test_start_pressed_with_door_open_is_a_start_not_a_resume():
    """Captured: Ready -> Pause (door open, alarm DC) -> Run once closed."""
    t = CycleTracker()
    door_open = st(machine_state="Pause", progress="None")
    assert detect(st(), door_open, t, NAME, now=0.0) == []
    evs = detect(door_open, st(machine_state="Run", progress="None", remaining_s=1200),
                 t, NAME, now=5.0)
    assert kinds(evs) == ["cycle_started"]
    # Same thing with Delay End armed -> scheduled, not resumed.
    t = CycleTracker()
    door_open = st(machine_state="Pause", progress="None", remaining_s=14400, delay_end_s=14400)
    detect(st(), door_open, t, NAME, now=0.0)
    evs = detect(door_open, st(machine_state="Run", progress="None", remaining_s=14400,
                               delay_end_s=14400), t, NAME, now=5.0)
    assert kinds(evs) == ["cycle_scheduled"]


def test_pause_resume_after_joining_mid_cycle_is_a_resume():
    t = CycleTracker()
    detect(None, st(machine_state="Pause", progress="Wash", remaining_s=3000), t, NAME)
    evs = detect(st(machine_state="Pause", progress="Wash", remaining_s=3000),
                 st(machine_state="Run", progress="Wash", remaining_s=3000), t, NAME)
    assert kinds(evs) == ["cycle_resumed"]


def test_normal_start_has_zero_delay_end():
    t = CycleTracker()
    evs = detect(st(), st(machine_state="Run", progress="None", delay_end_s=0), t, NAME)
    assert kinds(evs) == ["cycle_started"]
