"""
Bunny Stream HTTP clients, pinned to Bunny's published OpenAPI documents
(read 2026-10-21):

  video.bunnycdn.com  (library AccessKey)
    GET  /library/{lib}/videos?page&itemsPerPage&search&orderBy   List Videos
    GET  /library/{lib}/videos/{id}                               Get Video
    POST /library/{lib}/videos/fetch      {url, headers?, title?} Fetch Video
         -> StatusModel {success, message, statusCode}: NO video id
    POST /library/{lib}/videos/{id}       {metaTags:[...]}        Update Video
    POST /library/{lib}/videos/{id}/captions/{srclang}
         {label, captionsFile: base64}  -> data {valid, errorList, warningList}
    GET  /library/{lib}/videos/{id}/resolutions | /storage
  api.bunny.net  (ACCOUNT or subuser AccessKey; the library key gets 401)
    GET  /videolibrary/{id}   EncodingTier, EnableTranscribing, OutputCodecs, ...

RETRY POLICY. Reads retry on network errors, 429 and 5xx with bounded
exponential backoff. A write that may have reached Bunny is NEVER retried
here: a timeout, connection reset or 5xx after sending a Fetch or a caption is
raised as UNCERTAIN, and the caller reconciles by reading state back. Only a
429 on Fetch ("too many fetch jobs queued") is a definite refusal and retried.

SECRECY. The AccessKey travels only in the request header. A fetch URL travels
only in the request body. Neither ever appears in an exception, a log line or a
return value; every message from Bunny is scrubbed of URLs before use.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

STREAM_BASE_URL = "https://video.bunnycdn.com"
CORE_BASE_URL = "https://api.bunny.net"

# Bunny VideoModelStatus.
STATUS_NAMES = {0: "Created", 1: "Uploaded", 2: "Processing", 3: "Transcoding", 4: "Finished",
                5: "Error", 6: "UploadFailed", 7: "JitSegmenting", 8: "JitPlaylistsCreated"}
FINISHED = 4
FAILED_STATUSES = frozenset({5, 6})

_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def scrub(text, limit: int = 300) -> str:
    return _URL.sub("[url]", str(text or ""))[:limit]


class BunnyError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None,
                 retryable: bool = False, uncertain: bool = False):
        self.code, self.status = code, status
        self.retryable, self.uncertain = retryable, uncertain
        super().__init__(f"[{code}] {scrub(message)}")


@dataclass(frozen=True)
class Response:
    status: int
    body: object


def _default_open(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class _Http:
    def __init__(self, base_url: str, api_key: str, *, open_url=None, sleep=time.sleep,
                 timeout: float = 60.0, max_attempts: int = 4, backoff_seconds: float = 2.0):
        if not api_key:
            raise ValueError("a Bunny AccessKey is required")
        self._base = base_url.rstrip("/")
        self.__key = api_key
        self._open = open_url or _default_open
        self._sleep = sleep
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.calls = 0

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base={self._base!r})"

    def _once(self, method: str, path: str, body: dict | None) -> Response:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"AccessKey": self.__key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
        self.calls += 1
        try:
            with self._open(request, self.timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
            finally:
                error.close()
            status = error.code
        text = raw.decode("utf-8", "replace") if raw else ""
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = scrub(text)
        return Response(status, parsed)

    @staticmethod
    def _message(body) -> str:
        if isinstance(body, dict):
            return str(body.get("message") or body.get("Message") or body.get("ErrorKey") or "")
        return str(body or "")

    def call(self, method: str, path: str, body: dict | None = None, *, idempotent: bool,
             retry_429: bool = False, pass_statuses: frozenset = frozenset()) -> Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._once(method, path, body)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as error:
                reason = getattr(error, "reason", error)
                # Name resolution failed or the connection was refused: the
                # request never left this machine, so even a write is safe to retry.
                never_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError))
                if not idempotent and not never_sent:
                    raise BunnyError("UNCERTAIN_NETWORK", f"{method} outcome unknown ({type(error).__name__})",
                                     uncertain=True) from None
                if attempt >= self.max_attempts:
                    raise BunnyError("NETWORK_ERROR", f"{type(error).__name__} after {attempt} attempts",
                                     retryable=True) from None
                self._sleep(self.backoff_seconds * 2 ** (attempt - 1))
                continue
            status = response.status
            if status < 400 or status in pass_statuses:
                return response
            message = self._message(response.body)
            if status == 429 and (idempotent or retry_429) and attempt < self.max_attempts:
                self._sleep(self.backoff_seconds * 2 ** (attempt - 1))
                continue
            if status >= 500:
                if not idempotent:
                    raise BunnyError("UNCERTAIN_SERVER_ERROR", message or f"HTTP {status}",
                                     status=status, uncertain=True)
                if attempt < self.max_attempts:
                    self._sleep(self.backoff_seconds * 2 ** (attempt - 1))
                    continue
            raise BunnyError(f"HTTP_{status}", message or f"HTTP {status}", status=status,
                             retryable=status == 429 or status >= 500)


class BunnyStreamClient:
    """Library-scoped Stream API. One instance is bound to ONE library."""

    def __init__(self, library_id: int, api_key: str, *, base_url: str = STREAM_BASE_URL, **http):
        self.library_id = int(library_id)
        self._http = _Http(base_url, api_key, **http)

    @property
    def calls(self) -> int:
        return self._http.calls

    def _lib(self, suffix: str = "") -> str:
        return f"/library/{self.library_id}{suffix}"

    # -- reads -------------------------------------------------------------------
    def list_videos(self, *, page: int = 1, items_per_page: int = 100, search: str | None = None,
                    order_by: str = "date") -> dict:
        query = {"page": page, "itemsPerPage": items_per_page, "orderBy": order_by}
        if search:
            query["search"] = search
        response = self._http.call("GET", self._lib("/videos?" + urllib.parse.urlencode(query)),
                                   idempotent=True)
        if not isinstance(response.body, dict) or not isinstance(response.body.get("items"), list):
            raise BunnyError("INVALID_LIST_RESPONSE", "List Videos returned no items array")
        return response.body

    def find_videos_with_token(self, token: str, *, max_pages: int = 50) -> list[dict]:
        """Every video whose title contains `token` (search first, full scan if unavailable)."""
        try:
            found, page = [], 1
            while page <= max_pages:
                body = self.list_videos(page=page, items_per_page=100, search=token)
                found += [v for v in body["items"] if token in str(v.get("title") or "")]
                if page * int(body.get("itemsPerPage") or 100) >= int(body.get("totalItems") or 0):
                    return found
                page += 1
            raise BunnyError("LIST_TOO_LARGE", "search result exceeded the page bound")
        except BunnyError as error:
            if error.status not in (400, 503):
                raise
        found, page = [], 1                         # "Search is not available": scan
        while page <= max_pages:
            body = self.list_videos(page=page, items_per_page=1000)
            found += [v for v in body["items"] if token in str(v.get("title") or "")]
            if page * int(body.get("itemsPerPage") or 1000) >= int(body.get("totalItems") or 0):
                return found
            page += 1
        raise BunnyError("LIST_TOO_LARGE", "library exceeded the scan bound")

    def get_video(self, video_id: str) -> dict:
        response = self._http.call("GET", self._lib(f"/videos/{urllib.parse.quote(str(video_id))}"),
                                   idempotent=True)
        if not isinstance(response.body, dict) or not response.body.get("guid"):
            raise BunnyError("INVALID_VIDEO_RESPONSE", "Get Video returned no guid")
        return response.body

    def get_resolutions(self, video_id: str) -> dict:
        response = self._http.call(
            "GET", self._lib(f"/videos/{urllib.parse.quote(str(video_id))}/resolutions"), idempotent=True)
        body = response.body if isinstance(response.body, dict) else {}
        return body.get("data") or {}

    def get_storage(self, video_id: str) -> dict:
        response = self._http.call(
            "GET", self._lib(f"/videos/{urllib.parse.quote(str(video_id))}/storage"), idempotent=True)
        body = response.body if isinstance(response.body, dict) else {}
        return body.get("data") or {}

    # -- writes ------------------------------------------------------------------
    def fetch_video(self, source_url: str, *, title: str) -> dict:
        """Ask Bunny to pull `source_url`. Returns StatusModel; carries NO video id."""
        if not str(source_url or "").startswith("https://"):
            raise BunnyError("FETCH_URL_NOT_HTTPS", "fetch source must be an https URL")
        response = self._http.call("POST", self._lib("/videos/fetch"),
                                   {"url": source_url, "title": title},
                                   idempotent=False, retry_429=True)
        body = response.body if isinstance(response.body, dict) else {}
        if body.get("success") is not True:
            raise BunnyError("FETCH_NOT_ACCEPTED", self._http._message(body) or "success was not true",
                             status=response.status, uncertain=True)
        return {"success": True, "statusCode": body.get("statusCode"), "message": scrub(body.get("message"))}

    def delete_video(self, video_id: str) -> dict:
        """
        Delete ONE video. Only the publisher's confirmed-failed-import cleanup
        calls this (app/publisher/bunny_jobs.py), after the deletion is audited.
        404 = already gone. A timeout or 5xx is UNCERTAIN: read the video back.
        """
        response = self._http.call("DELETE", self._lib(f"/videos/{urllib.parse.quote(str(video_id))}"),
                                   idempotent=False, pass_statuses=frozenset({404}))
        return {"deleted": response.status < 400, "already_absent": response.status == 404,
                "http_status": response.status}

    def set_meta_tags(self, video_id: str, tags: dict[str, str]) -> None:
        """Full replace of this video's metaTags (idempotent: same input, same state)."""
        payload = {"metaTags": [{"property": k, "value": v} for k, v in sorted(tags.items())]}
        self._http.call("POST", self._lib(f"/videos/{urllib.parse.quote(str(video_id))}"), payload,
                        idempotent=True)

    def add_caption(self, video_id: str, srclang: str, *, label: str, vtt_text: str) -> dict:
        """Upload one caption file. Returns Bunny's CaptionValidationModel (`data`)."""
        if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})?", srclang or ""):
            raise BunnyError("INVALID_SRCLANG", "srclang must be a language shortcode")
        encoded = base64.b64encode(vtt_text.encode("utf-8")).decode("ascii")
        path = self._lib(f"/videos/{urllib.parse.quote(str(video_id))}/captions/{srclang}")
        # 400 "Failed uploading the captions" carries the validation model too.
        response = self._http.call("POST", path, {"label": label, "captionsFile": encoded},
                                   idempotent=False, pass_statuses=frozenset({400}))
        body = response.body if isinstance(response.body, dict) else {}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {"http_status": response.status,
                "success": body.get("success"), "statusCode": body.get("statusCode"),
                "message": scrub(body.get("message")), "valid": data.get("valid"),
                "errorList": [scrub(x, 200) for x in (data.get("errorList") or [])],
                "warningList": [scrub(x, 200) for x in (data.get("warningList") or [])],
                "warningMessage": scrub(data.get("warningMessage"), 300) or None}


class BunnyCoreClient:
    """Account-scoped core API, used READ-ONLY to verify library settings."""

    def __init__(self, api_key: str, *, base_url: str = CORE_BASE_URL, **http):
        self._http = _Http(base_url, api_key, **http)

    def get_video_library(self, library_id: int) -> dict:
        response = self._http.call("GET", f"/videolibrary/{int(library_id)}", idempotent=True)
        if not isinstance(response.body, dict):
            raise BunnyError("INVALID_LIBRARY_RESPONSE", "Get Video Library returned no object")
        return response.body
