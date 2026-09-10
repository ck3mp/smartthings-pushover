import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse

import pytest

from smartthings_pushover import __version__
from smartthings_pushover.pushover import (
    Notification,
    PushoverError,
    PushoverQuotaError,
    PushoverSender,
    send_once,
)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener_ok(captured):
    def opener(req, timeout):
        captured["url"] = req.full_url
        captured["ua"] = req.get_header("User-agent")
        captured["form"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return _Resp(json.dumps({"status": 1, "request": "abc"}).encode())

    return opener


def _http_error(req, code, body=b"", headers=None):
    return urllib.error.HTTPError(req.full_url, code, "err", headers or {}, io.BytesIO(body))


def test_form_encoding_and_success():
    captured = {}
    note = Notification(message="Laundry is done", title="Washer", priority=1,
                        sound="magic", device="phone", timestamp=1700000000)
    result = send_once("tok", "usr", note, opener=_opener_ok(captured))
    assert result["request"] == "abc"
    assert captured["url"] == "https://api.pushover.net/1/messages.json"
    assert captured["ua"] == f"smartthings-pushover/{__version__}"
    assert captured["form"] == {
        "token": "tok", "user": "usr", "message": "Laundry is done",
        "priority": "1", "title": "Washer", "sound": "magic",
        "device": "phone", "timestamp": "1700000000"}


def test_html_flag_sent_and_stripped_from_log_line():
    captured = {}
    send_once("tok", "usr", Notification(message="<b>Status:</b> Test", html=True),
              opener=_opener_ok(captured))
    assert captured["form"]["html"] == "1"
    captured = {}
    send_once("tok", "usr", Notification(message="plain"), opener=_opener_ok(captured))
    assert "html" not in captured["form"]


def test_optional_fields_omitted():
    captured = {}
    send_once("tok", "usr", Notification(message="hi"), opener=_opener_ok(captured))
    assert set(captured["form"]) == {"token", "user", "message", "priority"}


def test_message_truncated():
    captured = {}
    send_once("tok", "usr", Notification(message="x" * 5000), opener=_opener_ok(captured))
    assert len(captured["form"]["message"]) == 1024


def test_4xx_is_permanent():
    def opener(req, timeout):
        raise _http_error(req, 400, json.dumps(
            {"status": 0, "errors": ["user identifier is invalid"]}).encode())

    with pytest.raises(PushoverError, match="user identifier is invalid"):
        send_once("tok", "usr", Notification(message="hi"), opener=opener)


def test_5xx_is_retryable():
    def opener(req, timeout):
        raise _http_error(req, 500)

    with pytest.raises(urllib.error.HTTPError):
        send_once("tok", "usr", Notification(message="hi"), opener=opener)


def test_429_is_quota_error_with_reset():
    def opener(req, timeout):
        raise _http_error(req, 429, b'{"status":0,"errors":["quota"]}',
                          {"X-Limit-App-Reset": "1800000000"})

    with pytest.raises(PushoverQuotaError) as ei:
        send_once("tok", "usr", Notification(message="hi"), opener=opener)
    assert ei.value.reset_at == 1800000000
    assert isinstance(ei.value, PushoverError)


def test_status_zero_body_is_error():
    def opener(req, timeout):
        return _Resp(json.dumps({"status": 0, "errors": ["nope"]}).encode())

    with pytest.raises(PushoverError, match="nope"):
        send_once("tok", "usr", Notification(message="hi"), opener=opener)


# ---- sender worker ----------------------------------------------------------
def _sender(opener, **kw):
    return PushoverSender("tok", "usr", opener=opener, logger=logging.getLogger("t"), **kw)


def test_sender_delivers_and_applies_defaults():
    captured = {}
    s = _sender(_opener_ok(captured), default_sound="bike", default_device="phone",
                default_priority=-1)
    s.start()
    s.submit("hello\nsecond line", title="T")
    s.stop(timeout=5)
    assert s.sent_count == 1 and s.failed_count == 0
    assert captured["form"]["sound"] == "bike"
    assert captured["form"]["device"] == "phone"
    assert captured["form"]["priority"] == "-1"


def test_sender_quota_drops_until_reset():
    calls = []

    def opener(req, timeout):
        calls.append(1)
        raise _http_error(req, 429, b"", {"X-Limit-App-Reset": str(int(time.time()) + 3600)})

    s = _sender(opener)
    s.start()
    s.submit("a")
    s.submit("b")
    s.stop(timeout=5)
    assert len(calls) == 1  # second message dropped without a request
    assert s.failed_count == 2 and s.quota_reset_at is not None


def test_sender_stop_aborts_long_retry():
    started = threading.Event()

    def opener(req, timeout):
        started.set()
        raise OSError("network down")

    s = _sender(opener, max_attempts=5)
    s.start()
    s.submit("a")
    assert started.wait(2)
    t0 = time.monotonic()
    s.stop(timeout=0.2)  # first retry delay is 2s; stop must not wait for it
    assert time.monotonic() - t0 < 1.5
    assert s.failed_count == 1
