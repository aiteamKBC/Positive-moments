"""
Creatomate REST API rendering provider (API v2).

    POST https://api.creatomate.com/v2/renders        Authorization: Bearer <key>
    GET  https://api.creatomate.com/v2/renders/{id}

The request is RenderScript without a template: one video element whose
`source` is a FRESH temporary Microsoft Graph download URL for the exact
resolved DriveItem, trimmed with `trim_start` / `trim_duration` (seconds).
`fit: contain` inside a 1280x720 frame preserves the source aspect ratio;
`max_width` / `max_height` cap the output as well.

Render states (official): planned, waiting, transcribing, rendering,
succeeded, failed.

Webhooks from Creatomate carry no signature, so a webhook is never trusted on
its own: the status is always re-read here, server-side, with the API key.

The API key is held in memory only. It never appears in an exception, a log
line, a return value or the database. Neither does the source URL.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.media.render.base import (
    FAILED,
    QUEUED,
    RENDERING,
    SUCCEEDED,
    RenderPolicy,
    RenderProviderError,
    RenderRequest,
    RenderStatus,
    validate_output,
)

API_BASE = "https://api.creatomate.com/v2"
PROVIDER_NAME = "creatomate"

_STATES = {"planned": QUEUED, "waiting": QUEUED, "transcribing": RENDERING,
           "rendering": RENDERING, "succeeded": SUCCEEDED, "failed": FAILED}


def build_render_script(request: RenderRequest) -> dict:
    """The RenderScript body. Pure; asserted by tests."""
    policy = request.policy
    duration = round(request.trim_duration_seconds, 3)
    body = {
        "output_format": policy.output_format,
        "width": policy.max_width, "height": policy.max_height,
        "max_width": policy.max_width, "max_height": policy.max_height,
        "frame_rate": policy.frame_rate,
        "duration": duration,
        "metadata": json.dumps({"job_id": request.job_id}),
        "elements": [{
            "type": "video",
            "source": request.source_url,
            "trim_start": round(request.trim_start_seconds, 3),
            "trim_duration": duration,
            "duration": duration,
            "fit": "contain",
        }],
    }
    if request.webhook_url:
        body["webhook_url"] = request.webhook_url
    return body


def _default_http(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class CreatomateMediaRenderProvider:
    name = PROVIDER_NAME

    def __init__(self, *, api_key: str, http=None, timeout: float = 30.0,
                 api_base: str = API_BASE):
        if not str(api_key or "").strip():
            raise RenderProviderError("CREATOMATE_NOT_CONFIGURED",
                                      "CREATOMATE_API_KEY is not configured", retryable=False)
        self._api_key = api_key.strip()
        self._http = http or _default_http
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")
        self.calls = 0

    def __repr__(self) -> str:
        return "CreatomateMediaRenderProvider(api_key=<hidden>)"

    def _call(self, method: str, path: str, payload: dict | None = None):
        self.calls += 1
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            status, raw = self._http(method, f"{self.api_base}{path}", headers, body,
                                     self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RenderProviderError("CREATOMATE_UNREACHABLE",
                                      f"Creatomate unreachable ({type(error).__name__})") from None
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            data = {}
        if status >= 400:
            # 4xx other than throttling will not succeed on retry.
            retryable = status in (408, 409, 425, 429) or status >= 500
            code = "CREATOMATE_AUTH_REJECTED" if status in (401, 403) else f"CREATOMATE_HTTP_{status}"
            raise RenderProviderError(code, f"Creatomate answered HTTP {status}",
                                      http_status=status, retryable=retryable and status not in (401, 403))
        return data

    @staticmethod
    def _status(data) -> RenderStatus:
        if isinstance(data, list):
            if len(data) != 1:
                raise RenderProviderError("CREATOMATE_UNEXPECTED_RESPONSE",
                                          f"expected one render, got {len(data)}")
            data = data[0]
        if not isinstance(data, dict) or not data.get("id"):
            raise RenderProviderError("CREATOMATE_UNEXPECTED_RESPONSE", "render id missing")
        provider_status = str(data.get("status") or "").lower()
        state = _STATES.get(provider_status)
        if state is None:
            raise RenderProviderError("CREATOMATE_UNKNOWN_STATUS",
                                      f"unknown render status {provider_status[:40]!r}")

        def number(key, kind=float):
            try:
                return kind(data[key]) if data.get(key) is not None else None
            except (TypeError, ValueError):
                return None

        error = data.get("error_message")
        return RenderStatus(
            render_id=str(data["id"]), status=state, provider_status=provider_status,
            output_url=data.get("url") if state == SUCCEEDED else None,
            duration_seconds=number("duration"), size_bytes=number("file_size", int),
            width=number("width", int), height=number("height", int),
            error_message=str(error)[:300] if error else None)

    def submit(self, request: RenderRequest) -> RenderStatus:
        if not str(request.source_url or "").startswith("https://"):
            raise RenderProviderError("SOURCE_URL_INVALID", "the source URL is not https",
                                      retryable=False)
        if request.trim_duration_seconds <= 0 or request.trim_start_seconds < 0:
            raise RenderProviderError("TRIM_INVALID", "invalid trim range", retryable=False)
        return self._status(self._call("POST", "/renders", build_render_script(request)))

    def get_status(self, render_id: str) -> RenderStatus:
        from urllib.parse import quote
        return self._status(self._call("GET", f"/renders/{quote(str(render_id), safe='')}"))

    def validate_result(self, status: RenderStatus, *, expected_duration_seconds: float,
                        policy: RenderPolicy) -> list:
        return validate_output(status, expected_duration_seconds=expected_duration_seconds,
                               policy=policy)
