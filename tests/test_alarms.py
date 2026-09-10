from smartthings_pushover.alarms import AlarmThrottle, codes_in, describe, throttle_key


def test_codes_in_extracts_and_dedupes():
    pairs = (("count", "2"), ("items", "{code=4C, alarmType=Water}, {code=dc, x=1}, {code=4C}"))
    assert codes_in(pairs) == ["4C", "DC"]
    assert codes_in(()) == []


REAL_DOOR_ALARM = (
    ("items", "{id=0, description=Alarm, alarmType=Device, code=ErrorCode_DC, "
              "triggeredTime=2026-09-10T12:40:48, state=Created}"),
)


def test_real_shape_with_errorcode_prefix():
    assert codes_in(REAL_DOOR_ALARM) == ["DC"]
    assert describe(REAL_DOOR_ALARM).startswith("Error DC: door open or not latched")


def test_throttle_key_ignores_volatile_fields():
    again = (("items", REAL_DOOR_ALARM[0][1].replace("12:40:48", "12:40:49")),)
    assert throttle_key(REAL_DOOR_ALARM) == throttle_key(again) == "DC"
    # No code at all: fall back to the full text.
    assert throttle_key((("count", "1"),)) == "count: 1"


def test_describe_known_and_unknown_codes():
    text = describe((("items", "{code=4C, alarmType=Water}"),))
    assert text.splitlines()[0] == "Error 4C: water supply problem: check the tap and inlet hose"
    assert "items: {code=4C, alarmType=Water}" in text
    text = describe((("items", "{code=ZZ9}"),))
    assert text.startswith("Error ZZ9 (see the panel)")
    text = describe((("count", "1"),))
    assert text.startswith("Appliance reports a problem")


def test_throttle_suppresses_repeats_and_storms():
    t = AlarmThrottle(repeat_window_s=600, max_per_window=3)
    assert t.allow("A", 0)[0]
    ok, reason = t.allow("A", 10)
    assert not ok and "same alarm" in reason
    assert t.allow("B", 20)[0]
    assert t.allow("C", 30)[0]
    ok, reason = t.allow("D", 40)
    assert not ok and "3 alarms" in reason
    # Window expires: everything is allowed again.
    assert t.allow("A", 700)[0]
    assert t.allow("D", 701)[0]


def test_throttle_disabled():
    t = AlarmThrottle(repeat_window_s=0)
    assert t.allow("A", 0)[0] and t.allow("A", 0)[0]
