"""
A fake Microsoft Graph client for the P0-B transcript worker tests.

It sits UNDER the real TranscriptGateway, so the real request shapes and the
real GraphError -> listing/content status mapping are exercised. Nothing here
opens a socket. Transcript text is synthetic.
"""
from __future__ import annotations

import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.graph.transport import GraphError, GraphResponse
from app.transcripts.graph import TranscriptGateway

START = datetime(2030, 6, 12, 9, 0, tzinfo=timezone.utc)      # Cairo 12:00 - one business date
END = START + timedelta(hours=2)


def vtt(minutes: int = 60, *, speaker: str = "Trainer Synthetic", label: str = "a") -> bytes:
    """Synthetic speaker-attributed WebVTT: one 50-second cue per minute."""
    blocks = ["WEBVTT", ""]
    for m in range(minutes):
        blocks += [f"{m // 60:02d}:{m % 60:02d}:00.000 --> {m // 60:02d}:{m % 60:02d}:50.000",
                   f"<v {speaker}>Synthetic line {label}{m}.</v>", ""]
    return "\n".join(blocks).encode()


def http(status: int, code: str = "error") -> GraphError:
    return GraphError("Microsoft Graph", status, code, "synthetic failure")


def network() -> GraphError:
    return GraphError("Microsoft Graph", None, "gaierror", "network request failed")


class FakeGraph:
    """Programmable listing + content per transcript id; records every call."""

    def __init__(self):
        self.transcripts: dict[str, dict] = {}
        self.listing_errors: list = []          # consumed one per listing call
        self.content: dict[str, list] = defaultdict(list)   # id -> [bytes | GraphError, ...]; last repeats
        self.calls: list[tuple] = []
        self.invalidations = 0

    def add(self, transcript_id: str, *, start: datetime, end: datetime, call_id: str = "call-1",
            content=None, correlation: str | None = None):
        self.transcripts[transcript_id] = {
            "id": transcript_id, "createdDateTime": start.isoformat().replace("+00:00", "Z"),
            "endDateTime": end.isoformat().replace("+00:00", "Z"), "callId": call_id,
            "contentCorrelationId": correlation or f"corr-{transcript_id[-8:]}", "meetingId": "synthetic-meeting"}
        if content is not None:
            self.content[transcript_id] = list(content) if isinstance(content, list) else [content]

    # -- the client surface the gateway uses ------------------------------------
    def get_collection(self, path: str):
        self.calls.append(("list", path))
        if self.listing_errors:
            error = self.listing_errors.pop(0)
            if error is not None:
                raise error
        return list(self.transcripts.values())

    def request(self, method: str, path: str, *, accept: str = "application/json"):
        assert method == "GET"
        transcript_id = urllib.parse.unquote(path.split("/transcripts/", 1)[1].split("/content", 1)[0])
        self.calls.append(("content", transcript_id))
        queue = self.content.get(transcript_id) or [http(404, "NotFound")]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, GraphError):
            raise item
        return GraphResponse(item, "text/vtt", 200)

    def invalidate_token(self):
        self.invalidations += 1

    # -- assertions helpers ---------------------------------------------------------
    def content_calls(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "content"]

    def gateway(self) -> TranscriptGateway:
        return TranscriptGateway(self)
