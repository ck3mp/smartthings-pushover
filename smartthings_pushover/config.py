"""Environment-driven configuration.

Everything is a plain env var so the same settings work for
`docker run -e`, compose `env_file`, and a bare-metal shell. Secrets may
alternatively be supplied as files via `<NAME>_FILE` (Docker secrets).

Each appliance kind in `kinds.KIND_SPECS` is configured by its own
`<KIND>_*` block and switched on with `<KIND>_ENABLED` (default: enabled
when its `_IP` is set). Pushover, event selection, quiet hours,
certificates and tuning are shared.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from types import MappingProxyType
from typing import Literal, get_args

from .kinds import KIND_SPECS, KINDS, KindSpec, spec

# Events the bridge can emit. Order here is the order shown in --help /
# README; the default set is the "tell me when the laundry is done" subset.
EventKind = Literal[
    "cycle_scheduled",
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
    "startup",
]
ALL_EVENTS: tuple[EventKind, ...] = get_args(EventKind)
DEFAULT_EVENTS: tuple[EventKind, ...] = (
    "cycle_scheduled",
    "cycle_started",
    "cycle_finished",
    "cycle_paused",
    "cycle_cancelled",
    "alarm",
)

# Standard OCF secure port plus the dynamic band Samsung's RT-OCF picks
# from. Both appliances this was built against listen on 49154.
OCF_PORT_CANDIDATES = (5684, *range(49152, 49161))

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Pushover priority 2 (emergency) needs retry/expire params and keeps
# buzzing until acknowledged; not something a laundry bridge should send.
PRIORITY_MIN, PRIORITY_MAX = -2, 1

__all__ = [
    "ALL_EVENTS",
    "DEFAULT_EVENTS",
    "KINDS",
    "ApplianceConfig",
    "Config",
    "ConfigError",
    "EventKind",
    "in_quiet_hours",
    "parse_course_names",
    "parse_event_priorities",
    "parse_events",
    "parse_quiet_hours",
]


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------
def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default


def _secret(name: str) -> str | None:
    """`NAME`, or the stripped contents of the file named by `NAME_FILE`."""
    direct = _env(name)
    if direct is not None:
        return direct
    path = _env(f"{name}_FILE")
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError as e:
        raise ConfigError(f"{name}_FILE: cannot read {path!r}: {e.strerror}") from e
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e
    if value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(f"{name} must be a finite number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}, got {raw!r}")
    return value


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


def _env_port(name: str, default: int | None) -> int | None:
    raw = _env(name)
    if raw is None:
        return default
    port = _env_int(name, 0)
    if not (1 <= port <= 65535):
        raise ConfigError(f"{name} out of range: {port}")
    return port


def _env_priority(name: str, default: int) -> int:
    p = _env_int(name, default)
    if not (PRIORITY_MIN <= p <= PRIORITY_MAX):
        raise ConfigError(f"{name} must be between {PRIORITY_MIN} and {PRIORITY_MAX}")
    return p


def _local_port(var: str, default: int) -> int | None:
    raw = _env(var)
    if raw is None:
        return default
    if raw.lower() in ("0", "none", "random"):
        return None
    return _env_port(var, default)


# ---------------------------------------------------------------------------
# parsers (pure; unit-tested directly)
# ---------------------------------------------------------------------------
def parse_events(raw: str | None) -> frozenset[EventKind]:
    if raw is None:
        return frozenset(DEFAULT_EVENTS)
    if raw.strip().lower() == "all":
        return frozenset(ALL_EVENTS)
    out: set[EventKind] = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item not in ALL_EVENTS:
            raise ConfigError(
                f"EVENTS contains unknown event {item!r}; valid: {', '.join(ALL_EVENTS)}"
            )
        out.add(item)
    return frozenset(out)


_HEX_CODE = re.compile(r"[0-9A-F]+")


def parse_course_names(raw: str | None, var: str = "COURSE_NAMES") -> dict[str, str]:
    """`1C=Eco 40-60,1B=Cotton` -> {'1C': 'Eco 40-60', '1B': 'Cotton'}.

    Keys are the hex course code after `_Course_` in the appliance's
    `x.com.samsung.da.st.washerMode` / `dryerMode` string, upper-cased.
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError(f"{var} entry {pair!r} must look like 1C=Eco 40-60")
        code, _, name = pair.partition("=")
        code = code.strip().upper()
        code = code.removeprefix("COURSE_")
        if not code or not name.strip():
            raise ConfigError(f"{var} entry {pair!r} is incomplete")
        if not _HEX_CODE.fullmatch(code):
            raise ConfigError(f"{var} code {code!r} must be a hex course code such as 1C")
        out[code] = name.strip()
    return out


def parse_event_priorities(raw: str | None) -> Mapping[str, int]:
    """`cycle_finished=1,phase_changed=-1` -> {'cycle_finished': 1, ...}."""
    out: dict[str, int] = {}
    if not raw:
        return MappingProxyType(out)
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        kind, _, value = pair.partition("=")
        kind = kind.strip().lower()
        if kind not in ALL_EVENTS:
            raise ConfigError(f"EVENT_PRIORITIES has unknown event {kind!r}")
        try:
            p = int(value.strip())
        except ValueError:
            raise ConfigError(f"EVENT_PRIORITIES entry {pair!r} must look like alarm=1") from None
        if not (PRIORITY_MIN <= p <= PRIORITY_MAX):
            raise ConfigError(
                f"EVENT_PRIORITIES {kind} must be between {PRIORITY_MIN} and {PRIORITY_MAX}"
            )
        out[kind] = p
    return MappingProxyType(out)


_HHMM = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")


def _parse_hhmm(text: str, var: str) -> int:
    m = _HHMM.match(text.strip())
    if not m:
        raise ConfigError(f"{var} times must look like 22:00, got {text!r}")
    h, mm = int(m.group(1)), int(m.group(2) or 0)
    if h > 24 or mm > 59 or (h == 24 and mm != 0):
        raise ConfigError(f"{var} time out of range: {text!r}")
    return (h * 60 + mm) % (24 * 60)


def parse_quiet_hours(raw: str | None, var: str = "QUIET_HOURS") -> tuple[int, int] | None:
    """`22:00-07:00` -> (1320, 420) minutes after midnight; None when unset.

    The window may wrap past midnight. Start == end means "never".
    """
    if raw is None:
        return None
    if "-" not in raw:
        raise ConfigError(f"{var} must look like 22:00-07:00, got {raw!r}")
    start, _, end = raw.partition("-")
    s, e = _parse_hhmm(start, var), _parse_hhmm(end, var)
    if s == e:
        return None
    return s, e


def in_quiet_hours(window: tuple[int, int] | None, now: datetime | time) -> bool:
    if window is None:
        return False
    start, end = window
    minute = now.hour * 60 + now.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApplianceConfig:
    kind: str  # key into kinds.KIND_SPECS
    ip: str
    port: int | None  # None -> probe OCF_PORT_CANDIDATES
    name: str  # notification title
    course_names: Mapping[str, str] = field(default_factory=dict)
    dtls_local_port: int | None = None  # None -> OS picks

    @property
    def spec(self) -> KindSpec:
        return spec(self.kind)

    @property
    def prefix(self) -> str:
        return self.spec.prefix

    @classmethod
    def from_env(cls, kind: str) -> ApplianceConfig | None:
        """None when this appliance is switched off."""
        ks = spec(kind)
        p = ks.prefix
        ip = _env(f"{p}_IP")
        if not _env_bool(f"{p}_ENABLED", ip is not None):
            return None
        if not ip:
            raise ConfigError(f"{p}_ENABLED is true but {p}_IP is not set")

        courses_var = f"{p}_COURSE_NAMES"
        courses_raw = _env(courses_var)
        if courses_raw is None and kind == "washer":
            courses_raw = _env("COURSE_NAMES")  # pre-dryer name
            if courses_raw is not None:
                courses_var = "COURSE_NAMES"

        local_port_var = f"{p}_DTLS_LOCAL_PORT"
        if (
            _env(local_port_var) is None
            and kind == "washer"
            and _env("DTLS_LOCAL_PORT") is not None
        ):
            local_port_var = "DTLS_LOCAL_PORT"  # pre-dryer name

        return cls(
            kind=kind,
            ip=ip,
            port=_env_port(f"{p}_PORT", None),
            name=_env(f"{p}_NAME", ks.default_name) or ks.default_name,
            course_names=MappingProxyType(parse_course_names(courses_raw, courses_var)),
            dtls_local_port=_local_port(local_port_var, ks.default_local_port),
        )


@dataclass(frozen=True)
class Config:
    appliances: tuple[ApplianceConfig, ...]
    cert_path: str
    key_path: str

    pushover_token: str
    pushover_user: str
    pushover_device: str | None
    pushover_sound: str | None
    pushover_priority: int
    pushover_finished_sound: str | None
    pushover_alarm_priority: int

    events: frozenset[EventKind]
    event_priorities: Mapping[str, int] = field(default_factory=dict)
    quiet_hours: tuple[int, int] | None = None
    quiet_priority: int = -1

    alarm_repeat_s: float = 600.0  # identical alarm content is re-sent at most this often
    offline_after_s: float = 900.0  # how long unreachable before "offline"
    health_interval_s: float = 300.0
    ping_interval_s: float = 25.0
    hot_poll_s: float = 2.0
    hot_poll_active_s: float = 1.0
    log_level: str = "INFO"

    state_dir: str | None = None  # persisted cycle trackers; None disables
    heartbeat_path: str | None = None  # touched while healthy; None disables

    def appliance(self, kind: str) -> ApplianceConfig | None:
        for a in self.appliances:
            if a.kind == kind:
                return a
        return None

    def priority_for(self, kind: str, event_priority: int | None, now: datetime) -> int:
        """Resolve the Pushover priority for one outgoing event: the
        event's own hint, the alarm priority, an explicit per-event
        override, then the quiet-hours cap (alarms are exempt)."""
        priority = self.pushover_priority if event_priority is None else event_priority
        if kind == "alarm":
            priority = self.pushover_alarm_priority
        if kind in self.event_priorities:
            priority = self.event_priorities[kind]
        if kind != "alarm" and in_quiet_hours(self.quiet_hours, now):
            priority = min(priority, self.quiet_priority)
        return priority

    @classmethod
    def from_env(cls) -> Config:
        appliances = tuple(
            a for a in (ApplianceConfig.from_env(k) for k in KINDS) if a is not None
        )
        if not appliances:
            ips = " and/or ".join(f"{KIND_SPECS[k].prefix}_IP" for k in KINDS)
            raise ConfigError(f"no appliance configured: set {ips}")
        ports = [a.dtls_local_port for a in appliances if a.dtls_local_port is not None]
        if len(ports) != len(set(ports)):
            names = " and ".join(f"{a.prefix}_DTLS_LOCAL_PORT" for a in appliances)
            raise ConfigError(f"{names} must differ")

        token = _secret("PUSHOVER_TOKEN")
        user = _secret("PUSHOVER_USER")
        if not token or not user:
            raise ConfigError(
                "PUSHOVER_TOKEN and PUSHOVER_USER are required (or their _FILE variants)"
            )

        events = set(parse_events(_env("EVENTS")))
        if _env_bool("STARTUP_NOTIFY", False):
            events.add("startup")

        log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")

        return cls(
            appliances=appliances,
            cert_path=_env("CERT_PATH", "/config/client_fullchain.pem") or "",
            key_path=_env("KEY_PATH", "/config/client.key") or "",
            pushover_token=token,
            pushover_user=user,
            pushover_device=_env("PUSHOVER_DEVICE"),
            pushover_sound=_env("PUSHOVER_SOUND"),
            pushover_priority=_env_priority("PUSHOVER_PRIORITY", 0),
            pushover_finished_sound=_env("PUSHOVER_FINISHED_SOUND"),
            pushover_alarm_priority=_env_priority("PUSHOVER_ALARM_PRIORITY", 1),
            events=frozenset(events),
            event_priorities=parse_event_priorities(_env("EVENT_PRIORITIES")),
            quiet_hours=parse_quiet_hours(_env("QUIET_HOURS")),
            quiet_priority=_env_priority("QUIET_PRIORITY", -1),
            alarm_repeat_s=_env_float("ALARM_REPEAT_S", 600.0, minimum=0.0),
            offline_after_s=_env_float("OFFLINE_AFTER_S", 900.0, minimum=60.0),
            health_interval_s=_env_float("HEALTH_INTERVAL_S", 300.0, minimum=30.0),
            ping_interval_s=_env_float("PING_INTERVAL_S", 25.0, minimum=5.0),
            hot_poll_s=_env_float("HOT_POLL_S", 2.0, minimum=0.5),
            hot_poll_active_s=_env_float("HOT_POLL_ACTIVE_S", 1.0, minimum=0.5),
            log_level=log_level,
            state_dir=_env("STATE_DIR"),
            heartbeat_path=_env("HEARTBEAT_PATH"),
        )
