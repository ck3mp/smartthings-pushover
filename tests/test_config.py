import pytest

from smartthings_pushover.config import (
    ALL_EVENTS, DEFAULT_EVENTS, Config, ConfigError, parse_course_names,
    parse_events,
)

REQUIRED = {"WASHER_IP": "192.168.1.40", "PUSHOVER_TOKEN": "t", "PUSHOVER_USER": "u"}
OPTIONAL = ("WASHER_PORT", "WASHER_ENABLED", "WASHER_COURSE_NAMES", "COURSE_NAMES",
            "WASHER_DTLS_LOCAL_PORT", "DTLS_LOCAL_PORT", "DRYER_IP", "DRYER_ENABLED",
            "DRYER_PORT", "DRYER_COURSE_NAMES", "DRYER_DTLS_LOCAL_PORT", "EVENTS")


@pytest.fixture
def env(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    for k in OPTIONAL:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults(env):
    cfg = Config.from_env()
    assert [a.kind for a in cfg.appliances] == ["washer"]
    w = cfg.appliance("washer")
    assert w.ip == "192.168.1.40" and w.port is None
    assert w.name == "Washing machine"
    assert w.dtls_local_port == 49700
    assert cfg.appliance("dryer") is None
    assert cfg.events == frozenset(DEFAULT_EVENTS)
    assert cfg.cert_path == "/config/client_fullchain.pem"
    assert cfg.pushover_priority == 0 and cfg.pushover_alarm_priority == 1


def test_blank_port_means_auto(env):
    env.setenv("WASHER_PORT", "")
    assert Config.from_env().appliance("washer").port is None
    env.setenv("WASHER_PORT", "49154")
    assert Config.from_env().appliance("washer").port == 49154


def test_dryer_enabled_by_ip(env):
    env.setenv("DRYER_IP", "192.168.1.253")
    env.setenv("DRYER_COURSE_NAMES", "16=Cotton")
    cfg = Config.from_env()
    assert [a.kind for a in cfg.appliances] == ["washer", "dryer"]
    d = cfg.appliance("dryer")
    assert d.name == "Tumble dryer"
    assert d.dtls_local_port == 49701
    assert d.course_names == {"16": "Cotton"}


def test_enabled_flags(env):
    env.setenv("DRYER_IP", "192.168.1.253")
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


def test_legacy_washer_vars(env):
    env.setenv("COURSE_NAMES", "1C=Eco 40-60")
    env.setenv("DTLS_LOCAL_PORT", "49710")
    w = Config.from_env().appliance("washer")
    assert w.course_names == {"1C": "Eco 40-60"}
    assert w.dtls_local_port == 49710
    env.setenv("WASHER_COURSE_NAMES", "1B=Cotton")   # new name wins
    assert Config.from_env().appliance("washer").course_names == {"1B": "Cotton"}


def test_local_ports_must_differ(env):
    env.setenv("DRYER_IP", "192.168.1.253")
    env.setenv("DRYER_DTLS_LOCAL_PORT", "49700")
    with pytest.raises(ConfigError, match="must differ"):
        Config.from_env()
    env.setenv("DRYER_DTLS_LOCAL_PORT", "random")
    assert Config.from_env().appliance("dryer").dtls_local_port is None


def test_priority_range(env):
    env.setenv("PUSHOVER_PRIORITY", "2")
    with pytest.raises(ConfigError, match="PUSHOVER_PRIORITY"):
        Config.from_env()


def test_parse_events():
    assert parse_events(None) == frozenset(DEFAULT_EVENTS)
    assert parse_events("all") == frozenset(ALL_EVENTS)
    assert parse_events(" cycle_finished, ALARM ,") == {"cycle_finished", "alarm"}
    with pytest.raises(ConfigError, match="unknown event"):
        parse_events("cycle_finished,door_open")


def test_parse_course_names():
    assert parse_course_names(None) == {}
    assert parse_course_names("1c=Eco 40-60, Course_1B = Cotton") == {
        "1C": "Eco 40-60", "1B": "Cotton"}
    with pytest.raises(ConfigError, match="DRYER_COURSE_NAMES"):
        parse_course_names("Cotton", "DRYER_COURSE_NAMES")
