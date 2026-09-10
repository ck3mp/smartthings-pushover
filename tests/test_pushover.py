import io
import json
import urllib.error
import urllib.parse

import pytest

from smartthings_pushover.pushover import Notification, PushoverError, send_once


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener_ok(captured):
    def opener(req, timeout):
        captured["url"] = req.full_url
        captured["form"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return _Resp(json.dumps({"status": 1, "request": "abc"}).encode())
    return opener


def test_form_encoding_and_success():
    captured = {}
    note = Notification(message="Laundry is done", title="Washer", priority=1,
                        sound="magic", device="phone", timestamp=1700000000)
    result = send_once("tok", "usr", note, opener=_opener_ok(captured))
    assert result["request"] == "abc"
    assert captured["url"] == "https://api.pushover.net/1/messages.json"
    assert captured["form"] == {
        "token": "tok", "user": "usr", "message": "Laundry is done",
        "priority": "1", "title": "Washer", "sound": "magic",
        "device": "phone", "timestamp": "1700000000"}


def test_optional_fields_omitted():
    captured = {}
    send_once("tok", "usr", Notification(message="hi"), opener=_opener_ok(captured))
    assert set(captured["form"]) == {"token", "user", "message", "priority"}


def test_message_truncated():
    captured = {}
    send_once("tok", "usr", Notification(message="x" * 5000),
              opener=_opener_ok(captured))
    assert len(captured["form"]["message"]) == 1024


def test_4xx_is_permanent():
    def opener(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(json.dumps({"status": 0, "errors": ["user identifier is invalid"]}).encode()))
    with pytest.raises(PushoverError, match="user identifier is invalid"):
        send_once("tok", "usr", Notification(message="hi"), opener=opener)


def test_5xx_and_429_are_retryable():
    for code in (500, 429):
        def opener(req, timeout, code=code):
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(b""))
        with pytest.raises(urllib.error.HTTPError):
            send_once("tok", "usr", Notification(message="hi"), opener=opener)


def test_status_zero_body_is_error():
    def opener(req, timeout):
        return _Resp(json.dumps({"status": 0, "errors": ["nope"]}).encode())
    with pytest.raises(PushoverError, match="nope"):
        send_once("tok", "usr", Notification(message="hi"), opener=opener)
