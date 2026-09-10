"""Environment-driven configuration.

Everything is a plain env var so the same settings work for
`docker run -e`, compose `env_file`, and a bare-metal shell.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Events the bridge can emit. Order here is the order shown in --help /
# README; the default set is the "tell me when the laundry is done" subset.
ALL_EVENTS = (
    "cycle_started",
    "cycle_finished",
    "cycle_paused",
    "cycle_resumed",
    "cycle_cancelled",
    "phase_changed",
    "alarm",
    "power_on",
    "power_off",
    "remote_control",
    "child_lock",
    "offline",
    "online",
)
DEFAULT_EVENTS = (
    "cycle_started",
    "cycle_finished",
    "cycle_paused",
    "cycle_cancelled",
    "alarm",
)

# Standard OCF secure port plus the dynamic band Samsung's RT-OCF picks
# from. The washer this was built against listens on 49154.
OCF_PORT_CANDIDATES = (5684,) + tuple(range(49152, 49161))


class ConfigError(ValueError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def parse_events(raw: str | None) -> frozenset[str]:
    if raw is None:
        return frozenset(DEFAULT_EVENTS)
    if raw.strip().lower() == "all":
        return frozenset(ALL_EVENTS)
    out = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item not in ALL_EVENTS:
            raise ConfigError(
                f"EVENTS contains unknown event {item!r}; "
                f"valid: {', '.join(ALL_EVENTS)}")
        out.add(item)
    return frozenset(out)


def parse_course_names(raw: str | None) -> dict[str, str]:
    """`1C=Eco 40-60,1B=Cotton` -> {'1C': 'Eco 40-60', '1B': 'Cotton'}.

    Keys are the hex course code after `_Course_` in the washer's
    `x.com.samsung.da.st.washerMode` string, upper-cased.
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError(
                f"COURSE_NAMES entry {pair!r} must look like 1C=Eco 40-60")
        code, _, name = pair.partition("=")
        code = code.strip().upper()
        if code.startswith("COURSE_"):
            code = code[len("COURSE_"):]
        if not code or not name.strip():
            raise ConfigError(f"COURSE_NAMES entry {pair!r} is incomplete")
        out[code] = name.strip()
    return out


@dataclass(frozen=True)
class Config:
    washer_ip: str
    washer_port: int | None            # None -> probe OCF_PORT_CANDIDATES
    washer_name: str
    cert_path: str
    key_path: str

    pushover_token: str
    pushover_user: str
    pushover_device: str | None
    pushover_sound: str | None
    pushover_priority: int
    pushover_finished_sound: str | None
    pushover_alarm_priority: int

    events: frozenset[str]
    course_names: dict[str, str] = field(default_factory=dict)

    startup_notify: bool = False
    offline_after_s: float = 900.0     # how long unreachable before "offline"
    health_interval_s: float = 300.0
    ping_interval_s: float = 25.0
    dtls_local_port: int | None = 49700
    hot_poll_s: float = 2.0
    hot_poll_active_s: float = 1.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        ip = _env("WASHER_IP")
        if not ip:
            raise ConfigError("WASHER_IP is required")
        token = _env("PUSHOVER_TOKEN")
        user = _env("PUSHOVER_USER")
        if not token or not user:
            raise ConfigError("PUSHOVER_TOKEN and PUSHOVER_USER are required")

        port_raw = _env("WASHER_PORT")
        port = None
        if port_raw is not None:
            port = _env_int("WASHER_PORT", 0)
            if not (1 <= port <= 65535):
                raise ConfigError(f"WASHER_PORT out of range: {port}")

        priority = _env_int("PUSHOVER_PRIORITY", 0)
        alarm_priority = _env_int("PUSHOVER_ALARM_PRIORITY", 1)
        for label, p in (("PUSHOVER_PRIORITY", priority),
                         ("PUSHOVER_ALARM_PRIORITY", alarm_priority)):
            if not (-2 <= p <= 1):
                # Priority 2 (emergency) needs retry/expire params and
                # keeps buzzing until acknowledged; not what a washer
                # notification should do by default.
                raise ConfigError(f"{label} must be between -2 and 1")

        local_port_raw = _env("DTLS_LOCAL_PORT")
        local_port: int | None = 49700
        if local_port_raw is not None:
            if local_port_raw.lower() in ("0", "none", "random"):
                local_port = None
            else:
                local_port = _env_int("DTLS_LOCAL_PORT", 49700)

        return cls(
            washer_ip=ip,
            washer_port=port,
            washer_name=_env("WASHER_NAME", "Washing machine") or "Washing machine",
            cert_path=_env("CERT_PATH", "/config/client_fullchain.pem"),
            key_path=_env("KEY_PATH", "/config/client.key"),
            pushover_token=token,
            pushover_user=user,
            pushover_device=_env("PUSHOVER_DEVICE"),
            pushover_sound=_env("PUSHOVER_SOUND"),
            pushover_priority=priority,
            pushover_finished_sound=_env("PUSHOVER_FINISHED_SOUND"),
            pushover_alarm_priority=alarm_priority,
            events=parse_events(_env("EVENTS")),
            course_names=parse_course_names(_env("COURSE_NAMES")),
            startup_notify=_env_bool("STARTUP_NOTIFY", False),
            offline_after_s=_env_float("OFFLINE_AFTER_S", 900.0),
            health_interval_s=_env_float("HEALTH_INTERVAL_S", 300.0),
            ping_interval_s=_env_float("PING_INTERVAL_S", 25.0),
            dtls_local_port=local_port,
            hot_poll_s=_env_float("HOT_POLL_S", 2.0),
            hot_poll_active_s=_env_float("HOT_POLL_ACTIVE_S", 1.0),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
