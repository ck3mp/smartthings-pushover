from smartthings_pushover import appliance

# Trimmed from a real /device/0 read of a WW80CGC04DAEEU (DA_WM_TP1_21_COMMON).
IDLE_LINKS = {
    "/power/vs/0": {"x.com.samsung.da.power": "Off"},
    "/operational/state/vs/0": {
        "x.com.samsung.da.state": "Ready",
        "x.com.samsung.da.remainingTime": "02:36:00",
        "x.com.samsung.da.progressPercentage": "1",
        "x.com.samsung.da.progress": "None",
        "x.com.samsung.da.delayEndTime": "00:00:00",
    },
    "/washer/vs/0": {
        "x.com.samsung.da.waterTemperature": "40",
        "x.com.samsung.da.spinLevel": "1400",
        "x.com.samsung.da.rinseCycles": "2",
    },
    "/st/washercourse/vs/0": {
        "x.com.samsung.da.st.washerMode": "Table_02_Course_1C",
        "x.com.samsung.da.st.courseTable": "Table_02",
    },
    "/remotectrl/vs/0": {"x.com.samsung.da.remoteControlEnabled": "false"},
    "/kidslock/vs/0": {"x.com.samsung.da.kidsLock": "Ready"},
    "/alarms/vs/0": {},
    "/wm/jobbeginingstatus/vs/0": {"x.com.samsung.da.currentStatus": "None"},
    "/energy/consumption/vs/0": {"x.com.samsung.da.cumulativePower": "113300"},
}


def with_state(links, state, progress="None", remaining="02:36:00", **extra):
    links = {k: dict(v) for k, v in links.items()}
    op = links["/operational/state/vs/0"]
    op["x.com.samsung.da.state"] = state
    op["x.com.samsung.da.progress"] = progress
    op["x.com.samsung.da.remainingTime"] = remaining
    for href, rep in extra.items():
        links[href] = rep
    return links


def test_flatten_idle():
    s = appliance.flatten(IDLE_LINKS)
    assert s.power == "Off"
    assert s.machine_state == "Ready"
    assert s.progress == "None"
    assert s.progress_pct == 1
    assert s.remaining_s == 2 * 3600 + 36 * 60
    assert s.remaining_hms() == "02:36:00"
    assert s.delay_end_s == 0
    assert s.course == "1C"
    assert s.water_temp == "40" and s.spin == "1400" and s.rinse == "2"
    assert s.remote_control is False
    assert s.child_lock is False
    assert s.alarms == ()
    assert s.energy_wh == 113300
    assert not s.running and not s.paused and not s.in_cycle and not s.finished


def test_flatten_running_and_active_hook():
    links = with_state(IDLE_LINKS, "Run", "Wash", "01:10:00")
    s = appliance.flatten(links)
    assert s.running and s.in_cycle and not s.finished
    assert appliance.is_active(links)
    assert not appliance.is_active(IDLE_LINKS)


def test_flatten_tolerates_missing_and_garbage():
    s = appliance.flatten({})
    assert s == appliance.ApplianceState()
    s = appliance.flatten({"/operational/state/vs/0": {
        "x.com.samsung.da.progressPercentage": "abc",
        "x.com.samsung.da.remainingTime": "soon"}})
    assert s.progress_pct is None and s.remaining_s is None


def test_alarms_flatten_sorted_and_stripped():
    links = dict(IDLE_LINKS)
    links["/alarms/vs/0"] = {
        "x.com.samsung.da.items": [{"x.com.samsung.da.code": "4C",
                                    "x.com.samsung.da.alarmType": "Water"}],
        "x.com.samsung.da.count": 1,
    }
    s = appliance.flatten(links)
    assert s.alarms == (("count", "1"),
                        ("items", "{code=4C, alarmType=Water}"))


def test_course_code_shapes():
    assert appliance.course_code("Table_02_Course_1C") == "1C"
    assert appliance.course_code("Course_1b") == "1B"
    assert appliance.course_code("Cotton") == "Cotton"
    assert appliance.course_code(None) is None
    assert appliance.course_code("") is None


def test_poll_tiers_use_vendor_paths_only():
    tiers = appliance.poll_tiers("washer")
    for t in tiers:
        for p in t.paths:
            assert p == appliance.SEED_PATH or p[-2:] == ("vs", "0"), p
    for p in appliance.observe_paths("washer"):
        assert p[-2:] == ("vs", "0"), p


# Verbatim from /course/vs/0 on the WW80CGC04DAEEU (Table_02, 14 courses).
SUPPORTED_OPTIONS_BLOB = (
    "31C8410923FA67F1B847E923FA67F25843E933FA57F20857E943FA67F088000913FA67F"
    "7485209204A5208780009000A00006841E930FA30F7F841E920FA30F65841E943FA57F"
    "8F8102923FA57F96841E920FA37F34841E923FA67FA0811E933FA33F")


def test_supported_course_codes():
    links = {"/course/vs/0": {
        "x.com.samsung.da.supportedOptions": [SUPPORTED_OPTIONS_BLOB]}}
    assert appliance.supported_course_codes(links) == [
        "1C", "1B", "25", "20", "08", "74", "87", "06", "7F", "65", "8F",
        "96", "34", "A0"]
    # plain string works too; header digit is the per-record field count
    assert appliance.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions":
                          SUPPORTED_OPTIONS_BLOB[:15]}}) == ["1C"]
    assert appliance.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions":
                          "1" + "1C8410" + "1B8000"}}) == ["1C", "1B"]


def test_supported_course_codes_bad_shapes():
    assert appliance.supported_course_codes({}) == []
    assert appliance.supported_course_codes({"/course/vs/0": {}}) == []
    assert appliance.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions": ["zz"]}}) == []
    assert appliance.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions": ["31C8410923FA6"]}}) == []
    assert appliance.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions": ["0"]}}) == []


# Trimmed from a real /device/0 read of a DV80CGC0B0AEEU (DA_WM_TP1_21_COMMON_DV5000C).
DRYER_LINKS = {
    "/power/vs/0": {"x.com.samsung.da.power": "Off"},
    "/operational/state/vs/0": {
        "x.com.samsung.da.state": "Ready",
        "x.com.samsung.da.remainingTime": "03:10:00",
        "x.com.samsung.da.progressPercentage": "1",
        "x.com.samsung.da.progress": "None",
        "x.com.samsung.da.delayEndTime": "00:00:00",
        "x.com.samsung.da.supportedProgress": ["None", "Drying", "Cooling", "Finish"],
    },
    "/washer/vs/0": {
        "x.com.samsung.da.wrinklePrevent": "Off",
        "x.com.samsung.da.dryLevel": "Normal",
        "x.com.samsung.da.dryTime": "00:00:00",
        "x.com.samsung.da.dryerType": "Electricity",
    },
    "/st/dryercourse/vs/0": {
        "x.com.samsung.da.st.dryerMode": "Table_03_Course_16",
        "x.com.samsung.da.st.courseTable": "Table_03",
    },
    "/remotectrl/vs/0": {"x.com.samsung.da.remoteControlEnabled": "false"},
    "/kidslock/vs/0": {"x.com.samsung.da.kidsLock": "Ready"},
    "/alarms/vs/0": {},
    "/wm/jobbeginingstatus/vs/0": {"x.com.samsung.da.currentStatus": "None"},
    # The dryer reports instantaneous power only, no cumulativePower.
    "/energy/consumption/vs/0": {"x.com.samsung.da.instantaneousPower": "-500"},
    "/course/vs/0": {"x.com.samsung.da.supportedOptions": [
        "216D20EE0001FD20EE00019D204E00020D102E0001AD102E0001DD204E0001BD204E00"
        "01ED204E00043D204E00024D000E10E25D000E10E27D000E37E23D000E00018D20EE000"]},
}


def test_flatten_dryer():
    s = appliance.flatten(DRYER_LINKS)
    assert s.course == "16"
    assert s.dry_level == "Normal" and s.dry_time_s == 0
    assert s.wrinkle_prevent is False
    assert s.water_temp is None and s.spin is None and s.rinse is None
    assert s.energy_wh is None
    assert s.remaining_s == 3 * 3600 + 10 * 60
    assert not s.in_cycle


def test_dryer_course_codes():
    assert appliance.supported_course_codes(DRYER_LINKS) == [
        "16", "1F", "19", "20", "1A", "1D", "1B", "1E", "43", "24", "25", "27",
        "23", "18"]


def test_paths_per_kind():
    assert ("st", "dryercourse", "vs", "0") in appliance.observe_paths("dryer")
    assert ("st", "washercourse", "vs", "0") not in appliance.observe_paths("dryer")
    assert ("st", "washercourse", "vs", "0") in appliance.warm_paths("washer")
    for kind in appliance.KINDS:
        for tier in appliance.poll_tiers(kind):
            for path in tier.paths:
                assert path[-2:] == ("vs", "0") or path == appliance.SEED_PATH


def test_to_bool_rejects_garbage():
    assert appliance._to_bool("true") is True and appliance._to_bool("Off") is False
    assert appliance._to_bool("maybe") is None and appliance._to_bool(None) is None


def test_delay_waiting():
    # As captured on the WW80: Run, progress None, remaining == delayEnd.
    S = appliance.ApplianceState
    assert S(machine_state="Run", progress="None", remaining_s=14400, delay_end_s=14400).delay_waiting
    assert S(machine_state="Run", progress="Delaywash", remaining_s=14400, delay_end_s=14400).delay_waiting
    # Normal start: delayEnd 00:00:00.
    assert not S(machine_state="Run", progress="None", remaining_s=1200, delay_end_s=0).delay_waiting
    # Delay armed but not started, or the wait is over and washing has begun.
    assert not S(machine_state="Ready", progress="None", remaining_s=14400, delay_end_s=14400).delay_waiting
    assert not S(machine_state="Run", progress="Wash", remaining_s=9000, delay_end_s=9000).delay_waiting


def test_kind_registry_drives_paths():
    for kind in appliance.KINDS:
        assert appliance.warm_paths(kind)[-1] == appliance.observe_paths(kind)[-2]
