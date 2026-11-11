"""
SharePoint delivery: fresh source URLs in, durable DriveItems out.

MEMORY
------
The VPS has run out of memory before. Nothing here holds a video in RAM:

  * the rendered MP4 is streamed from the provider to ONE temporary file in
    DOWNLOAD_CHUNK_BYTES reads;
  * the upload reads that file back UPLOAD_CHUNK_BYTES at a time into a Graph
    upload session (a multiple of 320 KiB, as Graph requires);
  * the temp file is removed on success and on failure.

SECRETS
-------
`@microsoft.graph.downloadUrl`, the Location of a `/content` redirect and an
upload session's `uploadUrl` are pre-authenticated. They are used once, in memory, and never logged, returned
to a caller outside the runner, or persisted. Upload-session requests carry NO
Authorization header (Graph rejects one, and it would leak the token to the
upload host).

OVERWRITE SAFETY
----------------
Uploads use conflictBehavior=fail. If the name already exists, the existing
item is adopted only when its size equals ours (a crash between upload and
commit); otherwise delivery stops with DESTINATION_NAME_CONFLICT.
"""
from __future__ import annotations

import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

from app.graph.transport import GraphError

DOWNLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_CHUNK_UNIT = 320 * 1024
UPLOAD_CHUNK_BYTES = 32 * UPLOAD_CHUNK_UNIT            # 10 MiB
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024              # a clip, not a lecture

# `content.downloadUrl` is how $select asks Graph for the download annotation.
# Selecting the annotation's own name (`@microsoft.graph.downloadUrl`) is
# silently ignored: measured on the production tenant 2026-09-27, the item came
# back without it. The reply still carries it as `@microsoft.graph.downloadUrl`.
SOURCE_FIELDS = "id,name,size,video,file,content.downloadUrl"


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = True):
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class SourceFile:
    item_id: str
    name: str | None
    size_bytes: int | None
    duration_seconds: float | None
    download_url: str            # temporary; never persisted

    def __repr__(self) -> str:
        return f"SourceFile(item_id={self.item_id!r}, name={self.name!r})"


@dataclass(frozen=True)
class DeliveredItem:
    item_id: str
    drive_id: str
    web_url: str
    size_bytes: int | None
    name: str
    adopted_existing: bool = False


def _default_open(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=timeout)


def _source_graph_error(error: GraphError) -> DeliveryError:
    """
    A Graph failure reading the source recording, as a safe job error.

    404/410 and 401/403 do not heal by waiting, so they are final instead of
    retrying on a timer; an operator Retry is honoured for a permission
    failure once access is granted (see RECOVERABLE_FINAL).
    """
    if error.status in (404, 410):
        return DeliveryError("SOURCE_GRAPH_ITEM_NOT_FOUND",
                             "the source recording DriveItem was not found", retryable=False)
    if error.status in (401, 403):
        return DeliveryError("SOURCE_GRAPH_PERMISSION_DENIED",
                             f"Graph refused access to the source recording ({error.code})",
                             retryable=False)
    return DeliveryError(f"SOURCE_ITEM_HTTP_{error.status}",
                         f"source recording could not be read ({error.code})",
                         retryable=error.status is None or error.status >= 429)


def safe_filename(value: str, limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value or "lecture").strip("-")
    return slug[:limit].strip("-") or "lecture"


def output_filename(*, session_date: str, subject: str | None, lecture_id: str,
                    moment_index: int, plan_fingerprint: str) -> str:
    """
    Deterministic: 20260925_AI-in-Project-Control_1a2b3c4d_positive-01_9f8e7d6c.mp4

    The plan fingerprint suffix makes a new plan (new evidence, alignment or
    render policy) a new file, while a rerun of the same plan names the same
    file - which conflictBehavior=fail then refuses to overwrite.
    """
    day = str(session_date or "")[:10].replace("-", "") or "00000000"
    return (f"{day}_{safe_filename(subject)}_{str(lecture_id).replace('-', '')[:8]}"
            f"_positive-{moment_index:02d}_{plan_fingerprint[:8]}.mp4")


class SharePointMedia:
    """Graph drive operations for media, through the platform's app-only client."""

    def __init__(self, graph, *, open_url=None, timeout: float = 120.0,
                 upload_chunk_bytes: int = UPLOAD_CHUNK_BYTES):
        if upload_chunk_bytes % UPLOAD_CHUNK_UNIT:
            raise ValueError("upload chunks must be a multiple of 320 KiB")
        self.graph = graph
        self._open = open_url or _default_open
        self.timeout = timeout
        self.upload_chunk_bytes = upload_chunk_bytes
        self.calls = 0

    # -- the source recording ----------------------------------------------------

    def source_file(self, drive_id: str, item_id: str) -> SourceFile:
        """
        A FRESH temporary download URL for exactly this DriveItem.

        Read immediately before every submit, never stored. The item must be
        the recording's durable identity. The download annotation is preferred;
        without it, `/content` is asked for its redirect WITHOUT following it,
        so the recording itself is never downloaded here.
        """
        self.calls += 1
        path = f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}"
        try:
            item = self.graph.get_json(f"{path}?$select={SOURCE_FIELDS}")
        except GraphError as error:
            raise _source_graph_error(error) from None
        if str(item.get("id") or "") != str(item_id):
            raise DeliveryError("SOURCE_GRAPH_ITEM_MISMATCH",
                                "Graph returned a different DriveItem than the recording's "
                                "durable identity", retryable=False)
        url = item.get("@microsoft.graph.downloadUrl")
        if not str(url or "").startswith("https://"):
            url = self._content_redirect(path)
        duration = (item.get("video") or {}).get("duration")
        return SourceFile(item_id=str(item_id), name=item.get("name"),
                          size_bytes=item.get("size"),
                          duration_seconds=(int(duration) / 1000.0 if duration else None),
                          download_url=url)

    def _content_redirect(self, path: str) -> str:
        """The Location of `/content`'s redirect, in memory only; never followed."""
        self.calls += 1
        try:
            status, location = self.graph.redirect_location(f"{path}/content")
        except GraphError as error:
            raise _source_graph_error(error) from None
        if not 300 <= status < 400:
            raise DeliveryError("SOURCE_GRAPH_DOWNLOAD_URL_UNAVAILABLE",
                                "Graph returned no download URL for the source recording")
        if not str(location or "").startswith("https://"):
            raise DeliveryError("SOURCE_GRAPH_CONTENT_REDIRECT_MISSING",
                                "Graph /content did not redirect to an https download location")
        return location

    def item_facts(self, drive_id: str, item_id: str) -> dict:
        """Name, size and measured duration of a DriveItem. No URL."""
        self.calls += 1
        try:
            item = self.graph.get_json(
                f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}"
                "?$select=id,name,size,video,file")
        except GraphError as error:
            raise DeliveryError(f"SOURCE_ITEM_HTTP_{error.status}",
                                f"source recording metadata unavailable ({error.code})",
                                retryable=error.status is None or error.status >= 429) from None
        duration = (item.get("video") or {}).get("duration")
        return {"name": item.get("name"), "size_bytes": item.get("size"),
                "duration_seconds": int(duration) / 1000.0 if duration else None}

    # -- the rendered output --------------------------------------------------------

    def download_to_temp(self, url: str, *, expected_size: int | None = None,
                         directory: str | None = None) -> str:
        """Stream a rendered MP4 to one temp file; returns its path."""
        handle, path = tempfile.mkstemp(prefix="positive-moment-", suffix=".mp4", dir=directory)
        written = 0
        try:
            with os.fdopen(handle, "wb") as sink:
                try:
                    response = self._open("GET", url, {}, None, self.timeout)
                except (urllib.error.URLError, OSError) as error:
                    raise DeliveryError("RENDER_DOWNLOAD_FAILED",
                                        f"rendered file download failed ({type(error).__name__})"
                                        ) from None
                with response:
                    status = getattr(response, "status", 200)
                    if status >= 400:
                        raise DeliveryError("RENDER_DOWNLOAD_FAILED",
                                            f"rendered file download HTTP {status}")
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_OUTPUT_BYTES:
                            raise DeliveryError("RENDER_OUTPUT_TOO_LARGE",
                                                "rendered file exceeds the size ceiling",
                                                retryable=False)
                        sink.write(chunk)
            if written == 0:
                raise DeliveryError("RENDER_OUTPUT_EMPTY", "rendered file is empty")
            if expected_size is not None and written != expected_size:
                raise DeliveryError("RENDER_OUTPUT_SIZE_MISMATCH",
                                    f"downloaded {written} bytes, provider reported {expected_size}")
            return path
        except BaseException:
            remove_quietly(path)
            raise

    def upload(self, path: str, *, drive_id: str, folder_item_id: str,
               filename: str) -> DeliveredItem:
        size = os.path.getsize(path)
        self.calls += 1
        target = (f"/drives/{quote(drive_id, safe='')}/items/{quote(folder_item_id, safe='')}"
                  f":/{quote(filename, safe='')}:/createUploadSession")
        try:
            session = self.graph.post_json(target, {"item": {
                "@microsoft.graph.conflictBehavior": "fail", "name": filename}})
        except GraphError as error:
            if error.status == 409:
                return self._adopt_existing(drive_id, folder_item_id, filename, size)
            raise DeliveryError(f"UPLOAD_SESSION_HTTP_{error.status}",
                                f"upload session refused ({error.code})",
                                retryable=error.status is None or error.status in (
                                    408, 429) or (error.status or 0) >= 500) from None
        upload_url = session.get("uploadUrl")
        if not str(upload_url or "").startswith("https://"):
            raise DeliveryError("UPLOAD_SESSION_INVALID", "upload session has no URL")
        item = None
        with open(path, "rb") as source:
            offset = 0
            while offset < size:
                chunk = source.read(self.upload_chunk_bytes)
                end = offset + len(chunk) - 1
                headers = {"Content-Length": str(len(chunk)),
                           "Content-Range": f"bytes {offset}-{end}/{size}"}
                try:
                    response = self._open("PUT", upload_url, headers, chunk, self.timeout)
                except urllib.error.HTTPError as error:
                    if error.code == 409:
                        return self._adopt_existing(drive_id, folder_item_id, filename, size)
                    raise DeliveryError(f"UPLOAD_CHUNK_HTTP_{error.code}",
                                        "upload chunk rejected",
                                        retryable=error.code in (408, 429) or error.code >= 500
                                        ) from None
                except (urllib.error.URLError, OSError) as error:
                    raise DeliveryError("UPLOAD_CHUNK_FAILED",
                                        f"upload chunk failed ({type(error).__name__})") from None
                with response:
                    body = response.read()
                    if getattr(response, "status", 200) in (200, 201):
                        import json
                        item = json.loads(body.decode("utf-8")) if body else {}
                offset = end + 1
        if not item or not item.get("id") or not str(item.get("webUrl") or "").startswith("https://"):
            raise DeliveryError("UPLOAD_NOT_CONFIRMED", "Graph did not confirm the uploaded item")
        if item.get("size") is not None and int(item["size"]) != size:
            raise DeliveryError("UPLOAD_SIZE_MISMATCH", "uploaded size differs from the file",
                                retryable=False)
        return DeliveredItem(item_id=str(item["id"]), drive_id=drive_id,
                             web_url=item["webUrl"], size_bytes=size, name=filename)

    def _adopt_existing(self, drive_id, folder_item_id, filename, size) -> DeliveredItem:
        self.calls += 1
        try:
            existing = self.graph.get_json(
                f"/drives/{quote(drive_id, safe='')}/items/{quote(folder_item_id, safe='')}"
                f":/{quote(filename, safe='')}?$select=id,name,size,webUrl")
        except GraphError as error:
            raise DeliveryError("DESTINATION_NAME_CONFLICT",
                                f"a file with this name exists and could not be read ({error.code})",
                                retryable=False) from None
        if int(existing.get("size") or -1) != size or not str(
                existing.get("webUrl") or "").startswith("https://"):
            raise DeliveryError("DESTINATION_NAME_CONFLICT",
                                "a different file with this name already exists; not overwritten",
                                retryable=False)
        return DeliveredItem(item_id=str(existing["id"]), drive_id=drive_id,
                             web_url=existing["webUrl"], size_bytes=size, name=filename,
                             adopted_existing=True)


def remove_quietly(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
