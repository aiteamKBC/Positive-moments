"""
DriveItem discovery: where the recording FILE lives.

Graph's callRecording tells us THAT a recording exists and when it was
created; it does not say which SharePoint/OneDrive file holds it. That file is
what a human opens, so it is what `recording_url` must point at. Three
independent sources, each small, each cached per run:

  * TenantRecordingSearch  - POST /search/query for the lecture's date. The
    only source that found the 2026-09 Martech recordings (they live in a
    OneDrive no fixed folder listing reaches). App-only search requires a
    `region`; with no region configured the source is not attempted.
  * OneDriveRecordingsFolder - GET /users/{user}/drive/root:/Recordings:/children
    for the lookup mailbox and the validated meeting organizer.
  * ChannelRecordingsFolder - for a channel meeting (thread `…@thread.tacv2`),
    the channel's Files folder `Recordings` child.

A source that FAILS is recorded as failed, never as "found nothing": the
decision refuses to call a candidate unique when any attempted source could
not be read. Application permissions these routes need: Files.Read.All and
Sites.Read.All (search additionally requires `region`). The platform's own
Phase 6A note records that this registration received 403 on a driveItem
route, so until that is granted every source here reports failure and no
link is written.
"""
from __future__ import annotations

from urllib.parse import quote

from app.graph.transport import GraphError
from app.recordings.candidates import candidate_from_drive_item, deduplicate
from app.recordings.models import DiscoveryResult, RecordingTarget


ITEM_FIELDS = "id,name,webUrl,parentReference,createdDateTime,file,folder"
SEARCH_FIELDS = ["id", "name", "webUrl", "createdDateTime", "parentReference", "file"]
SEARCH_PAGE_SIZE = 500


def _failure(source: str, error: GraphError) -> dict:
    return {"source": source, "http_status": error.status,
            "error_code": str(error.code or "")[:120]}


class _Source:
    name = "source"

    def __init__(self, graph):
        self.graph = graph
        self.calls = 0

    def applicable(self, target: RecordingTarget) -> bool:
        return True

    def items(self, target: RecordingTarget) -> list[dict]:
        raise NotImplementedError


class TenantRecordingSearch(_Source):
    name = "tenant_search"

    def __init__(self, graph, *, region: str | None):
        super().__init__(graph)
        self.region = (region or "").strip() or None
        self._by_date: dict[str, list[dict]] = {}

    def applicable(self, target) -> bool:
        return self.region is not None

    def items(self, target) -> list[dict]:
        compact = target.session_date.replace("-", "")
        if compact not in self._by_date:
            self.calls += 1
            payload = self.graph.post_json("/search/query", {"requests": [{
                "entityTypes": ["driveItem"],
                "query": {"queryString": f'{compact} "Meeting Recording" filetype:mp4'},
                "from": 0, "size": SEARCH_PAGE_SIZE, "region": self.region,
                "fields": SEARCH_FIELDS}]})
            hits, more = [], False
            for value in payload.get("value") or []:
                for container in value.get("hitsContainers") or []:
                    more = more or bool(container.get("moreResultsAvailable"))
                    hits.extend(hit.get("resource") or {}
                                for hit in container.get("hits") or [])
            if more:
                # A partial result cannot prove a candidate is the only one.
                raise GraphError("Microsoft Graph", 200, "search_results_truncated",
                                 "search reported more results than one page")
            self._by_date[compact] = hits
        return self._by_date[compact]


class OneDriveRecordingsFolder(_Source):
    name = "onedrive_recordings"

    def __init__(self, graph):
        super().__init__(graph)
        self._by_user: dict[str, list[dict]] = {}

    def users(self, target) -> list[str]:
        users = [target.meeting_lookup_user_id, target.meeting_organizer_user_id]
        return sorted({str(u).strip() for u in users if u and str(u).strip()})

    def applicable(self, target) -> bool:
        return bool(self.users(target))

    def items(self, target) -> list[dict]:
        rows = []
        for user in self.users(target):
            if user not in self._by_user:
                self.calls += 1
                path = (f"/users/{quote(user, safe='')}/drive/root:/Recordings:/children"
                        f"?$select={ITEM_FIELDS}")
                try:
                    self._by_user[user] = self.graph.get_collection(path)
                except GraphError as error:
                    if error.status == 404 and str(error.code) == "itemNotFound":
                        self._by_user[user] = []   # no Recordings folder: truly empty
                    else:
                        raise
            rows.extend(self._by_user[user])
        return rows


class ChannelRecordingsFolder(_Source):
    name = "channel_recordings"

    def __init__(self, graph):
        super().__init__(graph)
        self._channel_team: dict[str, str] | None = None
        self._by_channel: dict[str, list[dict]] = {}

    def applicable(self, target) -> bool:
        thread = target.thread_id or ""
        return thread.endswith("@thread.tacv2") and bool(target.meeting_lookup_user_id)

    def _step(self, step, call, *args):
        """Name the step that failed: joinedTeams, allChannels, filesFolder or children."""
        self.calls += 1
        try:
            return call(*args)
        except GraphError as error:
            raise GraphError(error.service, error.status, f"{step}:{error.code}",
                             error.message) from None

    def _team_for(self, user: str, channel_id: str) -> str | None:
        if self._channel_team is None:
            mapping = {}
            teams = self._step("joinedTeams", self.graph.get_collection,
                               f"/users/{quote(user, safe='')}/joinedTeams?$select=id")
            for team in teams:
                channels = self._step(
                    "allChannels", self.graph.get_collection,
                    f"/teams/{quote(team['id'], safe='')}/allChannels?$select=id")
                for channel in channels:
                    mapping.setdefault(channel["id"], team["id"])
            self._channel_team = mapping
        return self._channel_team.get(channel_id)

    def items(self, target) -> list[dict]:
        channel_id = target.thread_id
        if channel_id not in self._by_channel:
            team_id = self._team_for(target.meeting_lookup_user_id, channel_id)
            if team_id is None:
                raise GraphError("Microsoft Graph", 404, "channel_not_in_joined_teams",
                                 "the meeting channel is not in the lookup user's teams")
            folder = self._step(
                "filesFolder", self.graph.get_json,
                f"/teams/{quote(team_id, safe='')}/channels/"
                f"{quote(channel_id, safe='')}/filesFolder")
            drive_id = (folder.get("parentReference") or {}).get("driveId")
            if not drive_id or not folder.get("id"):
                raise GraphError("Microsoft Graph", 200, "files_folder_unresolved",
                                 "the channel files folder has no drive")
            try:
                self._by_channel[channel_id] = self._step(
                    "children", self.graph.get_collection,
                    f"/drives/{quote(drive_id, safe='')}/items/"
                    f"{quote(folder['id'], safe='')}:/Recordings:/children"
                    f"?$select={ITEM_FIELDS}")
            except GraphError as error:
                if error.status == 404 and str(error.code) == "children:itemNotFound":
                    self._by_channel[channel_id] = []
                else:
                    raise
        return self._by_channel[channel_id]


class DriveItemDiscovery:
    """Run every applicable source; keep failures as failures."""

    def __init__(self, sources):
        self.sources = list(sources)

    @property
    def calls(self) -> int:
        return sum(source.calls for source in self.sources)

    def discover(self, target: RecordingTarget) -> DiscoveryResult:
        attempted, failed, candidates, counts = [], [], [], {}
        for source in self.sources:
            if not source.applicable(target):
                continue
            attempted.append(source.name)
            try:
                raw = source.items(target)
            except GraphError as error:
                failed.append(_failure(source.name, error))
                continue
            found = [candidate_from_drive_item(item, source.name) for item in raw]
            counts[source.name] = sum(1 for c in found if c is not None)
            candidates.extend(found)
        return DiscoveryResult(candidates=tuple(deduplicate(candidates)),
                               sources_attempted=tuple(attempted),
                               sources_failed=tuple(failed),
                               source_counts=tuple(sorted(counts.items())))


class RecordingLinkPublisher:
    """
    The URL to persist for an exact match.

    Prefers an organization-scoped VIEW link (what every n8n-written row holds;
    POST createLink needs Files.ReadWrite.All or Sites.ReadWrite.All for an
    app). If that is refused, falls back to the exact driveItem's own webUrl,
    exactly as v8/v9 did. A SharePoint list-form URL is never persisted.
    """

    def __init__(self, graph, *, create_links: bool = True):
        self.graph = graph
        self.create_links = create_links
        self.calls = 0

    @staticmethod
    def usable(url) -> str | None:
        text = str(url or "").strip()
        if not text.startswith("https://") or "/forms/dispform.aspx" in text.casefold():
            return None
        return text

    def publish(self, candidate) -> tuple[str | None, str | None, dict | None]:
        """(url, 'organization'|'web_url'|None, create_link_failure)."""
        failure = None
        if self.create_links:
            self.calls += 1
            try:
                reply = self.graph.post_json(
                    f"/drives/{quote(candidate.drive_id, safe='')}/items/"
                    f"{quote(candidate.item_id, safe='')}/createLink",
                    {"type": "view", "scope": "organization"})
                url = self.usable((reply.get("link") or {}).get("webUrl"))
                if url:
                    return url, "organization", None
            except GraphError as error:
                failure = _failure("create_link", error)
        url = self.usable(candidate.web_url)
        return (url, "web_url" if url else None, failure)
