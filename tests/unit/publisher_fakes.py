"""
In-memory Bunny library and SharePoint source fakes for the P0-D publisher tests.

FakeBunnyLibrary behaves like Bunny's Stream API at the level the publisher
relies on: URL Fetch returns NO video id; the fetched video appears under its
title and "encodes" over a few polls; an expired source URL yields an
UploadFailed video; delete, captions, list and search work; every outcome the
publisher must survive (uncertain fetch with / without a created video, refused
fetch, failed / uncertain delete, rejected caption, network outage) is
scriptable. FakeMedia hands out a fresh synthetic temporary URL per call.
Nothing opens a socket.
"""
from __future__ import annotations

import uuid
from collections import deque

from app.bunny.client import BunnyError
from app.media.delivery import DeliveryError, SourceFile

LIBRARY_ID = 786045
RESOLUTIONS = ["360p", "480p", "720p", "1080p"]


def library_settings(**overrides) -> dict:
    return {"Id": LIBRARY_ID, "EncodingTier": "Free", "EnableTranscribing": False,
            "EnableTranscribingTitleGeneration": False, "EnableTranscribingDescriptionGeneration": False,
            "TranscribingCaptionLanguages": [], "OutputCodecs": "x264", "EnabledResolutions": ",".join(RESOLUTIONS),
            "JitEncodingEnabled": False, **overrides}


class FakeCore:
    def __init__(self, **overrides):
        self.settings = library_settings(**overrides)
        self.calls = 0

    def get_video_library(self, library_id):
        self.calls += 1
        return dict(self.settings)


class FakeMedia:
    """source_file(drive, item) -> a FRESH temporary URL (synthetic), from the recording fake's items."""

    def __init__(self, items: dict):
        self.items = items                     # (drive, item) -> driveItem metadata (FakeRecordingGraph.items)
        self.calls = 0
        self.errors: deque = deque()           # consumed one per call; an Exception is raised
        self.expire_next = 0                   # the next N URLs are already expired when Bunny uses them

    def source_file(self, drive_id, item_id):
        self.calls += 1
        if self.errors:
            error = self.errors.popleft()
            if error is not None:
                raise error
        meta = self.items[(drive_id, item_id)]
        expired = self.expire_next > 0
        self.expire_next -= int(expired)
        url = (f"https://example.invalid/dl/{drive_id}/{item_id}?tempauth=SYNTHETIC-{self.calls}"
               f"{'&expired=1' if expired else ''}")
        duration = (meta.get("video") or {}).get("duration")
        return SourceFile(item_id=item_id, name=meta["name"], size_bytes=meta["size"],
                          duration_seconds=duration / 1000 if duration else None, download_url=url)


def transient_source_error():
    return DeliveryError("SOURCE_GRAPH_SERVER_ERROR", "synthetic outage", retryable=True)


def uncertain(code="UNCERTAIN_NETWORK"):
    return BunnyError(code, "outcome unknown", uncertain=True)


class FakeBunnyLibrary:
    def __init__(self, media: FakeMedia, library_id: int = LIBRARY_ID, *, polls_to_finish: int = 2):
        self.media = media
        self.library_id = library_id
        self.polls_to_finish = polls_to_finish
        self.videos: dict[str, dict] = {}
        self.fetch_plan: deque = deque()       # "ok" | "uncertain_created" | "uncertain_absent" | "refused" | "forbidden"
        self.delete_plan: deque = deque()      # "ok" | "fail" | "uncertain_deleted" | "uncertain_kept"
        self.caption_plan: deque = deque()     # "ok" | "uncertain" | "reject"
        self.outage = 0                        # the next N reads fail with a network error
        self.fetch_calls, self.delete_calls, self.caption_calls, self.meta_calls = [], [], [], []

    # -- helpers ------------------------------------------------------------------------------
    def _read(self):
        if self.outage:
            self.outage -= 1
            raise BunnyError("NETWORK_ERROR", "synthetic outage", retryable=True)

    def add_video(self, title, *, status=4, length=7000) -> str:
        guid = str(uuid.uuid4())
        self.videos[guid] = {"guid": guid, "title": title, "status": status, "encodeProgress": 100 if status == 4 else 0,
                             "length": length, "captions": [], "outputCodecs": "x264" if status == 4 else None,
                             "availableResolutions": ",".join(RESOLUTIONS) if status == 4 else "", "width": 1920,
                             "height": 1080, "storageSize": 1, "transcodingMessages": [], "_polls": 0,
                             "_fails": False, "_length": length}
        return guid

    def _created(self, url, title):
        if "tempauth" in title or "https://" in title:
            raise AssertionError("a temporary URL reached a title")
        drive, item = url.split("/dl/", 1)[1].split("?", 1)[0].split("/", 1)
        meta = self.media.items[(drive, item)]
        length = round(((meta.get("video") or {}).get("duration") or 0) / 1000)
        guid = self.add_video(title, status=1, length=length)
        self.videos[guid]["_fails"] = "expired=1" in url
        return guid

    # -- Stream API surface ------------------------------------------------------------------
    def list_videos(self, *, page=1, items_per_page=100, search=None, order_by="date"):
        self._read()
        items = [self._public(v) for v in self.videos.values() if not search or search in v["title"]]
        start = (page - 1) * items_per_page
        return {"totalItems": len(items), "currentPage": page, "itemsPerPage": items_per_page,
                "items": items[start:start + items_per_page]}

    def find_videos_with_token(self, token):
        self._read()
        return [self._public(v) for v in self.videos.values() if token in v["title"]]

    def get_video(self, video_id):
        self._read()
        video = self.videos.get(str(video_id))
        if video is None:
            raise BunnyError("HTTP_404", "Video not found", status=404)
        video["_polls"] += 1
        if video["status"] in (1, 2, 3):
            if video["_fails"]:
                video.update(status=6, encodeProgress=0)
            elif video["_polls"] >= self.polls_to_finish:
                video.update(status=4, encodeProgress=100, length=video["_length"], outputCodecs="x264",
                             availableResolutions=",".join(RESOLUTIONS), storageSize=812_345_678)
            else:
                video.update(status=3, encodeProgress=40)
        return self._public(video)

    @staticmethod
    def _public(video):
        return {k: (list(v) if isinstance(v, list) else v) for k, v in video.items() if not k.startswith("_")}

    def get_resolutions(self, video_id):
        self._read()
        return {"configuredResolutions": list(RESOLUTIONS),
                "availableResolutions": self.videos[str(video_id)]["availableResolutions"].split(",")}

    def get_storage(self, video_id):
        return {"originals": 0, "thumbnails": 1, "previews": 1, "mp4Fallback": 0, "miscellaneous": 0}

    def fetch_video(self, source_url, *, title):
        if not str(source_url).startswith("https://"):
            raise AssertionError("fetch needs an https URL")
        self.fetch_calls.append(title)
        plan = self.fetch_plan.popleft() if self.fetch_plan else "ok"
        if plan == "ok":
            self._created(source_url, title)
            return {"success": True, "statusCode": 200, "message": "OK"}
        if plan == "uncertain_created":
            self._created(source_url, title)
            raise uncertain()
        if plan == "uncertain_absent":
            raise uncertain()
        if plan == "forbidden":
            raise BunnyError("HTTP_401", "unauthorized", status=401)
        raise BunnyError("HTTP_400", "Bad request", status=400)

    def delete_video(self, video_id):
        self.delete_calls.append(str(video_id))
        plan = self.delete_plan.popleft() if self.delete_plan else "ok"
        if plan == "fail":
            raise BunnyError("HTTP_400", "cannot delete", status=400)
        if plan == "uncertain_kept":
            raise uncertain("UNCERTAIN_SERVER_ERROR")
        existed = self.videos.pop(str(video_id), None) is not None
        if plan == "uncertain_deleted":
            raise uncertain("UNCERTAIN_SERVER_ERROR")
        return {"deleted": existed, "already_absent": not existed, "http_status": 200 if existed else 404}

    def set_meta_tags(self, video_id, tags):
        self.meta_calls.append((str(video_id), dict(tags)))

    def add_caption(self, video_id, srclang, *, label, vtt_text):
        self.caption_calls.append((str(video_id), srclang))
        plan = self.caption_plan.popleft() if self.caption_plan else "ok"
        if plan == "reject":
            return {"http_status": 400, "success": False, "valid": False, "errorList": ["synthetic rejection"],
                    "warningList": []}
        video = self.videos[str(video_id)]
        video["captions"] = [c for c in video["captions"] if c["srclang"] != srclang]
        video["captions"].append({"srclang": srclang, "label": label, "version": len(self.caption_calls)})
        video.setdefault("_caption_text", {})[srclang] = vtt_text
        if plan == "uncertain":
            raise uncertain()
        return {"http_status": 200, "success": True, "statusCode": 200, "valid": True, "errorList": [],
                "warningList": []}

    def caption_text(self, video_id, srclang="en"):
        return self.videos[str(video_id)].get("_caption_text", {}).get(srclang)
