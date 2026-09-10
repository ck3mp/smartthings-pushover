from smartthings_pushover.events import CycleTracker, detect, fmt_duration
from smartthings_pushover.appliance import ApplianceState as WasherState

NAME = "Washer"


def st(**kw):
    base = dict(power="On", machine_state="Ready", progress="None",
                remaining_s=9360, course="1C", water_temp="40", spin="1400",
                rinse="2", remote_control=False, child_lock=False, alarms=())
    base.update(kw)
    return WasherState(**base)


def kinds(events):
    return [e.kind for e in events]


def test_first_snapshot_is_baseline_only():
    t = CycleTracker()
    assert detect(None, st(machine_state="Run", progress="Wash"), t, NAME) == []
    # joined mid-cycle: remembers the course for the eventual finish
    assert t.course == "1C"


def test_no_change_no_events():
    t = CycleTracker()
    assert detect(st(), st(), t, NAME) == []


def test_cycle_started_message():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    evs = detect(st(), st(machine_state="Run", progress="Weightsensing",
                         remaining_s=4200), t, NAME, now=1000.0)
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
    evs = detect(st(machine_state="Run", progress="Wash"),
                 st(machine_state="Pause", progress="Wash", remaining_s=1800), t, NAME)
    assert kinds(evs) == ["cycle_paused"]
    assert "during wash" in evs[0].message and "30m remaining" in evs[0].message
    evs = detect(st(machine_state="Pause", progress="Wash"),
                 st(machine_state="Run", progress="Wash"), t, NAME)
    assert kinds(evs) == ["cycle_resumed"]
    # resume must not reset the start time
    assert t.started_at is not None


def test_finished_via_end_state_reports_elapsed():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(st(machine_state="Run", progress="Spin", remaining_s=60),
                 st(machine_state="End", progress="Finish", remaining_s=0),
                 t, NAME, now=4500.0)
    assert kinds(evs) == ["cycle_finished"]
    assert "Laundry is done: Eco 40-60" in evs[0].message
    assert "Finished after 1h 15m" in evs[0].message
    assert t.started_at is None


def test_finished_via_progress_finish_while_state_still_run():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(st(machine_state="Run", progress="Spin"),
                 st(machine_state="Run", progress="Finish"), t, NAME, now=100.0)
    assert kinds(evs) == ["cycle_finished"]
    # Subsequent Finish -> Finish ticks and Finish -> Ready must not emit.
    evs = detect(st(machine_state="Run", progress="Finish", remaining_s=10),
                 st(machine_state="Run", progress="Finish", remaining_s=0), t, NAME)
    assert evs == []
    evs = detect(st(machine_state="Run", progress="Finish"),
                 st(machine_state="Ready", progress="None"), t, NAME)
    assert evs == []
    # End -> Ready after a reported End likewise stays silent.
    evs = detect(st(machine_state="End", progress="Finish"),
                 st(machine_state="Ready", progress="None"), t, NAME)
    assert evs == []
    # ...and the next cycle still starts cleanly.
    evs = detect(st(machine_state="End", progress="Finish"),
                 st(machine_state="Run", progress="Weightsensing"), t, NAME)
    assert kinds(evs) == ["cycle_started"]


def test_run_to_ready_is_cancel_unless_nearly_done():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(st(machine_state="Run", progress="Rinse", remaining_s=1500),
                 st(machine_state="Ready", progress="None"), t, NAME, now=10.0)
    assert kinds(evs) == ["cycle_cancelled"]
    assert "Was in rinse with 25m remaining" in evs[0].message

    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(st(machine_state="Run", progress="Spin", remaining_s=30),
                 st(machine_state="Ready", progress="None"), t, NAME, now=10.0)
    assert kinds(evs) == ["cycle_finished"]


def test_finish_after_reconnect_without_seen_start():
    """Bridge was down when the cycle started; first snapshot is mid-cycle."""
    t = CycleTracker()
    detect(None, st(machine_state="Run", progress="Wash", remaining_s=3600), t, NAME)
    evs = detect(st(machine_state="Run", progress="Wash", remaining_s=3600),
                 st(machine_state="End", progress="Finish", remaining_s=0), t, NAME)
    assert kinds(evs) == ["cycle_finished"]
    assert "Cycle was about 1h" in evs[0].message


def test_phase_changes_only_while_running():
    t = CycleTracker()
    evs = detect(st(machine_state="Run", progress="Wash"),
                 st(machine_state="Run", progress="Rinse", remaining_s=1200,
                    progress_pct=60), t, NAME)
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
    assert "items: {code=4C, alarmType=Water}" in evs[0].message
    assert detect(st(alarms=alarm), st(), t, NAME) == []


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
    evs = detect(st(machine_state="Run", progress="Wash", remaining_s=3600),
                 st(machine_state="Run", progress="Wash", remaining_s=3540), t, NAME)
    assert evs == []


def test_fmt_duration():
    assert fmt_duration(None) is None
    assert fmt_duration(0) == "0m"
    assert fmt_duration(59) == "0m"
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
