"""Resource map for Samsung laundry appliances on the DA_WM_TP1_21 /
TP2_20 firmware family. Verified against a WW80CGC04DAEEU washer
(WW5000C) and a DV80CGC0B0AEEU dryer (DV5000C); both expose the same
tree, differing only in the course resource (`/st/washercourse` vs
`/st/dryercourse`) and in what `/washer/vs/0` carries (temperature /
spin / rinse on the washer, dry level / dry time on the dryer).

Only the Samsung `/<x>/vs/0` siblings push OBSERVE notifications. The
OCF-standard `/<x>/0` paths accept a registration and never fire, so
everything here reads from the vendor paths.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from smartthings_local.ocf.poll_scheduler import PollTier

KINDS = ("washer", "dryer")

STATE_PATH = ("operational", "state", "vs", "0")
SEED_PATH = ("device", "0")

# Selected-course resource and the key holding `Table_NN_Course_HH`.
COURSE_RESOURCES: dict[str, tuple[tuple[str, ...], str]] = {
    "washer": (("st", "washercourse", "vs", "0"), "x.com.samsung.da.st.washerMode"),
    "dryer": (("st", "dryercourse", "vs", "0"), "x.com.samsung.da.st.dryerMode"),
}

_COMMON_WARM: tuple[tuple[str, ...], ...] = (
    ("power", "vs", "0"),
    ("kidslock", "vs", "0"),
    ("remotectrl", "vs", "0"),
    ("alarms", "vs", "0"),
    ("washer", "vs", "0"),                   # temp/spin/rinse or dry level/time
    ("wm", "jobbeginingstatus", "vs", "0"),
)


def warm_paths(kind: str) -> tuple[tuple[str, ...], ...]:
    return _COMMON_WARM + (COURSE_RESOURCES[kind][0],)


def observe_paths(kind: str) -> tuple[tuple[str, ...], ...]:
    return (STATE_PATH,) + warm_paths(kind) + (("energy", "consumption", "vs", "0"),)

# Samsung `x.com.samsung.da.state` values seen on laundry firmware.
RUNNING_STATES = frozenset({"Run", "Running"})
PAUSED_STATES = frozenset({"Pause", "Paused"})
FINISHED_STATES = frozenset({"End", "Finish", "Finished", "Complete"})
IDLE_STATES = frozenset({"Ready", "Idle", "None", "Off"})


def poll_tiers(kind: str, hot_s: float = 2.0, hot_active_s: float = 1.0) -> list[PollTier]:
    """Hot/warm/sweep tiers. OBSERVE pushes are the accelerator; when the
    washer has no internet its notify dispatch goes quiet and these polls
    alone carry the events, so the hot tier must be tight enough that a
    cycle end is noticed within a second or two."""
    return [
        PollTier(
            name="hot",
            interval_s=hot_s,
            active_interval_s=hot_active_s,
            timeout_s=2.0,
            paths=(STATE_PATH,),
        ),
        PollTier(
            name="warm",
            interval_s=15.0,
            timeout_s=4.0,
            paths=warm_paths(kind),
        ),
        PollTier(
            name="sweep",
            interval_s=300.0,
            timeout_s=15.0,
            paths=(SEED_PATH,),
            is_sweep=True,
        ),
    ]


def _s(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)


def _to_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "on", "yes")


def parse_hms(s: str | None) -> int | None:
    """'02:36:00' -> 9360 seconds. None on anything unparseable."""
    if not isinstance(s, str):
        return None
    parts = s.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, sec = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + sec


_COURSE_RE = re.compile(r"(?:^|_)Course_([0-9A-Fa-f]+)$")


def course_code(mode: str | None) -> str | None:
    """'Table_02_Course_1C' -> '1C'. Returns the input verbatim if the
    string isn't in that shape (so unknown formats are still visible)."""
    if not isinstance(mode, str) or not mode:
        return None
    m = _COURSE_RE.search(mode)
    if m:
        return m.group(1).upper()
    return mode


def supported_course_codes(links: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Every course code the appliance's firmware table advertises, in the
    order the firmware lists them.

    `/course/vs/0` carries `x.com.samsung.da.supportedOptions`: one hex
    string laid out as the official SmartThings plugin parses it - a
    header digit N (option fields per record), then one record per course
    of two hex chars of course code followed by N four-char option fields
    (temperature / rinse / spin defaults and allowed bitmaps). Only the
    codes are meaningful to us; the fields are ignored. Returns [] if the
    blob is missing or not in that shape.
    """
    rep = links.get("/course/vs/0") or {}
    raw = rep.get("x.com.samsung.da.supportedOptions")
    if isinstance(raw, (list, tuple)):
        raw = "".join(str(x) for x in raw)
    if not isinstance(raw, str):
        return []
    raw = raw.strip()
    if len(raw) < 3 or not re.fullmatch(r"[0-9A-Fa-f]+", raw):
        return []
    nfields = int(raw[0], 16)
    body = raw[1:]
    rec = 2 + 4 * nfields
    if nfields == 0 or len(body) % rec:
        return []
    codes: list[str] = []
    for i in range(0, len(body), rec):
        code = body[i:i + 2].upper()
        if code not in codes:
            codes.append(code)
    return codes


@dataclass(frozen=True)
class ApplianceState:
    """Flattened, comparable snapshot of what we care about. Washer-only
    and dryer-only fields are simply None on the other appliance."""
    power: str | None = None                 # 'On' / 'Off'
    machine_state: str | None = None         # 'Ready' / 'Run' / 'Pause' / 'End'
    progress: str | None = None              # washer: 'None'/'Wash'/'Rinse'/'Spin'/'Finish'
                                             # dryer:  'None'/'Drying'/'Cooling'/'Finish'
    progress_pct: int | None = None
    remaining_s: int | None = None
    delay_end_s: int | None = None
    course: str | None = None                # hex code, e.g. '1C'
    water_temp: str | None = None            # washer
    spin: str | None = None                  # washer
    rinse: str | None = None                 # washer
    dry_level: str | None = None             # dryer: 'Less' / 'Normal' / 'More'
    dry_time_s: int | None = None            # dryer: manual time-dry setting, 0 = auto
    wrinkle_prevent: bool | None = None      # dryer
    remote_control: bool | None = None
    child_lock: bool | None = None
    alarms: tuple[tuple[str, str], ...] = ()  # sorted (key, value) pairs
    job_begin_status: str | None = None
    energy_wh: int | None = None

    @property
    def running(self) -> bool:
        return self.machine_state in RUNNING_STATES

    @property
    def paused(self) -> bool:
        return self.machine_state in PAUSED_STATES

    @property
    def finished(self) -> bool:
        return self.machine_state in FINISHED_STATES or self.progress == "Finish"

    @property
    def in_cycle(self) -> bool:
        return self.running or self.paused

    def remaining_hms(self) -> str | None:
        if self.remaining_s is None:
            return None
        h, rest = divmod(self.remaining_s, 3600)
        m, s = divmod(rest, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


def _flatten_alarms(rep: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """The rep is `{}` when nothing is wrong. Its populated shape isn't
    documented, so keep every scalar/list field as a sorted string pair
    for display and change detection."""
    if not isinstance(rep, Mapping) or not rep:
        return ()
    out = []
    for k, v in rep.items():
        key = str(k).replace("x.com.samsung.da.", "")
        out.append((key, _compact(v)))
    return tuple(sorted(out))


def _compact(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(_compact(x) for x in v)
    if isinstance(v, Mapping):
        return "{" + ", ".join(
            f"{str(k).replace('x.com.samsung.da.', '')}={_compact(x)}"
            for k, x in v.items()) + "}"
    return str(v)


def flatten(links: Mapping[str, Mapping[str, Any]]) -> ApplianceState:
    """links: href -> rep dict (the StateCache snapshot). Works for both
    kinds: the washer- and dryer-specific keys never collide, so whatever
    the tree carries is picked up and the rest stays None."""
    def g(href: str, key: str) -> Any:
        rep = links.get(href) or {}
        return rep.get(key)

    pct_raw = g("/operational/state/vs/0", "x.com.samsung.da.progressPercentage")
    try:
        pct = int(pct_raw) if pct_raw is not None else None
    except (TypeError, ValueError):
        pct = None

    wh_raw = g("/energy/consumption/vs/0", "x.com.samsung.da.cumulativePower")
    try:
        wh = int(float(wh_raw)) if wh_raw is not None else None
    except (TypeError, ValueError):
        wh = None

    course = None
    for path, key in COURSE_RESOURCES.values():
        course = course_code(g("/" + "/".join(path), key))
        if course:
            break

    wrinkle = g("/washer/vs/0", "x.com.samsung.da.wrinklePrevent")

    return ApplianceState(
        power=_s(g("/power/vs/0", "x.com.samsung.da.power")),
        machine_state=_s(g("/operational/state/vs/0", "x.com.samsung.da.state")),
        progress=_s(g("/operational/state/vs/0", "x.com.samsung.da.progress")),
        progress_pct=pct,
        remaining_s=parse_hms(g("/operational/state/vs/0",
                                "x.com.samsung.da.remainingTime")),
        delay_end_s=parse_hms(g("/operational/state/vs/0",
                                "x.com.samsung.da.delayEndTime")),
        course=course,
        water_temp=_s(g("/washer/vs/0", "x.com.samsung.da.waterTemperature")),
        spin=_s(g("/washer/vs/0", "x.com.samsung.da.spinLevel")),
        rinse=_s(g("/washer/vs/0", "x.com.samsung.da.rinseCycles")),
        dry_level=_s(g("/washer/vs/0", "x.com.samsung.da.dryLevel")),
        dry_time_s=parse_hms(g("/washer/vs/0", "x.com.samsung.da.dryTime")),
        wrinkle_prevent=None if wrinkle is None else _to_bool(wrinkle),
        remote_control=_to_bool(g("/remotectrl/vs/0",
                                  "x.com.samsung.da.remoteControlEnabled")),
        # Samsung reports 'Ready' when the lock is off; anything else is on.
        child_lock=(None if g("/kidslock/vs/0", "x.com.samsung.da.kidsLock") is None
                    else g("/kidslock/vs/0", "x.com.samsung.da.kidsLock") != "Ready"),
        alarms=_flatten_alarms(links.get("/alarms/vs/0")),
        job_begin_status=_s(g("/wm/jobbeginingstatus/vs/0",
                              "x.com.samsung.da.currentStatus")),
        energy_wh=wh,
    )


def is_active(links: Mapping[str, Mapping[str, Any]]) -> bool:
    """PollScheduler hook: tighten the hot tier while a cycle runs."""
    rep = links.get("/operational/state/vs/0") or {}
    return rep.get("x.com.samsung.da.state") in RUNNING_STATES
