"""
A fake Microsoft Graph for the P0-C recording worker tests.

It sits UNDER the real discovery sources (tenant search, OneDrive Recordings
folder, channel Files folder) and the real CandidateMetadataReader, so their
request shapes and GraphError handling are exercised. Nothing opens a socket.
Every webUrl it returns is a synthetic https URL so a test can prove no URL is
ever persisted. It refuses every write route (createLink and anything else).
"""
from __future__ import annotations

import urllib.parse
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.graph.transport import GraphError
from app.recordings.drive_items import CandidateMetadataReader, ChannelRecordingsFolder, DriveItemDiscovery, \
    OneDriveRecordingsFolder, TenantRecordingSearch

START = datetime(2030, 6, 12, 9, 0, tzinfo=timezone.utc)      # Cairo 12:00 - one business date
END = START + timedelta(hours=2)
SUBJECT = "Synthetic P0-C Lecture"
LOOKUP_USER = "organizer-oid"
DRIVE = "b!organizer-onedrive"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def http(status: int, code: str = "error") -> GraphError:
    return GraphError("Microsoft Graph", status, code, "synthetic failure")


def network() -> GraphError:
    return GraphError("Microsoft Graph", None, "gaierror", "network request failed")


def file_name(subject: str, started: datetime) -> str:
    return f"{subject}-{started.astimezone(timezone.utc):%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4"


class FakeRecordingGraph:
    def __init__(self):
        self.recordings: list[dict] = []
        self.recording_errors: list = []                 # consumed one per recordings listing
        self.onedrive: dict[str, list] = defaultdict(list)   # user -> driveItems
        self.onedrive_errors: dict[str, GraphError] = {}
        self.items: dict[tuple, dict] = {}               # (drive, item) -> current metadata
        self.item_errors: dict[tuple, list] = defaultdict(list)
        self.search_hits: list[dict] = []
        self.search_error: GraphError | None = None
        self.teams: dict[str, list[str]] = {}            # team -> channel ids (joined by the lookup user)
        self.channel_items: dict[str, list] = defaultdict(list)
        self.calls: list[tuple] = []
        self.invalidations = 0
        self._next = 0

    # -- building the world --------------------------------------------------------------
    def recording(self, start: datetime, end: datetime | None, *, call_id="call-1", correlation=None,
                  recording_id=None) -> dict:
        self._next += 1
        row = {"id": recording_id or f"rec-{uuid.uuid4().hex[:12]}", "callId": call_id,
               "contentCorrelationId": correlation or f"corr-{uuid.uuid4().hex[:12]}",
               "createdDateTime": iso(start), "endDateTime": iso(end) if end else None,
               "meetingId": "synthetic-meeting",
               "recordingContentUrl": "https://example.invalid/recording/content"}
        self.recordings.append(row)
        return row

    def mp4(self, *, started: datetime, duration_seconds: float | None, subject=SUBJECT, drive=DRIVE,
            item=None, size=900_000_000, where="onedrive", user=LOOKUP_USER, channel=None,
            index_duration=True) -> dict:
        self._next += 1
        item = item or f"ITEM-{uuid.uuid4().hex[:12]}"
        meta = {"id": item, "name": file_name(subject, started),
                "webUrl": f"https://example.invalid/sites/x/{item}.mp4",
                "parentReference": {"driveId": drive, "id": "folder-1"},
                "createdDateTime": iso(started), "lastModifiedDateTime": iso(started + timedelta(hours=3)),
                "file": {"mimeType": "video/mp4"}, "size": size}
        if duration_seconds is not None:
            meta["video"] = {"duration": int(round(duration_seconds * 1000))}
        self.items[(drive, item)] = meta
        listed = dict(meta) if index_duration else {k: v for k, v in meta.items() if k != "video"}
        if where == "onedrive":
            self.onedrive[user].append(listed)
        elif where == "channel":
            self.channel_items[channel].append(listed)
        elif where == "search":
            self.search_hits.append({k: v for k, v in listed.items() if k != "video"})
        return meta

    def recorded_file(self, recording: dict, *, lead=35.0, duration=None, **kwargs) -> dict:
        """The MP4 Teams writes for a Graph recording: named `lead` s before it, zero at createdDateTime."""
        created = datetime.fromisoformat(recording["createdDateTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(recording["endDateTime"].replace("Z", "+00:00"))
        return self.mp4(started=created - timedelta(seconds=lead),
                        duration_seconds=(end - created).total_seconds() if duration is None else duration, **kwargs)

    # -- the client surface ----------------------------------------------------------------
    def get_collection(self, path: str):
        decoded = urllib.parse.unquote(path)
        if "/onlineMeetings/" in decoded and decoded.endswith("/recordings"):
            self.calls.append(("recordings", decoded))
            if self.recording_errors:
                error = self.recording_errors.pop(0)
                if error is not None:
                    raise error
            return [dict(r) for r in self.recordings]
        if "/drive/root:/Recordings:/children" in decoded:
            user = decoded.split("/users/", 1)[1].split("/", 1)[0]
            self.calls.append(("onedrive", user))
            if user in self.onedrive_errors:
                raise self.onedrive_errors[user]
            return [dict(i) for i in self.onedrive.get(user, [])]
        if decoded.endswith("/joinedTeams?$select=id"):
            self.calls.append(("joinedTeams",))
            return [{"id": t} for t in self.teams]
        if "/allChannels" in decoded:
            team = decoded.split("/teams/", 1)[1].split("/", 1)[0]
            self.calls.append(("allChannels", team))
            return [{"id": c} for c in self.teams.get(team, [])]
        if ":/Recordings:/children" in decoded:
            channel = decoded.split("/items/channel-folder-", 1)[1].split(":/Recordings", 1)[0]
            self.calls.append(("channel", channel))
            return [dict(i) for i in self.channel_items.get(channel, [])]
        raise AssertionError(f"unexpected collection {decoded}")

    def get_json(self, path: str):
        decoded = urllib.parse.unquote(path)
        if decoded.endswith("/filesFolder"):
            channel = decoded.split("/channels/", 1)[1].split("/filesFolder", 1)[0]
            self.calls.append(("filesFolder", channel))
            return {"id": f"channel-folder-{channel}", "parentReference": {"driveId": f"b!channel-drive-{channel}"}}
        if decoded.startswith("/drives/") and "/items/" in decoded and "?$select=" in decoded:
            drive = decoded.split("/drives/", 1)[1].split("/items/", 1)[0]
            item = decoded.split("/items/", 1)[1].split("?", 1)[0]
            self.calls.append(("item", item))
            queue = self.item_errors.get((drive, item))
            if queue:
                error = queue.pop(0)
                if error is not None:
                    raise error
            if (drive, item) not in self.items:
                raise http(404, "itemNotFound")
            return dict(self.items[(drive, item)])
        raise AssertionError(f"unexpected GET {decoded}")

    def post_json(self, path: str, body: dict):
        if path != "/search/query":
            raise AssertionError(f"write route called: POST {path}")
        self.calls.append(("search",))
        if self.search_error is not None:
            raise self.search_error
        return {"value": [{"hitsContainers": [{"hits": [{"resource": dict(h)} for h in self.search_hits],
                                               "moreResultsAvailable": False}]}]}

    def request(self, method, path, **kwargs):
        raise AssertionError(f"no raw request expected: {method} {path}")

    def invalidate_token(self):
        self.invalidations += 1

    # -- worker wiring ------------------------------------------------------------------------
    def discovery(self, *, region: str | None = None) -> DriveItemDiscovery:
        return DriveItemDiscovery([TenantRecordingSearch(self, region=region), OneDriveRecordingsFolder(self),
                                   ChannelRecordingsFolder(self)])

    def reader(self) -> CandidateMetadataReader:
        return CandidateMetadataReader(self)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)
