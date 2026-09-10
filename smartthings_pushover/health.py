"""Reachability bookkeeping and the container heartbeat.

`Reachability` answers three questions for one appliance without
touching the network: how long has it been down, has the "offline"
threshold just been crossed, and did we already tell the user. The
bridge feeds it connect/disconnect times; the health loop asks it.

`Heartbeat` is the file Docker's HEALTHCHECK looks at: the main loop
touches it while every bridge thread is alive, so a wedged process
(rather than an unreachable appliance) gets restarted.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class Reachability:
    def __init__(self, offline_after_s: float) -> None:
        self.offline_after_s = offline_after_s
        self.down_since: float | None = None
        self.offline_notified = False

    def mark_down(self, now: float) -> None:
        if self.down_since is None:
            self.down_since = now

    def outage_s(self, now: float) -> float | None:
        return None if self.down_since is None else max(0.0, now - self.down_since)

    def mark_up(self, now: float) -> tuple[float | None, bool]:
        """Returns (outage length, whether an 'offline' notice had gone
        out and an 'online' one is therefore due) and resets."""
        outage = self.outage_s(now)
        was_notified = self.offline_notified
        self.down_since = None
        self.offline_notified = False
        return outage, was_notified

    def crossed_offline(self, now: float) -> bool:
        """True exactly once per outage, when it exceeds the threshold."""
        outage = self.outage_s(now)
        if outage is None or self.offline_notified or outage < self.offline_after_s:
            return False
        self.offline_notified = True
        return True


class Heartbeat:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._failed = False

    def touch(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
            os.utime(self.path, None)
        except OSError:
            self._failed = True
            return False
        return True

    @staticmethod
    def is_fresh(path: Path | str, max_age_s: float, now: float | None = None) -> bool:
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return False
        now = time.time() if now is None else now
        return (now - mtime) <= max_age_s
