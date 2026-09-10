"""Environment-driven configuration.

Everything is a plain env var so the same settings work for
`docker run -e`, compose `env_file`, and a bare-metal shell.

Two appliances are supported, a washer and a dryer, each configured by
its own `WASHER_*` / `DRYER_*` block and switched on with
`WASHER_ENABLED` / `DRYER_ENABLED` (default: enabled when its `_IP` is
set). Pushover, event selection, certificates and tuning are shared.
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
# from. Both appliances this was built against listen on 49154.
OCF_PORT_CANDIDATES = (5684,) + tuple(range(49152, 49161))

KINDS = ("washer", "dryer")
DEFAULT_NAMES = {"washer": "Washing machine", "dryer": "Tumble dryer"}
# Each appliance needs its own fixed local UDP port: a restart then
# re-handshakes from the same 5-tuple and the appliance evicts its stale
# session instead of ignoring us for 5-15 minutes.
DEFAULT_LOCAL_PORTS = {"washer": 49700, "dryer": 49701}


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
            raise ConfigError(
                f"{var} entry {pair!r} must look like 1C=Eco 40-60")
        code, _, name = pair.partition("=")
        code = code.strip().upper()
        if code.startswith("COURSE_"):
            code = code[len("COURSE_"):]
        if not code or not name.strip():
            raise ConfigError(f"{var} entry {pair!r} is incomplete")
        out[code] = name.strip()
    return out


def _local_port(var: str, default: int) -> int | None:
    raw = _env(var)
    if raw is None:
        return default
    if raw.lower() in ("0", "none", "random"):
        return None
    return _env_int(var, default)


@dataclass(frozen=True)
class ApplianceConfig:
    kind: str                          # "washer" | "dryer"
    ip: str
    port: int | None                   # None -> probe OCF_PORT_CANDIDATES
    name: str                          # notification title
    course_names: dict[str, str] = field(default_factory=dict)
    dtls_local_port: int | None = None # None -> OS picks

    @property
    def prefix(self) -> str:
        return self.kind.upper()

    @classmethod
    def from_env(cls, kind: str) -> "ApplianceConfig | None":
        """None when this appliance is switched off."""
        if kind not in KINDS:
            raise ValueError(kind)
        p = kind.upper()
        ip = _env(f"{p}_IP")
        if not _env_bool(f"{p}_ENABLED", ip is not None):
            return None
        if not ip:
            raise ConfigError(f"{p}_ENABLED is true but {p}_IP is not set")

        port = None
        if _env(f"{p}_PORT") is not None:
            port = _env_int(f"{p}_PORT", 0)
            if not (1 <= port <= 65535):
                raise ConfigError(f"{p}_PORT out of range: {port}")

        courses_var = f"{p}_COURSE_NAMES"
        courses_raw = _env(courses_var)
        if courses_raw is None and kind == "washer":
            courses_raw = _env("COURSE_NAMES")           # pre-dryer name
            if courses_raw is not None:
                courses_var = "COURSE_NAMES"

        local_port_var = f"{p}_DTLS_LOCAL_PORT"
        if _env(local_port_var) is None and kind == "washer" and _env("DTLS_LOCAL_PORT") is not None:
            local_port_var = "DTLS_LOCAL_PORT"           # pre-dryer name

        return cls(
            kind=kind,
            ip=ip,
            port=port,
            name=_env(f"{p}_NAME", DEFAULT_NAMES[kind]) or DEFAULT_NAMES[kind],
            course_names=parse_course_names(courses_raw, courses_var),
            dtls_local_port=_local_port(local_port_var, DEFAULT_LOCAL_PORTS[kind]),
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

    events: frozenset[str]

    startup_notify: bool = False
    offline_after_s: float = 900.0     # how long unreachable before "offline"
    health_interval_s: float = 300.0
    ping_interval_s: float = 25.0
    hot_poll_s: float = 2.0
    hot_poll_active_s: float = 1.0
    log_level: str = "INFO"

    def appliance(self, kind: str) -> ApplianceConfig | None:
        for a in self.appliances:
            if a.kind == kind:
                return a
        return None

    @classmethod
    def from_env(cls) -> "Config":
        appliances = tuple(a for a in (ApplianceConfig.from_env(k) for k in KINDS)
                           if a is not None)
        if not appliances:
            raise ConfigError(
                "no appliance configured: set WASHER_IP and/or DRYER_IP "
                "(or WASHER_ENABLED/DRYER_ENABLED=true)")
        ports = [a.dtls_local_port for a in appliances if a.dtls_local_port is not None]
        if len(ports) != len(set(ports)):
            raise ConfigError(
                "WASHER_DTLS_LOCAL_PORT and DRYER_DTLS_LOCAL_PORT must differ")

        token = _env("PUSHOVER_TOKEN")
        user = _env("PUSHOVER_USER")
        if not token or not user:
            raise ConfigError("PUSHOVER_TOKEN and PUSHOVER_USER are required")

        priority = _env_int("PUSHOVER_PRIORITY", 0)
        alarm_priority = _env_int("PUSHOVER_ALARM_PRIORITY", 1)
        for label, p in (("PUSHOVER_PRIORITY", priority),
                         ("PUSHOVER_ALARM_PRIORITY", alarm_priority)):
            if not (-2 <= p <= 1):
                # Priority 2 (emergency) needs retry/expire params and
                # keeps buzzing until acknowledged; not what a laundry
                # notification should do by default.
                raise ConfigError(f"{label} must be between -2 and 1")

        return cls(
            appliances=appliances,
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
            startup_notify=_env_bool("STARTUP_NOTIFY", False),
            offline_after_s=_env_float("OFFLINE_AFTER_S", 900.0),
            health_interval_s=_env_float("HEALTH_INTERVAL_S", 300.0),
            ping_interval_s=_env_float("PING_INTERVAL_S", 25.0),
            hot_poll_s=_env_float("HOT_POLL_S", 2.0),
            hot_poll_active_s=_env_float("HOT_POLL_ACTIVE_S", 1.0),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
