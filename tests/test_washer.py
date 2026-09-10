from smartthings_pushover import washer

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
    s = washer.flatten(IDLE_LINKS)
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
    s = washer.flatten(links)
    assert s.running and s.in_cycle and not s.finished
    assert washer.is_active(links)
    assert not washer.is_active(IDLE_LINKS)


def test_flatten_tolerates_missing_and_garbage():
    s = washer.flatten({})
    assert s == washer.WasherState()
    s = washer.flatten({"/operational/state/vs/0": {
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
    s = washer.flatten(links)
    assert s.alarms == (("count", "1"),
                        ("items", "{code=4C, alarmType=Water}"))


def test_course_code_shapes():
    assert washer.course_code("Table_02_Course_1C") == "1C"
    assert washer.course_code("Course_1b") == "1B"
    assert washer.course_code("Cotton") == "Cotton"
    assert washer.course_code(None) is None
    assert washer.course_code("") is None


def test_poll_tiers_use_vendor_paths_only():
    tiers = washer.poll_tiers()
    for t in tiers:
        for p in t.paths:
            assert p == washer.SEED_PATH or p[-2:] == ("vs", "0"), p
    for p in washer.OBSERVE_PATHS:
        assert p[-2:] == ("vs", "0"), p


# Verbatim from /course/vs/0 on the WW80CGC04DAEEU (Table_02, 14 courses).
SUPPORTED_OPTIONS_BLOB = (
    "31C8410923FA67F1B847E923FA67F25843E933FA57F20857E943FA67F088000913FA67F"
    "7485209204A5208780009000A00006841E930FA30F7F841E920FA30F65841E943FA57F"
    "8F8102923FA57F96841E920FA37F34841E923FA67FA0811E933FA33F")


def test_supported_course_codes():
    links = {"/course/vs/0": {
        "x.com.samsung.da.supportedOptions": [SUPPORTED_OPTIONS_BLOB]}}
    assert washer.supported_course_codes(links) == [
        "1C", "1B", "25", "20", "08", "74", "87", "06", "7F", "65", "8F",
        "96", "34", "A0"]
    # plain string works too; header digit is the per-record field count
    assert washer.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions":
                          SUPPORTED_OPTIONS_BLOB[:15]}}) == ["1C"]
    assert washer.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions":
                          "1" + "1C8410" + "1B8000"}}) == ["1C", "1B"]


def test_supported_course_codes_bad_shapes():
    assert washer.supported_course_codes({}) == []
    assert washer.supported_course_codes({"/course/vs/0": {}}) == []
    assert washer.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions": ["zz"]}}) == []
    assert washer.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions": ["31C8410923FA6"]}}) == []
    assert washer.supported_course_codes(
        {"/course/vs/0": {"x.com.samsung.da.supportedOptions": ["0"]}}) == []
