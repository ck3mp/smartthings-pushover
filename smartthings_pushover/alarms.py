"""Alarm decoding and rate limiting.

`/alarms/vs/0` is `{}` when nothing is wrong. Its populated shape has not
been captured on either test machine, so `describe()` works from the
flattened (key, value) pairs and pulls out anything that looks like a
`code=XX` field. The code table is the Samsung laundry error-code list
from the user manuals; treat a code that is not in it as "look at the
panel".
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable

# Front-panel error codes shared by Samsung washer / dryer firmware.
# Manual wording, lightly shortened. Keys are upper-case.
ALARM_CODES: dict[str, str] = {
    # water
    "4C": "water supply problem: check the tap and inlet hose",
    "4E": "water supply problem: check the tap and inlet hose",
    "4C2": "hot water connected to the cold inlet",
    "5C": "drain problem: check the drain hose and pump filter",
    "5E": "drain problem: check the drain hose and pump filter",
    "1C": "water level sensor fault",
    "1E": "water level sensor fault",
    "OC": "overflow: water level too high",
    "OE": "overflow: water level too high",
    "LC": "water leak detected",
    "LE": "water leak detected",
    "LC1": "water leak detected",
    # door / load
    "DC": "door open or not latched",
    "DE": "door open or not latched",
    "DC1": "door lock fault",
    "DDC": "add-door open during cycle",
    "UB": "unbalanced load: redistribute the laundry",
    "UE": "unbalanced load: redistribute the laundry",
    "SUD": "too much foam: use less detergent",
    "SD": "too much foam: use less detergent",
    "5D": "too much foam: use less detergent",
    # heating / sensors / motor
    "HC": "heater fault",
    "HE": "heater fault",
    "HC1": "heater fault",
    "TC": "temperature sensor fault",
    "TE": "temperature sensor fault",
    "TC5": "temperature sensor fault",
    "3C": "motor fault: try restarting the cycle",
    "3E": "motor fault: try restarting the cycle",
    "8C": "vibration sensor fault",
    "AC": "internal communication fault",
    "AE": "internal communication fault",
    "AC6": "internal communication fault",
    "PC": "power supply fault",
    "9C": "power supply fault",
    "UC": "voltage out of range",
    # dryer-specific
    "HOT": "dryer too hot: check the lint filter and exhaust",
    "FC": "dryer fan fault",
    "FE": "dryer fan fault",
    "CLOGGED": "exhaust blocked: clean the lint filter and duct",
    "FILTER": "clean the lint filter",
}

# Captured on the WW80 with the door open at Start:
#   items: {id=0, description=Alarm, alarmType=Device, code=ErrorCode_DC,
#           triggeredTime=2026-09-10T12:40:48, state=Created}
_CODE_RE = re.compile(r"\bcode=(?:ErrorCode_)?([0-9A-Za-z]{1,7})\b")


def codes_in(alarms: Iterable[tuple[str, str]]) -> list[str]:
    """Every `code=XX` / `code=ErrorCode_XX` value found in the flattened
    alarm pairs, upper-cased, in first-seen order."""
    out: list[str] = []
    for _key, value in alarms:
        for m in _CODE_RE.finditer(value):
            code = m.group(1).upper()
            if code not in out:
                out.append(code)
    return out


def throttle_key(alarms: Iterable[tuple[str, str]]) -> str:
    """What counts as "the same alarm" for rate limiting: the set of codes.
    The appliance re-raises an alarm with a fresh `triggeredTime` every
    time it re-checks, so the full text is useless for de-duplication."""
    pairs = tuple(alarms)
    codes = codes_in(pairs)
    return ",".join(codes) if codes else "\n".join(f"{k}: {v}" for k, v in pairs)


def describe(alarms: Iterable[tuple[str, str]]) -> str:
    """Human text for an alarm notification: one line per recognised
    code, then the raw fields so nothing is hidden."""
    pairs = tuple(alarms)
    lines: list[str] = []
    for code in codes_in(pairs):
        meaning = ALARM_CODES.get(code)
        lines.append(f"Error {code}: {meaning}" if meaning else f"Error {code} (see the panel)")
    if not lines and pairs:
        lines.append("Appliance reports a problem; check the panel")
    lines.extend(f"{k}: {v}" for k, v in pairs)
    return "\n".join(lines)


class AlarmThrottle:
    """Coalesce alarm notifications so a flapping sensor cannot page you
    every second.

    - Identical alarm content is forwarded at most once per
      `repeat_window_s`.
    - At most `max_per_window` distinct alarm notifications go out per
      window; the rest are logged and dropped.
    """

    def __init__(self, repeat_window_s: float = 600.0, max_per_window: int = 3) -> None:
        self.repeat_window_s = repeat_window_s
        self.max_per_window = max_per_window
        self._last_sent: dict[str, float] = {}
        self._recent: deque[float] = deque()

    def allow(self, content: str, now: float) -> tuple[bool, str]:
        """Returns (forward?, reason). Records the send when allowed."""
        if self.repeat_window_s <= 0:
            return True, ""
        cutoff = now - self.repeat_window_s
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        last = self._last_sent.get(content)
        if last is not None and last >= cutoff:
            return False, f"same alarm sent {now - last:.0f}s ago"
        if len(self._recent) >= self.max_per_window:
            return False, (
                f"{self.max_per_window} alarms already sent in the last "
                f"{self.repeat_window_s:.0f}s"
            )
        self._last_sent[content] = now
        self._recent.append(now)
        # Forget stale content keys so the dict cannot grow without bound.
        for key in [k for k, t in self._last_sent.items() if t < cutoff]:
            del self._last_sent[key]
        return True, ""
