"""Minimal Pushover client: a background queue so DTLS callbacks never
block on HTTPS, retries on transient failures, no third-party HTTP lib.

API: https://pushover.net/api  (POST https://api.pushover.net/1/messages.json)
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_URL = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE = 1024
MAX_TITLE = 250


class PushoverError(Exception):
    """Permanent failure (4xx): bad token/user, invalid parameter."""


@dataclass(frozen=True)
class Notification:
    message: str
    title: str | None = None
    priority: int = 0
    sound: str | None = None
    device: str | None = None
    timestamp: int | None = None

    def form(self, token: str, user: str) -> dict[str, str]:
        data = {
            "token": token,
            "user": user,
            "message": self.message[:MAX_MESSAGE],
            "priority": str(self.priority),
        }
        if self.title:
            data["title"] = self.title[:MAX_TITLE]
        if self.sound:
            data["sound"] = self.sound
        if self.device:
            data["device"] = self.device
        if self.timestamp is not None:
            data["timestamp"] = str(int(self.timestamp))
        return data


def send_once(token: str, user: str, note: Notification, *,
              timeout: float = 15.0, opener=None) -> dict:
    """Synchronous single attempt. Raises PushoverError on a 4xx (do not
    retry), urllib/OSError on network trouble (retry)."""
    body = urllib.parse.urlencode(note.form(token, user)).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "smartthings-pushover/0.1"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read() if hasattr(e, "read") else b""
        detail = _errors_from(payload)
        if 400 <= e.code < 500 and e.code != 429:
            raise PushoverError(f"HTTP {e.code}: {detail}") from e
        raise  # 5xx / 429 are retryable
    try:
        result = json.loads(payload.decode() or "{}")
    except ValueError:
        result = {}
    if result.get("status") != 1:
        raise PushoverError(f"status != 1: {_errors_from(payload)}")
    return result


def _errors_from(payload: bytes) -> str:
    try:
        d = json.loads(payload.decode())
    except Exception:
        return payload[:200].decode(errors="replace") if payload else "no body"
    errs = d.get("errors")
    if isinstance(errs, list) and errs:
        return "; ".join(str(e) for e in errs)
    return json.dumps(d)[:200]


class PushoverSender:
    """Queue + worker thread. `submit()` returns immediately."""

    def __init__(self, token: str, user: str, *,
                 default_device: str | None = None,
                 default_sound: str | None = None,
                 default_priority: int = 0,
                 max_attempts: int = 5,
                 logger: logging.Logger | None = None):
        self.token = token
        self.user = user
        self.default_device = default_device
        self.default_sound = default_sound
        self.default_priority = default_priority
        self.max_attempts = max_attempts
        self.log = logger or logging.getLogger("pushover")
        self._q: "queue.Queue[Notification | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self.sent_count = 0
        self.failed_count = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="pushover")
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(timeout)

    def submit(self, message: str, *, title: str | None = None,
               priority: int | None = None, sound: str | None = None) -> None:
        note = Notification(
            message=message,
            title=title,
            priority=self.default_priority if priority is None else priority,
            sound=sound or self.default_sound,
            device=self.default_device,
            timestamp=int(time.time()),
        )
        self._q.put(note)

    # -- worker ---------------------------------------------------------
    def _run(self) -> None:
        while True:
            note = self._q.get()
            if note is None:
                return
            self._deliver(note)

    def _deliver(self, note: Notification) -> None:
        delay = 2.0
        for attempt in range(1, self.max_attempts + 1):
            try:
                send_once(self.token, self.user, note)
                self.sent_count += 1
                self.log.info("sent: %s", note.message.splitlines()[0])
                return
            except PushoverError as e:
                self.failed_count += 1
                self.log.error("Pushover rejected message (not retrying): %s", e)
                return
            except Exception as e:  # network / 5xx / 429
                if attempt == self.max_attempts:
                    self.failed_count += 1
                    self.log.error("Pushover send failed after %d attempts: %s",
                                   attempt, e)
                    return
                self.log.warning("Pushover send failed (attempt %d): %s; "
                                 "retry in %.0fs", attempt, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
