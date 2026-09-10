import pytest

from smartthings_pushover.config import (
    ALL_EVENTS, DEFAULT_EVENTS, Config, ConfigError, parse_course_names,
    parse_events,
)

REQUIRED = {"WASHER_IP": "192.168.1.40", "PUSHOVER_TOKEN": "t", "PUSHOVER_USER": "u"}


def test_defaults(monkeypatch):
    for k in list(REQUIRED):
        monkeypatch.setenv(k, REQUIRED[k])
    for k in ("WASHER_PORT", "EVENTS", "COURSE_NAMES", "DTLS_LOCAL_PORT"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env()
    assert cfg.washer_port is None
    assert cfg.events == frozenset(DEFAULT_EVENTS)
    assert cfg.cert_path == "/config/client_fullchain.pem"
    assert cfg.dtls_local_port == 49700
    assert cfg.pushover_priority == 0 and cfg.pushover_alarm_priority == 1


def test_blank_port_means_auto(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WASHER_PORT", "")
    assert Config.from_env().washer_port is None
    monkeypatch.setenv("WASHER_PORT", "49154")
    assert Config.from_env().washer_port == 49154


def test_missing_required(monkeypatch):
    monkeypatch.delenv("WASHER_IP", raising=False)
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("PUSHOVER_USER", "u")
    with pytest.raises(ConfigError, match="WASHER_IP"):
        Config.from_env()


def test_priority_range(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PUSHOVER_PRIORITY", "2")
    with pytest.raises(ConfigError, match="PUSHOVER_PRIORITY"):
        Config.from_env()


def test_random_local_port(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DTLS_LOCAL_PORT", "random")
    assert Config.from_env().dtls_local_port is None


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
    with pytest.raises(ConfigError):
        parse_course_names("Cotton")
