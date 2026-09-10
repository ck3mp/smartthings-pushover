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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from . import __version__

API_URL = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE = 1024
MAX_TITLE = 250
USER_AGENT = f"smartthings-pushover/{__version__}"


class PushoverError(Exception):
    """Permanent failure (4xx): bad token/user, invalid parameter."""


class PushoverQuotaError(PushoverError):
    """HTTP 429: the application's monthly message quota is used up.
    Retrying is pointless until `reset_at` (unix seconds) if known."""

    def __init__(self, detail: str, reset_at: int | None = None) -> None:
        super().__init__(detail)
        self.reset_at = reset_at


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


Opener = Callable[..., Any]


def send_once(
    token: str,
    user: str,
    note: Notification,
    *,
    timeout: float = 15.0,
    opener: Opener | None = None,
) -> dict[str, Any]:
    """Synchronous single attempt. Raises PushoverError on a 4xx (do not
    retry), PushoverQuotaError on 429, urllib/OSError on network trouble
    (retry)."""
    body = urllib.parse.urlencode(note.form(token, user)).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:  # noqa: BLE001 - body is optional diagnostics
            payload = b""
        detail = _errors_from(payload)
        if e.code == 429:
            raise PushoverQuotaError(f"HTTP 429: {detail}", _reset_at(e.headers)) from e
        if 400 <= e.code < 500:
            raise PushoverError(f"HTTP {e.code}: {detail}") from e
        raise  # 5xx is retryable
    try:
        result = json.loads(payload.decode() or "{}")
    except ValueError:
        result = {}
    if not isinstance(result, dict) or result.get("status") != 1:
        raise PushoverError(f"status != 1: {_errors_from(payload)}")
    return result


def _reset_at(headers: Any) -> int | None:
    """Pushover sends X-Limit-App-Reset (unix time the quota resets)."""
    if headers is None:
        return None
    raw = headers.get("X-Limit-App-Reset") if hasattr(headers, "get") else None
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _errors_from(payload: bytes) -> str:
    try:
        d = json.loads(payload.decode())
    except Exception:  # noqa: BLE001 - any undecodable body is shown raw
        return payload[:200].decode(errors="replace") if payload else "no body"
    if not isinstance(d, dict):
        return json.dumps(d)[:200]
    errs = d.get("errors")
    if isinstance(errs, list) and errs:
        return "; ".join(str(e) for e in errs)
    return json.dumps(d)[:200]


class Sender(Protocol):
    """What the bridge needs from a notification sink."""

    sent_count: int
    failed_count: int

    def submit(
        self,
        message: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        sound: str | None = None,
    ) -> None: ...


class NullSender:
    """Swallows everything. Used by `--dump` and by tests."""

    def __init__(self) -> None:
        self.sent_count = 0
        self.failed_count = 0
        self.submitted: list[dict[str, Any]] = []

    def submit(
        self,
        message: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        sound: str | None = None,
    ) -> None:
        self.submitted.append(
            {"message": message, "title": title, "priority": priority, "sound": sound}
        )


class PushoverSender:
    """Queue + worker thread. `submit()` returns immediately."""

    def __init__(
        self,
        token: str,
        user: str,
        *,
        default_device: str | None = None,
        default_sound: str | None = None,
        default_priority: int = 0,
        max_attempts: int = 5,
        logger: logging.Logger | None = None,
        opener: Opener | None = None,
    ) -> None:
        self.token = token
        self.user = user
        self.default_device = default_device
        self.default_sound = default_sound
        self.default_priority = default_priority
        self.max_attempts = max_attempts
        self.log = logger or logging.getLogger("pushover")
        self._opener = opener
        self._q: queue.Queue[Notification | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()  # set to cut retries short at shutdown
        self.sent_count = 0
        self.failed_count = 0
        self.quota_reset_at: int | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="pushover")
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Drain the queue for up to `timeout` seconds, then abandon any
        retry still in progress."""
        if self._thread is None:
            return
        self._q.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._abort.set()
            self._thread.join(2.0)
            if self._thread.is_alive():
                self.log.warning("Pushover worker did not finish; some messages may be lost")

    def submit(
        self,
        message: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        sound: str | None = None,
    ) -> None:
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
        first_line = note.message.splitlines()[0] if note.message else ""
        if self.quota_reset_at is not None and time.time() < self.quota_reset_at:
            self.failed_count += 1
            self.log.error(
                "Pushover quota exhausted until %s; dropping: %s",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(self.quota_reset_at)),
                first_line,
            )
            return
        delay = 2.0
        for attempt in range(1, self.max_attempts + 1):
            try:
                send_once(self.token, self.user, note, opener=self._opener)
                self.sent_count += 1
                self.quota_reset_at = None
                self.log.info("sent: %s", first_line)
                return
            except PushoverQuotaError as e:
                self.failed_count += 1
                self.quota_reset_at = e.reset_at
                when = (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(e.reset_at))
                    if e.reset_at
                    else "the next monthly reset"
                )
                self.log.error(
                    "Pushover monthly message quota exhausted (%s); no messages will be "
                    "delivered until %s",
                    e,
                    when,
                )
                return
            except PushoverError as e:
                self.failed_count += 1
                self.log.error("Pushover rejected message (not retrying): %s", e)
                return
            except Exception as e:  # noqa: BLE001 - network / 5xx: retry
                if attempt == self.max_attempts:
                    self.failed_count += 1
                    self.log.error("Pushover send failed after %d attempts: %s", attempt, e)
                    return
                self.log.warning(
                    "Pushover send failed (attempt %d): %s; retry in %.0fs", attempt, e, delay
                )
                if self._abort.wait(delay):
                    self.failed_count += 1
                    self.log.warning("shutting down; giving up on: %s", first_line)
                    return
                delay = min(delay * 2, 60.0)
