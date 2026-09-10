from smartthings_pushover.appliance import ApplianceState
from smartthings_pushover.events import CycleTracker, Event, detect, fields, fmt_duration

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


def f(ev):
    """Fields as a dict for terse assertions."""
    return dict(ev.fields)


# ---- rendering ---------------------------------------------------------------
def test_fields_drop_empty_values_and_render():
    ev = Event("phase_changed", "W", fields(("Status", "Spinning"), ("Phase", None),
                                          ("Time Remaining", ""), ("Percentage Complete", "31%")))
    assert ev.fields == (("Status", "Spinning"), ("Percentage Complete", "31%"))
    assert ev.message == "Status: Spinning\nPercentage Complete: 31%"
    assert ev.html() == "<b>Status:</b> Spinning\n<b>Percentage Complete:</b> 31%"


def test_html_escapes_values():
    ev = Event("alarm", "W", fields(("Programme", "Wool <Delicates> & Silk")))
    assert ev.html() == "<b>Programme:</b> Wool &lt;Delicates&gt; &amp; Silk"


# ---- baseline ----------------------------------------------------------------
def test_first_snapshot_is_baseline_only():
    t = CycleTracker()
    assert detect(None, st(machine_state="Run", progress="Wash"), t, NAME) == []
    # joined mid-cycle: remembers the course for the eventual finish
    assert t.course == "1C"
    assert t.started_at is None


def test_baseline_from_partial_tree_then_full_tree_is_still_silent():
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
    assert f(evs[0])["Duration"] == "1h 15m"


def test_baseline_rejects_persisted_tracker_for_a_different_cycle():
    t = CycleTracker(started_at=100.0, course="1B", initial_remaining_s=5000)
    detect(None, st(machine_state="Run", progress="Wash", remaining_s=9000), t, NAME)
    assert t.started_at is None and t.course == "1C"
    t = CycleTracker(started_at=100.0, course="1C", initial_remaining_s=5000)
    detect(None, st(machine_state="Run", progress="Wash", remaining_s=9000), t, NAME)
    assert t.started_at is None


def test_no_change_no_events():
    t = CycleTracker()
    assert detect(st(), st(), t, NAME) == []


# ---- cycle -------------------------------------------------------------------
def test_cycle_started_fields():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    evs = detect(
        st(),
        st(machine_state="Run", progress="Weightsensing", remaining_s=4200),
        t, NAME, now=1000.0,
    )
    assert kinds(evs) == ["cycle_started"]
    assert evs[0].fields == (
        ("Status", "Started"),
        ("Programme", "Eco 40-60"),
        ("Temperature", "40°"),
        ("Spin", "1400 rpm"),
        ("Rinses", "2"),
        ("Time Remaining", "1h 10m"),
    )
    assert evs[0].message == (
        "Status: Started\nProgramme: Eco 40-60\nTemperature: 40°\nSpin: 1400 rpm\n"
        "Rinses: 2\nTime Remaining: 1h 10m"
    )
    assert t.started_at == 1000.0


def test_unknown_course_shows_code():
    t = CycleTracker()
    evs = detect(st(), st(machine_state="Run"), t, NAME)
    assert f(evs[0])["Programme"] == "Course 1C"


def test_none_settings_are_omitted():
    t = CycleTracker()
    evs = detect(st(), st(machine_state="Run", water_temp="None", spin="None", rinse="1"), t, NAME)
    assert "Temperature" not in f(evs[0]) and "Spin" not in f(evs[0])
    assert f(evs[0])["Rinses"] == "1"


def test_pause_resume():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME)
    evs = detect(
        st(machine_state="Run", progress="Wash"),
        st(machine_state="Pause", progress="Wash", remaining_s=1800), t, NAME,
    )
    assert kinds(evs) == ["cycle_paused"]
    assert evs[0].fields == (("Status", "Paused"), ("Phase", "Washing"), ("Time Remaining", "30m"))
    evs = detect(
        st(machine_state="Pause", progress="Wash"),
        st(machine_state="Run", progress="Wash"), t, NAME,
    )
    assert kinds(evs) == ["cycle_resumed"]
    assert f(evs[0])["Status"] == "Resumed" and f(evs[0])["Phase"] == "Washing"
    assert t.started_at is not None


def test_finished_via_end_state_reports_duration():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(
        st(machine_state="Run", progress="Spin", remaining_s=60),
        st(machine_state="End", progress="Finish", remaining_s=0),
        t, NAME, now=4500.0,
    )
    assert kinds(evs) == ["cycle_finished"]
    assert evs[0].fields == (
        ("Status", "Complete"), ("Programme", "Eco 40-60"), ("Duration", "1h 15m"))
    assert t.started_at is None


def test_finished_via_progress_finish_while_state_still_run():
    t = CycleTracker()
    detect(st(), st(machine_state="Run", progress="Wash"), t, NAME, now=0.0)
    evs = detect(
        st(machine_state="Run", progress="Spin"),
        st(machine_state="Run", progress="Finish"), t, NAME, now=100.0,
    )
    assert kinds(evs) == ["cycle_finished"]
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
    evs = detect(
        st(machine_state="End", progress="Finish"),
        st(machine_state="Ready", progress="None"), t, NAME,
    )
    assert evs == []
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
    assert evs[0].fields == (
        ("Status", "Cancelled"), ("Programme", "Course 1C"),
        ("Phase", "Rinsing"), ("Time Remaining", "25m"))

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
    assert evs[0].fields == (
        ("Status", "Complete"), ("Programme", "Eco 40-60"),
        ("Note", "Finished while the bridge was disconnected"))
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
    assert f(evs[0])["Cycle Length"] == "About 1h"
    assert "Duration" not in f(evs[0])


def test_phase_changes_only_while_running():
    t = CycleTracker()
    evs = detect(
        st(machine_state="Run", progress="Wash"),
        st(machine_state="Run", progress="Spin", remaining_s=840, progress_pct=31), t, NAME,
    )
    assert kinds(evs) == ["phase_changed"]
    assert evs[0].message == "Status: Spinning\nTime Remaining: 14m\nPercentage Complete: 31%"
    evs = detect(st(), st(machine_state="Run", progress="Wash"), t, NAME)
    assert kinds(evs) == ["cycle_started"]


def test_unknown_phase_uses_raw_value():
    t = CycleTracker()
    evs = detect(st(machine_state="Run", progress="Wash"),
                 st(machine_state="Run", progress="Steam", remaining_s=600), t, NAME)
    assert f(evs[0])["Status"] == "Steam"


# ---- alarms ------------------------------------------------------------------
def test_alarm_raised_then_cleared():
    t = CycleTracker()
    alarm = (("items", "{code=4C, alarmType=Water}"),)
    evs = detect(st(), st(alarms=alarm), t, NAME)
    assert kinds(evs) == ["alarm"]
    assert evs[0].title == "Washer: Error"
    assert evs[0].fields == (
        ("Status", "Error"), ("Code", "4C"),
        ("Meaning", "Water supply problem: check the tap and inlet hose"))
    assert evs[0].dedupe_key == "4C"
    assert detect(st(alarms=alarm), st(), t, NAME) == []


def test_alarm_real_door_open_shape():
    """Verbatim flattened shape captured from the WW80 on 2026-09-10."""
    t = CycleTracker()
    alarm = (("items", "{id=0, description=Alarm, alarmType=Device, code=ErrorCode_DC, "
                       "triggeredTime=2026-09-10T12:40:48, state=Created}"),)
    evs = detect(st(), st(alarms=alarm), t, NAME)
    assert evs[0].fields == (
        ("Status", "Error"), ("Code", "DC"), ("Meaning", "Door open or not latched"),
        ("Raised", "12:40:48 UTC"))
    assert evs[0].dedupe_key == "DC"


# ---- toggles -----------------------------------------------------------------
def test_power_and_toggles():
    t = CycleTracker()
    evs = detect(st(power="Off"), st(power="On"), t, NAME)
    assert kinds(evs) == ["power_on"] and evs[0].message == "Status: Powered On"
    assert kinds(detect(st(power="On"), st(power="Off"), t, NAME)) == ["power_off"]
    evs = detect(st(), st(remote_control=True), t, NAME)
    assert kinds(evs) == ["remote_control"] and evs[0].message == "Remote Control: Enabled"
    evs = detect(st(), st(child_lock=True), t, NAME)
    assert kinds(evs) == ["child_lock"] and evs[0].message == "Child Lock: On"
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
    assert fmt_duration(59) == "under 1m"
    assert fmt_duration(60) == "1m"
    assert fmt_duration(3600) == "1h"
    assert fmt_duration(3600 + 5 * 60) == "1h 05m"


# ---- dryer -------------------------------------------------------------------
def test_dryer_fields():
    t = CycleTracker(course_names={"16": "Cotton"}, kind="dryer")
    idle = st(course="16", water_temp=None, spin=None, rinse=None,
              dry_level="Normal", dry_time_s=0, remaining_s=7200)
    running = st(course="16", machine_state="Run", progress="Drying",
                 water_temp=None, spin=None, rinse=None, dry_level="Normal",
                 dry_time_s=0, remaining_s=7200)
    detect(None, idle, t, "Dryer", now=0)
    evs = detect(idle, running, t, "Dryer", now=0)
    assert evs[0].fields == (
        ("Status", "Started"), ("Programme", "Cotton"), ("Dry Level", "Normal"),
        ("Time Remaining", "2h"))
    cooling = st(course="16", machine_state="Run", progress="Cooling",
                 water_temp=None, spin=None, rinse=None, remaining_s=300)
    evs = detect(running, cooling, t, "Dryer", now=100)
    assert kinds(evs) == ["phase_changed"] and f(evs[0])["Status"] == "Cooling"
    done = st(course="16", machine_state="End", progress="Finish",
              water_temp=None, spin=None, rinse=None, remaining_s=0)
    evs = detect(cooling, done, t, "Dryer", now=7300)
    assert evs[0].fields == (("Status", "Complete"), ("Programme", "Cotton"), ("Duration", "2h 01m"))


def test_timed_dry_fields():
    t = CycleTracker(kind="dryer")
    idle = st(course="27", water_temp=None, spin=None, rinse=None,
              dry_level="None", dry_time_s=5400)
    running = st(course="27", machine_state="Run", progress="Drying",
                 water_temp=None, spin=None, rinse=None, dry_level="None",
                 dry_time_s=5400)
    detect(None, idle, t, "Dryer", now=0)
    evs = detect(idle, running, t, "Dryer", now=0)
    assert f(evs[0])["Dry Time"] == "1h 30m" and "Dry Level" not in f(evs[0])


# ---- Delay End (shapes captured on the WW80, 2026-09-10) -------------------
def test_delay_end_scheduled_then_started():
    t = CycleTracker(course_names={"1C": "Eco 40-60"})
    armed = st(remaining_s=4 * 3600, delay_end_s=4 * 3600)
    assert detect(st(), armed, t, NAME, now=0.0) == []
    waiting = st(machine_state="Run", progress="None", remaining_s=4 * 3600,
                 delay_end_s=4 * 3600)
    evs = detect(armed, waiting, t, NAME, now=0.0)
    assert kinds(evs) == ["cycle_scheduled"]
    assert evs[0].fields == (
        ("Status", "Delayed Start Armed"), ("Programme", "Eco 40-60"),
        ("Temperature", "40°"), ("Spin", "1400 rpm"), ("Rinses", "2"), ("Finishes In", "4h"))
    assert t.scheduled and t.started_at is None
    later = st(machine_state="Run", progress="None", remaining_s=3 * 3600, delay_end_s=3 * 3600)
    assert detect(waiting, later, t, NAME, now=3600.0) == []
    started = st(machine_state="Run", progress="Weightsensing", remaining_s=9360,
                 delay_end_s=9360)
    evs = detect(later, started, t, NAME, now=5040.0)
    assert kinds(evs) == ["cycle_started"]
    assert not t.scheduled and t.started_at == 5040.0
    done = st(machine_state="End", progress="Finish", remaining_s=0)
    evs = detect(started, done, t, NAME, now=5040.0 + 9300)
    assert f(evs[0])["Duration"] == "2h 35m"


def test_delay_end_cancelled():
    t = CycleTracker()
    waiting = st(machine_state="Run", progress="None", remaining_s=4 * 3600, delay_end_s=4 * 3600)
    detect(st(), waiting, t, NAME, now=0.0)
    evs = detect(waiting, st(), t, NAME, now=60.0)
    assert kinds(evs) == ["cycle_cancelled"]
    assert evs[0].fields == (("Status", "Delayed Start Cancelled"), ("Programme", "Course 1C"))
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
    t = CycleTracker()
    door_open = st(machine_state="Pause", progress="None")
    assert detect(st(), door_open, t, NAME, now=0.0) == []
    evs = detect(door_open, st(machine_state="Run", progress="None", remaining_s=1200),
                 t, NAME, now=5.0)
    assert kinds(evs) == ["cycle_started"]
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
