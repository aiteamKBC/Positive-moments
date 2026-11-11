"""Bunny HTTP client: retry policy, uncertainty, payload shape and secrecy."""
from __future__ import annotations

import base64
import io
import json
import socket
import urllib.error

import pytest

from app.bunny.client import BunnyError, BunnyStreamClient

KEY = "SECRET-LIBRARY-KEY-0000"
TEMP = "https://tenant.sharepoint.com/dl?tempauth=SECRET-TEMP"


class _Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if body is not None else b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Script:
    """open_url fake: plays back a list of outcomes and records requests."""

    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def __call__(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        status, body = outcome
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "x", {},
                                         io.BytesIO(json.dumps(body).encode()))
        return _Resp(status, body)


def client(script):
    return BunnyStreamClient(786045, KEY, open_url=script, sleep=lambda s: None)


def test_reads_retry_429_and_5xx():
    script = Script((429, {}), (503, {}), (200, {"totalItems": 0, "items": []}))
    assert client(script).list_videos()["totalItems"] == 0
    assert len(script.requests) == 3
    assert script.requests[0].get_header("Accesskey") == KEY


def test_fetch_timeout_is_uncertain_and_not_retried():
    script = Script(socket.timeout("timed out"))
    with pytest.raises(BunnyError) as error:
        client(script).fetch_video(TEMP, title="t")
    assert error.value.uncertain and len(script.requests) == 1


def test_fetch_dns_failure_was_never_sent_and_is_retried():
    script = Script(urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
                    (200, {"success": True, "statusCode": 200}))
    assert client(script).fetch_video(TEMP, title="t")["success"] is True
    assert len(script.requests) == 2


def test_fetch_dns_failure_exhausted_is_not_uncertain():
    script = Script(*[urllib.error.URLError(socket.gaierror(11001, "x"))] * 4)
    with pytest.raises(BunnyError) as error:
        client(script).fetch_video(TEMP, title="t")
    assert not error.value.uncertain and error.value.code == "NETWORK_ERROR"


def test_fetch_server_error_is_uncertain_and_not_retried():
    script = Script((500, {"message": f"failed for {TEMP}"}))
    with pytest.raises(BunnyError) as error:
        client(script).fetch_video(TEMP, title="t")
    assert error.value.uncertain and len(script.requests) == 1
    assert "tempauth" not in str(error.value) and KEY not in str(error.value)


def test_fetch_429_is_a_definite_refusal_and_retried():
    script = Script((429, {}), (200, {"success": True, "statusCode": 200}))
    assert client(script).fetch_video(TEMP, title="t")["success"] is True
    assert len(script.requests) == 2
    sent = json.loads(script.requests[-1].data)
    assert sent == {"url": TEMP, "title": "t"}


def test_fetch_definite_400_is_not_uncertain_and_scrubbed():
    script = Script((400, {"message": f"URL validation failed {TEMP}"}))
    with pytest.raises(BunnyError) as error:
        client(script).fetch_video(TEMP, title="t")
    assert not error.value.uncertain and error.value.status == 400
    assert "tempauth" not in str(error.value)


def test_fetch_requires_https():
    with pytest.raises(BunnyError):
        client(Script()).fetch_video("http://insecure", title="t")


def test_caption_payload_and_400_validation_model():
    script = Script((400, {"success": False, "statusCode": 400,
                           "data": {"valid": False, "errorList": ["bad"], "warningList": []}}))
    result = client(script).add_caption("vid", "en", label="English", vtt_text="WEBVTT\n\n")
    assert result["http_status"] == 400 and result["valid"] is False and result["errorList"] == ["bad"]
    body = json.loads(script.requests[0].data)
    assert body["label"] == "English" and base64.b64decode(body["captionsFile"]) == b"WEBVTT\n\n"
    assert script.requests[0].full_url.endswith("/library/786045/videos/vid/captions/en")


def test_search_unavailable_falls_back_to_scan():
    script = Script((400, {"message": "Search is not available for this library"}),
                    (200, {"totalItems": 2, "itemsPerPage": 1000,
                           "items": [{"guid": "a", "title": "x | rec-12345678"}, {"guid": "b", "title": "y"}]}))
    assert [v["guid"] for v in client(script).find_videos_with_token("rec-12345678")] == ["a"]


def test_repr_never_contains_key():
    assert KEY not in repr(client(Script())._http)
