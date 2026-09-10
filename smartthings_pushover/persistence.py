"""Tiny JSON store for per-appliance cycle trackers.

Restarting the bridge mid-cycle used to lose the start time, so the
"Finished after 1h 12m" line became "Cycle was about 1h 10m". With a
writable `STATE_DIR` the tracker survives. Every failure is logged once
and otherwise ignored: persistence is a convenience, never a reason to
stop notifying.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class TrackerStore:
    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self.path = path
        self.log = logger or logging.getLogger("state")
        self._warned = False

    def load(self) -> Mapping[str, Any] | None:
        try:
            with self.path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            self._warn(f"cannot read {self.path}: {e}")
            return None
        if not isinstance(data, dict):
            self._warn(f"{self.path} does not hold an object; ignoring it")
            return None
        return data

    def save(self, data: Mapping[str, Any]) -> bool:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(dict(data), fh)
            os.replace(tmp, self.path)
        except OSError as e:
            self._warn(f"cannot write {self.path}: {e}")
            return False
        return True

    def _warn(self, msg: str) -> None:
        if not self._warned:
            self._warned = True
            self.log.warning("%s (cycle state will not persist across restarts)", msg)
