"""In-memory Bunny, Graph and source fakes for the Bunny pilot unit tests."""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from app.bunny.client import BunnyError
from app.bunny.source import LectureSource, TranscriptSource
from app.media.delivery import SourceFile

UTC = timezone.utc
LIBRARY_ID = 786045
LECTURE_ID = "bb2d676b-1113-54af-876f-2c547a8ebd11"
DRIVE_ID = "b!synthetic-drive"
ITEM_ID = "01SYNTHETICITEM"
CREATED = datetime(2026, 10, 16, 7, 53, 2, 806691, tzinfo=UTC)
WINDOW_SECONDS = 7941.56
FILE_NAME = "AI in Project Control 2026-20261016_075151UTC-Meeting Recording.mp4"
TEMP_URL = "https://tenant.sharepoint.com/_layouts/15/download.aspx?tempauth=SECRET-TEMP-URL"
SPEAKERS = ("Synthetic Trainer Alpha", "Synthetic Learner Beta")


def teams_vtt(cue_count: int = 6, *, last_end_seconds: float = 7937.7) -> str:
    """CRLF Teams-shaped WebVTT with voice tags; last cue ends at last_end_seconds."""
    def stamp(ms):
        h, r = divmod(int(ms), 3_600_000); m, r = divmod(r, 60_000); s, ms_ = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms_:03d}"
    blocks = ["WEBVTT"]
    start = 413_180
    step = (last_end_seconds * 1000 - start) / cue_count
    for i in range(cue_count):
        a = start + int(i * step)
        b = start + int((i + 1) * step) if i < cue_count - 1 else int(last_end_seconds * 1000)
        speaker = SPEAKERS[i % 2]
        blocks.append(f"{stamp(a)} --> {stamp(b)}\r\n<v {speaker}>Spoken words &amp; number {i}.</v>")
    return "\r\n\r\n".join(blocks) + "\r\n"


def lecture_source(raw: str | None = None, **overrides) -> LectureSource:
    raw = raw if raw is not None else teams_vtt()
    import hashlib
    transcript = TranscriptSource(
        selection_id=str(uuid.uuid4()), selection_version="legacy_qa_v8_overlap_cluster_v1",
        selected_part_count=1, part_index=1, part_offset_ms=0, artifact_id=str(uuid.uuid4()),
        provider_transcript_id="TRANSCRIPT-1", provider_created_at=CREATED,
        provider_end_at=CREATED + timedelta(seconds=WINDOW_SECONDS),
        content_correlation_id="corr-1", content_version=1,
        stored_content_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw_content=raw)
    base = LectureSource(
        lecture_id=LECTURE_ID, subject="AI in Project Control 2026", session_date=date(2026, 10, 16),
        is_cancelled=False, recording_drive_id=DRIVE_ID, recording_item_id=ITEM_ID,
        recording_filename=FILE_NAME, link_stage_state="COMPLETE", link_status="WRITTEN",
        link_metadata={"graph": {"created_at": "2026-10-16T07:53:02.8066919Z",
                                 "end_at": "2026-10-16T10:05:24.3666919Z",
                                 "recording_id": "rec-graph-1", "content_correlation_id": "corr-1"}},
        legacy_drive_id=DRIVE_ID, legacy_item_id=ITEM_ID, recording_part_rows=0,
        transcript=transcript, selections_seen=[])
    return replace(base, **overrides)


class FakeGraph:
    def __init__(self, *, duration_ms: int = 7_941_632, name: str = FILE_NAME):
        self.duration_ms, self.name, self.calls = duration_ms, name, []

    def get_json(self, path):
        self.calls.append(path)
        return {"id": ITEM_ID, "name": self.name, "size": 1_058_777_855,
                "file": {"mimeType": "video/mp4"}, "parentReference": {"driveId": DRIVE_ID, "id": "parent"},
                "video": {"duration": self.duration_ms, "height": 1080, "width": 1920, "fourCC": "H264"},
                "createdDateTime": "2026-10-16T10:06:00Z",
                "@microsoft.graph.downloadUrl": TEMP_URL}


class FakeMedia:
    def __init__(self):
        self.calls = 0

    def source_file(self, drive_id, item_id):
        self.calls += 1
        return SourceFile(item_id=item_id, name=FILE_NAME, size_bytes=1_058_777_855,
                          duration_seconds=7941.632, download_url=TEMP_URL)


class FakeCore:
    def __init__(self, **settings):
        self.settings = {"Id": LIBRARY_ID, "EncodingTier": "Free", "EnableTranscribing": False,
                         "EnableTranscribingTitleGeneration": False,
                         "EnableTranscribingDescriptionGeneration": False,
                         "TranscribingCaptionLanguages": [], "OutputCodecs": "x264",
                         "EnabledResolutions": "360p,480p,720p,1080p", "JitEncodingEnabled": False,
                         **settings}

    def get_video_library(self, library_id):
        return dict(self.settings)


class FakeBunny:
    """A tiny Bunny library. Fetch creates a video that 'encodes' on each poll."""

    def __init__(self, library_id: int = LIBRARY_ID, *, length: int = 7941, fetch_error=None,
                 create_on_error: bool = True, polls_to_finish: int = 2, caption_result=None):
        self.library_id = library_id
        self.videos: dict[str, dict] = {}
        self.fetch_calls, self.caption_calls, self.meta_calls, self.list_calls = [], [], [], 0
        self.length, self.fetch_error, self.create_on_error = length, fetch_error, create_on_error
        self.polls_to_finish = polls_to_finish
        self.caption_result = caption_result or {"http_status": 200, "success": True, "statusCode": 200,
                                                 "valid": True, "errorList": [], "warningList": []}
        self.caption_files: dict[tuple, str] = {}

    def _new_video(self, title):
        guid = str(uuid.uuid4())
        self.videos[guid] = {"guid": guid, "title": title, "status": 1, "encodeProgress": 0, "polls": 0,
                             "length": 0, "captions": [], "outputCodecs": None,
                             "availableResolutions": "", "width": 1920, "height": 1080,
                             "storageSize": 0, "transcodingMessages": []}
        return guid

    def list_videos(self, *, page=1, items_per_page=100, search=None, order_by="date"):
        self.list_calls += 1
        items = [dict(v) for v in self.videos.values() if not search or search in v["title"]]
        return {"totalItems": len(items), "currentPage": page, "itemsPerPage": items_per_page, "items": items}

    def find_videos_with_token(self, token):
        return self.list_videos(search=token)["items"]

    def get_video(self, video_id):
        video = self.videos[video_id]
        video["polls"] += 1
        if video["polls"] >= self.polls_to_finish and video["status"] != 4:
            video.update(status=4, encodeProgress=100, length=self.length, outputCodecs="x264",
                         availableResolutions="360p,480p,720p,1080p", storageSize=812_345_678)
        elif video["status"] < 3:
            video.update(status=3, encodeProgress=40)
        return {k: (list(v) if isinstance(v, list) else v) for k, v in video.items()}

    def get_resolutions(self, video_id):
        return {"configuredResolutions": ["360p", "480p", "720p", "1080p"],
                "availableResolutions": self.videos[video_id]["availableResolutions"].split(",")}

    def get_storage(self, video_id):
        return {"originals": 0, "thumbnails": 10, "previews": 20, "mp4Fallback": 0, "miscellaneous": 5}

    def fetch_video(self, source_url, *, title):
        self.fetch_calls.append(title)
        if self.fetch_error:
            if self.create_on_error:
                self._new_video(title)
            raise self.fetch_error
        self._new_video(title)
        return {"success": True, "statusCode": 200, "message": "OK"}

    def set_meta_tags(self, video_id, tags):
        self.meta_calls.append((video_id, dict(tags)))

    def add_caption(self, video_id, srclang, *, label, vtt_text):
        self.caption_calls.append((video_id, srclang, label))
        if self.caption_result.get("valid") is not False:
            video = self.videos[video_id]
            video["captions"] = [c for c in video["captions"] if c["srclang"] != srclang]
            video["captions"].append({"srclang": srclang, "label": label, "version": len(self.caption_calls)})
            self.caption_files[(video_id, srclang)] = vtt_text
        return dict(self.caption_result)


def uncertain(code="UNCERTAIN_NETWORK"):
    return BunnyError(code, "outcome unknown", uncertain=True)


class FakeCdn:
    """open_url stand-in serving the caption files FakeBunny holds."""

    def __init__(self, bunny: FakeBunny, *, status: int | None = None):
        self.bunny, self.status, self.calls = bunny, status, []

    def __call__(self, request, timeout):
        import io
        import urllib.error
        url = request.full_url
        self.calls.append(url)
        if self.status:
            raise urllib.error.HTTPError(url, self.status, "denied", {}, io.BytesIO(b""))
        video_id = url.split("/")[3]
        text = self.bunny.caption_files[(video_id, "en")]

        class _Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _Response(text.encode("utf-8"))
