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
from datetime import UTC, datetime, tzinfo

# Front-panel error codes shared by Samsung washer / dryer firmware.
# Manual wording, lightly shortened, in sentence case as it appears in
# the notification's Meaning field. Keys are upper-case.
ALARM_CODES: dict[str, str] = {
    # water
    "4C": "Water supply problem: check the tap and inlet hose",
    "4E": "Water supply problem: check the tap and inlet hose",
    "4C2": "Hot water connected to the cold inlet",
    "5C": "Drain problem: check the drain hose and pump filter",
    "5E": "Drain problem: check the drain hose and pump filter",
    "1C": "Water level sensor fault",
    "1E": "Water level sensor fault",
    "OC": "Overflow: water level too high",
    "OE": "Overflow: water level too high",
    "LC": "Water leak detected",
    "LE": "Water leak detected",
    "LC1": "Water leak detected",
    # door / load
    "DC": "Door open or not latched",
    "DE": "Door open or not latched",
    "DC1": "Door lock fault",
    "DDC": "Add-door open during cycle",
    "UB": "Unbalanced load: redistribute the laundry",
    "UE": "Unbalanced load: redistribute the laundry",
    "SUD": "Too much foam: use less detergent",
    "SD": "Too much foam: use less detergent",
    "5D": "Too much foam: use less detergent",
    # heating / sensors / motor
    "HC": "Heater fault",
    "HE": "Heater fault",
    "HC1": "Heater fault",
    "TC": "Temperature sensor fault",
    "TE": "Temperature sensor fault",
    "TC5": "Temperature sensor fault",
    "3C": "Motor fault: try restarting the cycle",
    "3E": "Motor fault: try restarting the cycle",
    "8C": "Vibration sensor fault",
    "AC": "Internal communication fault",
    "AE": "Internal communication fault",
    "AC6": "Internal communication fault",
    "PC": "Power supply fault",
    "9C": "Power supply fault",
    "UC": "Voltage out of range",
    # dryer-specific
    "HOT": "Dryer too hot: check the lint filter and exhaust",
    "FC": "Dryer fan fault",
    "FE": "Dryer fan fault",
    "CLOGGED": "Exhaust blocked: clean the lint filter and duct",
    "FILTER": "Clean the lint filter",
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


UNKNOWN_CODE = "Not in the code table: check the panel"

_TRIGGERED_RE = re.compile(r"\btriggeredTime=(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})")


def alarm_fields(
    alarms: Iterable[tuple[str, str]], tz: tzinfo | None = None
) -> tuple[tuple[str, str], ...]:
    """Notification fields for an alarm: Status, then Code / Meaning per
    recognised code, the raise time (converted from the appliance's UTC
    stamp to `tz`, or process local time) if present, and the raw fields
    only when no code could be decoded (so nothing is hidden)."""
    pairs = tuple(alarms)
    out: list[tuple[str, str]] = [("Status", "Error")]
    codes = codes_in(pairs)
    for code in codes:
        out.append(("Code", code))
        out.append(("Meaning", ALARM_CODES.get(code, UNKNOWN_CODE)))
    for _key, value in pairs:
        m = _TRIGGERED_RE.search(value)
        if m:
            stamp = datetime.strptime(f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S")
            local = stamp.replace(tzinfo=UTC).astimezone(tz)
            out.append(("Raised", local.strftime("%H:%M")))
            break
    if not codes:
        out.append(("Details", "; ".join(f"{k}: {v}" for k, v in pairs) or "None"))
    return tuple(out)


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
