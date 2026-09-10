import os
from datetime import datetime

import pytest

from smartthings_pushover.config import (
    ALL_EVENTS,
    DEFAULT_EVENTS,
    Config,
    ConfigError,
    in_quiet_hours,
    parse_course_names,
    parse_event_priorities,
    parse_events,
    parse_quiet_hours,
)

REQUIRED = {"WASHER_IP": "192.168.1.50", "PUSHOVER_TOKEN": "t", "PUSHOVER_USER": "u"}
PREFIXES = ("WASHER_", "DRYER_", "PUSHOVER_", "EVENTS",
            "EVENT_PRIORITIES", "QUIET_", "STARTUP_NOTIFY", "OFFLINE_AFTER_S", "HEALTH_INTERVAL_S",
            "PING_INTERVAL_S", "HOT_POLL", "LOG_LEVEL", "CERT_PATH", "KEY_PATH", "STATE_DIR",
            "HEARTBEAT_PATH", "ALARM_REPEAT_S")


@pytest.fixture
def env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(PREFIXES):
            monkeypatch.delenv(k, raising=False)
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


def test_defaults(env):
    cfg = Config.from_env()
    assert [a.kind for a in cfg.appliances] == ["washer"]
    w = cfg.appliance("washer")
    assert w.ip == "192.168.1.50" and w.port is None
    assert w.name == "Washing machine"
    assert w.dtls_local_port == 49700
    assert cfg.appliance("dryer") is None
    assert cfg.events == frozenset(DEFAULT_EVENTS)
    assert "startup" not in cfg.events
    assert cfg.cert_path == "/config/client_fullchain.pem"
    assert cfg.pushover_priority == 0 and cfg.pushover_alarm_priority == 1
    assert cfg.quiet_hours is None and cfg.event_priorities == {}
    assert cfg.state_dir is None and cfg.heartbeat_path is None


def test_blank_port_means_auto(env):
    env.setenv("WASHER_PORT", "")
    assert Config.from_env().appliance("washer").port is None
    env.setenv("WASHER_PORT", "49154")
    assert Config.from_env().appliance("washer").port == 49154
    env.setenv("WASHER_PORT", "70000")
    with pytest.raises(ConfigError, match="WASHER_PORT out of range"):
        Config.from_env()


def test_dryer_enabled_by_ip(env):
    env.setenv("DRYER_IP", "192.168.1.51")
    env.setenv("DRYER_COURSE_NAMES", "16=Cotton")
    cfg = Config.from_env()
    assert [a.kind for a in cfg.appliances] == ["washer", "dryer"]
    d = cfg.appliance("dryer")
    assert d.name == "Tumble dryer"
    assert d.dtls_local_port == 49701
    assert d.course_names == {"16": "Cotton"}
    assert d.spec.phase_labels["Cooling"] == "Cooling"


def test_enabled_flags(env):
    env.setenv("DRYER_IP", "192.168.1.51")
    env.setenv("WASHER_ENABLED", "false")
    cfg = Config.from_env()
    assert [a.kind for a in cfg.appliances] == ["dryer"]
    env.setenv("DRYER_ENABLED", "false")
    with pytest.raises(ConfigError, match="no appliance configured"):
        Config.from_env()
    env.delenv("DRYER_IP")
    env.setenv("DRYER_ENABLED", "true")
    with pytest.raises(ConfigError, match="DRYER_IP"):
        Config.from_env()


def test_missing_required(env):
    env.delenv("WASHER_IP")
    with pytest.raises(ConfigError, match="no appliance configured"):
        Config.from_env()
    env.setenv("WASHER_IP", "1.2.3.4")
    env.delenv("PUSHOVER_TOKEN")
    with pytest.raises(ConfigError, match="PUSHOVER_TOKEN"):
        Config.from_env()


def test_secrets_from_files(env, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("  file-token\n")
    env.delenv("PUSHOVER_TOKEN")
    env.setenv("PUSHOVER_TOKEN_FILE", str(token_file))
    assert Config.from_env().pushover_token == "file-token"
    # A direct value wins over the file.
    env.setenv("PUSHOVER_TOKEN", "direct")
    assert Config.from_env().pushover_token == "direct"
    env.delenv("PUSHOVER_TOKEN")
    env.setenv("PUSHOVER_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="PUSHOVER_TOKEN_FILE"):
        Config.from_env()


def test_local_ports_must_differ(env):
    env.setenv("DRYER_IP", "192.168.1.51")
    env.setenv("DRYER_DTLS_LOCAL_PORT", "49700")
    with pytest.raises(ConfigError, match="must differ"):
        Config.from_env()
    env.setenv("DRYER_DTLS_LOCAL_PORT", "random")
    assert Config.from_env().appliance("dryer").dtls_local_port is None
    env.setenv("DRYER_DTLS_LOCAL_PORT", "99999")
    with pytest.raises(ConfigError, match="out of range"):
        Config.from_env()


def test_priority_range(env):
    env.setenv("PUSHOVER_PRIORITY", "2")
    with pytest.raises(ConfigError, match="PUSHOVER_PRIORITY"):
        Config.from_env()


@pytest.mark.parametrize(
    "var,value",
    [
        ("HOT_POLL_S", "0"),
        ("HOT_POLL_ACTIVE_S", "0.1"),
        ("PING_INTERVAL_S", "1"),
        ("HEALTH_INTERVAL_S", "5"),
        ("OFFLINE_AFTER_S", "-1"),
        ("OFFLINE_AFTER_S", "nan"),
        ("ALARM_REPEAT_S", "-5"),
    ],
)
def test_tuning_minimums(env, var, value):
    env.setenv(var, value)
    with pytest.raises(ConfigError, match=var):
        Config.from_env()


def test_log_level_whitelist(env):
    env.setenv("LOG_LEVEL", "warning")
    assert Config.from_env().log_level == "WARNING"
    env.setenv("LOG_LEVEL", "disable")
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Config.from_env()


def test_startup_notify_adds_event(env):
    env.setenv("STARTUP_NOTIFY", "true")
    assert "startup" in Config.from_env().events
    env.setenv("STARTUP_NOTIFY", "false")
    env.setenv("EVENTS", "startup,cycle_finished")
    assert Config.from_env().events == {"startup", "cycle_finished"}


def test_parse_events():
    assert parse_events(None) == frozenset(DEFAULT_EVENTS)
    assert parse_events("all") == frozenset(ALL_EVENTS)
    assert parse_events(" cycle_finished, ALARM ,") == {"cycle_finished", "alarm"}
    with pytest.raises(ConfigError, match="unknown event"):
        parse_events("cycle_finished,door_open")


def test_parse_course_names():
    assert parse_course_names(None) == {}
    assert parse_course_names("1c=Eco 40-60, Course_1B = Cotton") == {
        "1C": "Eco 40-60",
        "1B": "Cotton",
    }
    with pytest.raises(ConfigError, match="DRYER_COURSE_NAMES"):
        parse_course_names("Cotton", "DRYER_COURSE_NAMES")
    with pytest.raises(ConfigError, match="hex"):
        parse_course_names("Cotton=Cotton")


def test_parse_event_priorities():
    assert dict(parse_event_priorities(None)) == {}
    assert dict(parse_event_priorities("cycle_finished=1, phase_changed=-1")) == {
        "cycle_finished": 1,
        "phase_changed": -1,
    }
    with pytest.raises(ConfigError, match="unknown event"):
        parse_event_priorities("door=1")
    with pytest.raises(ConfigError, match="between"):
        parse_event_priorities("alarm=2")
    with pytest.raises(ConfigError, match="must look like"):
        parse_event_priorities("alarm=high")


def test_parse_quiet_hours():
    assert parse_quiet_hours(None) is None
    assert parse_quiet_hours("22:00-07:00") == (22 * 60, 7 * 60)
    assert parse_quiet_hours("23-6:30") == (23 * 60, 6 * 60 + 30)
    assert parse_quiet_hours("08:00-08:00") is None
    for bad in ("22:00", "25:00-07:00", "22:60-07:00", "night-day"):
        with pytest.raises(ConfigError, match="QUIET_HOURS"):
            parse_quiet_hours(bad)


def test_in_quiet_hours_wraps_midnight():
    window = (22 * 60, 7 * 60)
    assert in_quiet_hours(window, datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours(window, datetime(2026, 1, 1, 3, 0))
    assert in_quiet_hours(window, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours(window, datetime(2026, 1, 1, 7, 0))
    assert not in_quiet_hours(window, datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours(None, datetime(2026, 1, 1, 3, 0))
    daytime = (9 * 60, 17 * 60)
    assert in_quiet_hours(daytime, datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours(daytime, datetime(2026, 1, 1, 20, 0))


def test_priority_resolution(env):
    env.setenv("QUIET_HOURS", "22:00-07:00")
    env.setenv("QUIET_PRIORITY", "-1")
    env.setenv("EVENT_PRIORITIES", "cycle_finished=1")
    cfg = Config.from_env()
    day = datetime(2026, 1, 1, 12, 0)
    night = datetime(2026, 1, 1, 23, 0)
    assert cfg.priority_for("cycle_started", None, day) == 0
    assert cfg.priority_for("cycle_started", None, night) == -1
    assert cfg.priority_for("cycle_finished", None, day) == 1  # override
    assert cfg.priority_for("cycle_finished", None, night) == -1  # quiet caps it
    assert cfg.priority_for("alarm", None, night) == 1  # alarms are exempt
    assert cfg.priority_for("phase_changed", -2, day) == -2  # event hint honoured
