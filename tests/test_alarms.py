from datetime import UTC, timedelta, timezone

from smartthings_pushover.alarms import AlarmThrottle, alarm_fields, codes_in, throttle_key

REAL_DOOR_ALARM = (
    ("items", "{id=0, description=Alarm, alarmType=Device, code=ErrorCode_DC, "
              "triggeredTime=2026-09-10T12:40:48, state=Created}"),
)


def test_codes_in_extracts_and_dedupes():
    pairs = (("count", "2"), ("items", "{code=4C, alarmType=Water}, {code=dc, x=1}, {code=4C}"))
    assert codes_in(pairs) == ["4C", "DC"]
    assert codes_in(()) == []


def test_real_shape_with_errorcode_prefix():
    assert codes_in(REAL_DOOR_ALARM) == ["DC"]
    assert alarm_fields(REAL_DOOR_ALARM, UTC) == (
        ("Status", "Error"),
        ("Code", "DC"),
        ("Meaning", "Door open or not latched"),
        ("Raised", "12:40"),
    )
    # The appliance stamps UTC; Raised is shown in the configured zone.
    assert dict(alarm_fields(REAL_DOOR_ALARM, timezone(timedelta(hours=1))))["Raised"] == "13:40"


def test_throttle_key_ignores_volatile_fields():
    again = (("items", REAL_DOOR_ALARM[0][1].replace("12:40:48", "12:40:49")),)
    assert throttle_key(REAL_DOOR_ALARM) == throttle_key(again) == "DC"
    assert throttle_key((("count", "1"),)) == "count: 1"


def test_unknown_code_and_no_code():
    assert alarm_fields((("items", "{code=ZZ9}"),)) == (
        ("Status", "Error"),
        ("Code", "ZZ9"),
        ("Meaning", "Not in the code table; check the panel"),
    )
    assert alarm_fields((("count", "1"),)) == (("Status", "Error"), ("Details", "count: 1"))


def test_throttle_suppresses_repeats_and_storms():
    t = AlarmThrottle(repeat_window_s=600, max_per_window=3)
    assert t.allow("A", 0)[0]
    ok, reason = t.allow("A", 10)
    assert not ok and "same alarm" in reason
    assert t.allow("B", 20)[0]
    assert t.allow("C", 30)[0]
    ok, reason = t.allow("D", 40)
    assert not ok and "3 alarms" in reason
    assert t.allow("A", 700)[0]
    assert t.allow("D", 701)[0]


def test_throttle_disabled():
    t = AlarmThrottle(repeat_window_s=0)
    assert t.allow("A", 0)[0] and t.allow("A", 0)[0]
