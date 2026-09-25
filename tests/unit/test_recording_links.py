"""
The coded RECORDING_LINK stage, offline.

Graph is a stub, the database is a stub, and nothing here can reach a network
or a provider (tests/conftest.py blocks both). The SQL itself is exercised
against the isolated test database in tests/integration/test_recording_links_persistence.py.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.graph.transport import GraphError
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import (
    GRAPH_ACTIONS,
    LEGACY_WRITE_ACTIONS,
    PROVIDER_ACTIONS,
    StageRunner,
)
from app.orchestration.stages import (
    AUTOMATABLE_ACTIONS,
    COMPLETE,
    LINK_RECORDING,
    MANUAL_REVIEW_REQUIRED,
    MISSING,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    PRODUCTION_WRITE_ACTIONS,
    RECORDING_LINK,
    REVIEW_REQUIRED,
    WAIT_FOR_RECORDING,
    WAITING,
)
from app.orchestration.state import PipelineStateResolver
from app.recordings import models as m
from app.recordings.candidates import (
    candidate_from_drive_item,
    normalize_subject,
    parse_recording_name,
)
from app.recordings.drive_items import (
    ChannelRecordingsFolder,
    DriveItemDiscovery,
    OneDriveRecordingsFolder,
    RecordingLinkPublisher,
    TenantRecordingSearch,
)
from app.recordings.graph_lookup import RecordingMetadataGateway, recordings_path
from app.recordings.matching import match_candidates
from app.recordings.preview import (
    LiveThenOfflineDiscovery,
    LiveThenOfflineMetadata,
    OfflineMetadataGateway,
)
from app.recordings.service import RecordingLinkService
from app.transcripts.identity import teams_call_id
from tests.unit.recording_graph_fakes import FakeGraph, graph_error
from test_orchestration import RecordingRunner, ScriptedLecture, StubPreflight
from test_pipeline_state import LECTURE_ID, StubConnection, StubObservations, complete_rows
from test_recording_v9 import (
    LZ4_CALL_ID,
    LZ4_TRIPLE,
    REAL_CALL_ID,
    REAL_CANONICAL,
    REAL_VARIANT,
    graph_id,
)

ORGANIZER = "11111111-2222-3333-4444-555555555555"
MEETING = "MSo1MTEtbWVldGluZy0xKjAqKjE5Om1lZXRpbmdAdGhyZWFkLnYy"
GRAPH_CREATED = "2026-09-11T07:57:05.0872461Z"
SUBJECT = "Femi-Commercial Intelligence-Oct 25"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def recording(call_id=REAL_CALL_ID, created=GRAPH_CREATED, rid="rec-1"):
    return {"id": rid, "callId": call_id, "createdDateTime": created,
            "contentCorrelationId": "corr-1"}


def lookup_with(graph, call_id=REAL_CALL_ID):
    return RecordingMetadataGateway(graph).lookup(
        meeting_lookup_user_id=ORGANIZER, meeting_id=MEETING, call_id=call_id)


def target(**overrides):
    values = dict(lecture_id=LECTURE_ID, session_date="2026-09-11",
                  legacy_session_id=REAL_CANONICAL, legacy_meeting_id=MEETING,
                  lecture_meeting_id=MEETING, subject=SUBJECT,
                  meeting_lookup_user_id=ORGANIZER, meeting_organizer_user_id=None,
                  existing_recording_url=None, cancelled=False, cancelled_reason=None,
                  lecture_key=None, perfect_recording_url_empty=None,
                  thread_id="19:6125745a17464ca0801b13976816bde1@thread.tacv2")
    values.update(overrides)
    return m.RecordingTarget(**values)


def drive_item(seconds_before_graph=17.0872461, *, subject=SUBJECT, n=0,
               name=None, web_url=None):
    graph = datetime(2026, 9, 11, 7, 57, 5, 87246, tzinfo=timezone.utc)
    stamp = graph - timedelta(seconds=seconds_before_graph)
    name = name or f"{subject}-{stamp:%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4"
    return {"id": f"item-{n}", "name": name, "file": {},
            "webUrl": web_url or f"https://kbc.sharepoint.com/sites/x/r{n}.mp4",
            "parentReference": {"driveId": f"drive-{n}"}}


class StaticDiscovery:
    def __init__(self, items=(), failed=(), attempted=("fixture",), evidence="LIVE"):
        self.items, self.failed, self.attempted = list(items), tuple(failed), attempted
        self.evidence = evidence
        self.calls = 0

    def discover(self, target_):
        self.calls += 1
        return m.DiscoveryResult(
            candidates=tuple(candidate_from_drive_item(i, "fixture") for i in self.items),
            sources_attempted=tuple(self.attempted), sources_failed=self.failed,
            evidence=self.evidence)


class StubRepository:
    def __init__(self, target_=None, write_result=None):
        self._target = target_
        self.writes = []
        self.recorded = []
        self.write_result = write_result or {"write_eligible": True,
                                             "sessions_updated": 1,
                                             "perfect_rows_updated": 0}

    def target(self, connection, lecture_id, legacy_session_id):
        return self._target

    def write_link(self, connection, **kwargs):
        self.writes.append(kwargs)
        return self.write_result

    def record(self, connection, decision, *, written=False, now=None):
        self.recorded.append((decision.status, written))
        return {"stage_state": decision.stage_state}


class StubPublisher:
    def __init__(self, url="https://kbc.sharepoint.com/:v:/s/x/org-link", kind="organization"):
        self.url, self.kind, self.calls = url, kind, 0

    def publish(self, candidate):
        self.calls += 1
        return self.url, self.kind, None


def service(graph=None, discovery=None, repository=None, publisher=None):
    graph = graph or FakeGraph(collections={"/users/": [recording()]})
    return RecordingLinkService(
        repository=repository or StubRepository(target()),
        metadata_gateway=RecordingMetadataGateway(graph),
        discovery=discovery or StaticDiscovery([drive_item()]),
        publisher=publisher)


# ---------------------------------------------------------------------------
# 1. one canonical call-id decoder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (REAL_CANONICAL, REAL_CALL_ID),
    (REAL_VARIANT, REAL_CALL_ID),
    (graph_id(*LZ4_TRIPLE, compress=True), LZ4_CALL_ID),
    (graph_id(*LZ4_TRIPLE, compress=True, trailing_nil=True), LZ4_CALL_ID),
], ids=["canonical", "trailing-nil", "lz4", "lz4-trailing-nil"])
def test_the_call_id_comes_from_the_qa_core_rc4_decoder(raw, expected):
    assert teams_call_id(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-an-id", "ktVizIDG"])
def test_an_undecodable_session_id_has_no_call_id(raw):
    assert teams_call_id(raw) is None


def test_recording_links_have_no_decoder_of_their_own():
    """One decoder, used by QA and recording links alike: no base64 here."""
    for path in pathlib.Path("app/recordings").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "b64decode" not in source and "base64" not in source, path.name


# ---------------------------------------------------------------------------
# 2. organizer-aware Graph recording lookup
# ---------------------------------------------------------------------------

def test_the_lookup_uses_the_organizer_mailbox_never_me():
    graph = FakeGraph(collections={"/users/": [recording()]})
    lookup_with(graph)
    assert graph.paths == [("GET", f"/users/{ORGANIZER}/onlineMeetings/{MEETING}/recordings")]
    assert recordings_path("a b", "m/1") == "/users/a%20b/onlineMeetings/m%2F1/recordings"


@pytest.mark.parametrize("organizer", [None, "", "  "])
def test_a_missing_organizer_id_cannot_build_a_lookup(organizer):
    with pytest.raises(ValueError):
        recordings_path(organizer, MEETING)


@pytest.mark.parametrize("status, diagnostic", [
    (401, "GRAPH_AUTHENTICATION_FAILED"),
    (403, "GRAPH_ACCESS_FORBIDDEN"),
    (404, "GRAPH_MEETING_NOT_VISIBLE_TO_LOOKUP_USER"),
    (429, None), (503, None), (None, "GRAPH_NETWORK_FAILURE"),
])
def test_http_failures_are_lookup_failures_never_recording_absence(status, diagnostic):
    result = lookup_with(FakeGraph(errors={"/users/": graph_error(status)}))
    assert result.status == m.GRAPH_LOOKUP_FAILED
    assert result.status != m.GRAPH_RECORDING_NOT_FOUND
    assert result.http_status == status
    assert result.diagnostic_reason == diagnostic


def test_an_access_policy_refusal_is_named():
    error = graph_error(403, message="No application access policy found for this app")
    assert lookup_with(FakeGraph(errors={"/users/": error})).diagnostic_reason == \
        "APPLICATION_ACCESS_POLICY_DENIED"


def test_a_successful_empty_listing_is_recording_not_found():
    result = lookup_with(FakeGraph(collections={"/users/": []}))
    assert (result.status, result.recordings_returned) == (m.GRAPH_RECORDING_NOT_FOUND, 0)
    other = lookup_with(FakeGraph(collections={"/users/": [recording(call_id="0" * 8 + REAL_CALL_ID[8:])]}))
    assert (other.status, other.recordings_returned, other.call_id_matches) == (
        m.GRAPH_RECORDING_NOT_FOUND, 1, 0)


def test_two_recordings_for_one_call_are_ambiguous():
    result = lookup_with(FakeGraph(collections={"/users/": [
        recording(rid="a"), recording(rid="b", created="2026-09-11T10:03:56Z")]}))
    assert (result.status, result.call_id_matches) == (m.GRAPH_RECORDING_AMBIGUOUS, 2)
    assert result.recording_id is None and result.created_at is None


def test_exactly_one_recording_is_found_with_its_timestamp():
    result = lookup_with(FakeGraph(collections={"/users/": [
        recording(), recording(call_id="f" * 8 + REAL_CALL_ID[8:], rid="x")]}))
    assert (result.status, result.recording_id, result.created_at) == (
        m.GRAPH_RECORDING_FOUND, "rec-1", GRAPH_CREATED)


# ---------------------------------------------------------------------------
# 3. candidate normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("left, right", [
    ("Dr Femi-Commercial Intelligence", "Femi - Commercial   Intelligence"),
    ("Keith | Strategy & Planning – June 2026", "Keith Strategy &amp; Planning June 2026"),
    ("MARTECH - Thur", "martech-thur"),
])
def test_exact_subject_normalization(left, right):
    assert normalize_subject(left) == normalize_subject(right) != ""


def test_different_subjects_stay_different():
    assert normalize_subject("Martech - Thur") != normalize_subject("Martech - Fri")


def test_teams_file_names_parse_to_utc_date_and_time():
    subject, day, ts = parse_recording_name(
        "Martech - Thur-20260917_105013UTC-Meeting Recording.mp4")
    assert (subject, day) == ("martechthur", "2026-09-17")
    assert ts == datetime(2026, 9, 17, 10, 50, 13, tzinfo=timezone.utc).timestamp()
    assert parse_recording_name("x-20260230_105013-Meeting Recording.mp4") is None
    assert parse_recording_name("notes.docx") is None


def test_only_mp4_files_with_a_drive_become_candidates():
    assert candidate_from_drive_item(drive_item(), "s").drive_id == "drive-0"
    assert candidate_from_drive_item({**drive_item(), "name": "a.docx"}, "s") is None
    assert candidate_from_drive_item({**drive_item(), "parentReference": {}}, "s") is None


# ---------------------------------------------------------------------------
# 4. the timestamp rule and exactly-one matching
# ---------------------------------------------------------------------------

FOUND = m.GraphRecordingLookup(status=m.GRAPH_RECORDING_FOUND, call_id=REAL_CALL_ID,
                               created_at="2026-09-11T07:57:05Z")


def match(items, *, subject=SUBJECT, day="2026-09-11"):
    graph = datetime(2026, 9, 11, 7, 57, 5, tzinfo=timezone.utc)
    candidates = []
    for n, lead in enumerate(items):
        stamp = graph - timedelta(seconds=lead)
        item = drive_item(n=n, name=f"{subject}-{stamp:%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4")
        candidates.append(candidate_from_drive_item(item, "fixture"))
    return match_candidates(subject=subject, session_date=day, graph=FOUND,
                            candidates=candidates)


@pytest.mark.parametrize("lead", [0, 17, 24, 27, 72, 73, 120])
def test_a_file_named_up_to_120_seconds_before_graph_matches(lead):
    result = match([lead])
    assert result.status == m.EXACT_RECORDING_FILE_MATCHED
    assert result.timestamp_difference_seconds == lead


@pytest.mark.parametrize("lead", [121, 180, 3600])
def test_a_file_more_than_120_seconds_early_does_not_match(lead):
    result = match([lead])
    assert result.status == m.TIMESTAMP_MISMATCH and result.candidate is None


@pytest.mark.parametrize("lag", [1, 30, 3600])
def test_a_file_named_after_graph_never_matches(lag):
    assert match([-lag]).status == m.TIMESTAMP_MISMATCH


def test_two_qualifying_files_are_ambiguous_and_carry_no_candidate():
    result = match([17, 40])
    assert result.status == m.AMBIGUOUS_RECORDING_FILES
    assert result.candidate is None and result.exact_candidate_count == 2


def test_no_candidate_at_all_is_file_not_found():
    assert match([]).status == m.RECORDING_FILE_NOT_FOUND


def test_a_different_date_is_never_the_same_occurrence():
    assert match([17], day="2026-09-12").status == m.RECORDING_FILE_NOT_FOUND


def test_matching_refuses_without_an_exact_graph_recording():
    with pytest.raises(ValueError):
        match_candidates(subject=SUBJECT, session_date="2026-09-11",
                         graph=m.GraphRecordingLookup(status=m.GRAPH_LOOKUP_FAILED,
                                                      call_id=REAL_CALL_ID),
                         candidates=[])


# ---------------------------------------------------------------------------
# 5. the decision
# ---------------------------------------------------------------------------

def test_an_exact_live_match_is_writable():
    decision = service().decide(target())
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.would_write is True
    assert decision.match.timestamp_difference_seconds == pytest.approx(17.087, abs=0.01)
    assert decision.stage_state == m.STATE_READY


@pytest.mark.parametrize("overrides, status", [
    ({"cancelled": True, "cancelled_reason": "LEGACY_ROW_CANCELLED_SESSION"},
     m.NO_RECORDING_EXPECTED_CANCELLED),
    ({"existing_recording_url": "https://kbc.sharepoint.com/existing"},
     m.RECORDING_ALREADY_LINKED),
    ({"meeting_lookup_user_id": None}, m.ORGANIZER_LOOKUP_ID_MISSING),
    ({"meeting_lookup_user_id": "   "}, m.ORGANIZER_LOOKUP_ID_MISSING),
    ({"legacy_session_id": None}, m.NO_LEGACY_QA_ROW),
    ({"legacy_session_id": "opaque"}, m.SESSION_CALL_ID_MISSING),
    ({"lecture_meeting_id": "another-meeting"}, m.LECTURE_IDENTITY_MISMATCH),
])
def test_preconditions_are_decided_without_calling_graph(overrides, status):
    graph = FakeGraph(collections={"/users/": [recording()]})
    discovery = StaticDiscovery([drive_item()])
    decision = service(graph=graph, discovery=discovery).decide(target(**overrides))
    assert decision.status == status
    assert decision.would_write is False
    assert graph.paths == [] and discovery.calls == 0


def test_cancelled_is_terminal_not_a_defect():
    assert m.stage_state_for(m.NO_RECORDING_EXPECTED_CANCELLED) == m.STATE_NOT_APPLICABLE
    assert m.NO_RECORDING_EXPECTED_CANCELLED not in m.RETRYABLE_STATUSES


@pytest.mark.parametrize("graph, status", [
    (FakeGraph(errors={"/users/": graph_error(403)}), m.GRAPH_LOOKUP_FAILED),
    (FakeGraph(collections={"/users/": []}), m.GRAPH_RECORDING_NOT_FOUND),
    (FakeGraph(collections={"/users/": [recording(rid="a"), recording(rid="b")]}),
     m.GRAPH_RECORDING_AMBIGUOUS),
])
def test_no_file_is_considered_without_an_exact_graph_recording(graph, status):
    discovery = StaticDiscovery([drive_item()])
    decision = service(graph=graph, discovery=discovery).decide(target())
    assert decision.status == status and decision.would_write is False
    assert discovery.calls == 0


def test_a_failed_discovery_source_blocks_even_an_exact_candidate():
    discovery = StaticDiscovery([drive_item()], attempted=("a", "b"),
                                failed=({"source": "b", "http_status": 403,
                                         "error_code": "accessDenied"},))
    decision = service(discovery=discovery).decide(target())
    assert decision.status == m.DRIVE_ITEM_DISCOVERY_FAILED
    assert "partial result: EXACT_RECORDING_FILE_MATCHED" in decision.reason
    assert decision.would_write is False


def test_no_applicable_discovery_source_is_not_file_absence():
    decision = service(discovery=StaticDiscovery([], attempted=())).decide(target())
    assert decision.status == m.DRIVE_ITEM_DISCOVERY_FAILED


def test_offline_evidence_can_never_authorize_a_write():
    decision = service(discovery=StaticDiscovery([drive_item()], evidence="OFFLINE")
                       ).decide(target())
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.would_write is False and decision.would_update_perfect is False


def test_perfect_update_is_predicted_only_for_an_empty_perfect_row():
    assert service().decide(target(lecture_key="k", perfect_recording_url_empty=True)
                            ).would_update_perfect is True
    assert service().decide(target(lecture_key="k", perfect_recording_url_empty=False)
                            ).would_update_perfect is False


# ---------------------------------------------------------------------------
# 6. the write path
# ---------------------------------------------------------------------------

def test_an_armed_exact_match_publishes_and_writes_once():
    repository, publisher = StubRepository(target()), StubPublisher()
    outcome = service(repository=repository, publisher=publisher).link(
        None, LECTURE_ID, REAL_CANONICAL, write=True)
    assert outcome["status"] == m.WRITTEN and outcome["written"] is True
    assert publisher.calls == 1
    [write] = repository.writes
    assert write["link_status"] == m.LINK_ORGANIZATION_VIEW
    assert write["candidate"].item_id == "item-0"
    assert repository.recorded == [(m.WRITTEN, True)]


def test_an_unarmed_run_never_publishes_or_writes():
    repository, publisher = StubRepository(target()), StubPublisher()
    outcome = service(repository=repository, publisher=publisher).link(
        None, LECTURE_ID, REAL_CANONICAL, write=False, persist=False)
    assert outcome["status"] == m.EXACT_RECORDING_FILE_MATCHED
    assert publisher.calls == 0 and repository.writes == [] and repository.recorded == []


@pytest.mark.parametrize("discovery", [
    StaticDiscovery([drive_item(17, n=1), drive_item(40, n=2)]),
    StaticDiscovery([]),
])
def test_ambiguous_or_missing_candidates_never_write(discovery):
    repository, publisher = StubRepository(target()), StubPublisher()
    outcome = service(discovery=discovery, repository=repository,
                      publisher=publisher).link(None, LECTURE_ID, REAL_CANONICAL, write=True)
    assert outcome["written"] is False
    assert publisher.calls == 0 and repository.writes == []


def test_an_existing_recording_url_is_never_overwritten():
    repository, publisher = StubRepository(target(existing_recording_url="https://old")), \
        StubPublisher()
    outcome = service(repository=repository, publisher=publisher).link(
        None, LECTURE_ID, REAL_CANONICAL, write=True)
    assert outcome["status"] == m.RECORDING_ALREADY_LINKED
    assert repository.writes == [] and publisher.calls == 0


def test_a_concurrent_legacy_write_wins_and_is_not_overwritten():
    repository = StubRepository(target(), write_result={
        "write_eligible": True, "sessions_updated": 0, "perfect_rows_updated": 0})
    outcome = service(repository=repository, publisher=StubPublisher()).link(
        None, LECTURE_ID, REAL_CANONICAL, write=True)
    assert outcome["status"] == m.WRITE_REFUSED_ALREADY_LINKED
    assert outcome["written"] is False and outcome["stage_state"] == m.STATE_COMPLETE


def test_no_usable_url_is_a_retry_not_a_write():
    repository = StubRepository(target())
    outcome = service(repository=repository, publisher=StubPublisher(url=None, kind=None)
                      ).link(None, LECTURE_ID, REAL_CANONICAL, write=True)
    assert outcome["status"] == m.LINK_URL_UNAVAILABLE and repository.writes == []


def test_the_publisher_prefers_an_org_link_and_never_keeps_a_list_form_url():
    candidate = candidate_from_drive_item(drive_item(), "s")
    ok = FakeGraph(posts={"/drives/drive-0/items/item-0/createLink":
                          {"link": {"webUrl": "https://kbc.sharepoint.com/:v:/s/x"}}})
    assert RecordingLinkPublisher(ok).publish(candidate)[:2] == (
        "https://kbc.sharepoint.com/:v:/s/x", "organization")
    refused = FakeGraph(errors={"/drives/": graph_error(403)})
    url, kind, failure = RecordingLinkPublisher(refused).publish(candidate)
    assert (url, kind, failure["http_status"]) == (candidate.web_url, "web_url", 403)
    form = candidate_from_drive_item(
        drive_item(web_url="https://kbc.sharepoint.com/Forms/DispForm.aspx?ID=1"), "s")
    assert RecordingLinkPublisher(refused).publish(form)[0] is None


# ---------------------------------------------------------------------------
# 7. DriveItem discovery sources
# ---------------------------------------------------------------------------

def test_discovery_keeps_failures_as_failures_and_deduplicates():
    graph = FakeGraph(collections={f"/users/{ORGANIZER}/drive": [drive_item()]},
                      errors={f"/users/{ORGANIZER}/joinedTeams": graph_error(403)})
    discovery = DriveItemDiscovery([TenantRecordingSearch(graph, region=None),
                                    OneDriveRecordingsFolder(graph),
                                    ChannelRecordingsFolder(graph)])
    result = discovery.discover(target())
    assert result.sources_attempted == ("onedrive_recordings", "channel_recordings")
    assert [f["source"] for f in result.sources_failed] == ["channel_recordings"]
    assert len(result.candidates) == 1
    assert not any(method == "POST" for method, _ in graph.paths)   # no region: no search


def test_a_missing_recordings_folder_is_empty_not_failed():
    graph = FakeGraph(errors={"/users/": graph_error(404, code="itemNotFound")})
    result = DriveItemDiscovery([OneDriveRecordingsFolder(graph)]).discover(target())
    assert result.sources_failed == () and result.candidates == ()


def test_tenant_search_is_a_region_scoped_read_and_refuses_truncation():
    compact = "20260911"
    full = {"value": [{"hitsContainers": [{"moreResultsAvailable": False, "hits": [
        {"resource": drive_item()}]}]}]}
    graph = FakeGraph(posts={"/search/query": full})
    source = TenantRecordingSearch(graph, region="GBR")
    assert [item["id"] for item in source.items(target())] == ["item-0"]
    source.items(target())
    assert graph.paths == [("POST", "/search/query")]            # cached per date
    truncated = {"value": [{"hitsContainers": [{"moreResultsAvailable": True, "hits": []}]}]}
    result = DriveItemDiscovery([TenantRecordingSearch(
        FakeGraph(posts={"/search/query": truncated}), region="GBR")]).discover(target())
    assert result.sources_failed[0]["error_code"] == "search_results_truncated"
    assert compact in str(target().session_date).replace("-", "")


# ---------------------------------------------------------------------------
# 8. preview evidence
# ---------------------------------------------------------------------------

def test_offline_metadata_is_labelled_and_used_only_when_live_fails():
    offline = OfflineMetadataGateway({MEETING: {"value": [recording()]}})
    refused = RecordingMetadataGateway(FakeGraph(errors={"/users/": graph_error(403)}))
    result = LiveThenOfflineMetadata(refused, offline).lookup(
        meeting_lookup_user_id=ORGANIZER, meeting_id=MEETING, call_id=REAL_CALL_ID)
    assert (result.status, result.evidence) == (m.GRAPH_RECORDING_FOUND, "OFFLINE")
    live = RecordingMetadataGateway(FakeGraph(collections={"/users/": [recording()]}))
    assert LiveThenOfflineMetadata(live, offline).lookup(
        meeting_lookup_user_id=ORGANIZER, meeting_id=MEETING,
        call_id=REAL_CALL_ID).evidence == "LIVE"


def test_offline_discovery_is_only_a_fallback():
    failing = DriveItemDiscovery([OneDriveRecordingsFolder(
        FakeGraph(errors={"/users/": graph_error(403)}))])
    result = LiveThenOfflineDiscovery(failing, [drive_item()]).discover(target())
    assert result.evidence == "OFFLINE" and len(result.candidates) == 1
    assert result.live_failures[0]["http_status"] == 403


# ---------------------------------------------------------------------------
# 9. the resolver: durable, idempotent, bounded
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class Observations(StubObservations):
    def __init__(self, *, cancelled_session="false", **kwargs):
        super().__init__(**kwargs)
        self.cancelled_session = cancelled_session

    def recording_link(self, connection, legacy_session_id):
        row = self.legacy_qa_session(connection, legacy_session_id)
        return None if row is None else {**row, "cancelled_session": self.cancelled_session}


class Attempts:
    def __init__(self, attempt=None):
        self.value = attempt
        self.reads = 0

    def attempt(self, connection, lecture_id):
        self.reads += 1
        return self.value


def resolve(*, mode="write", attempt=None, recording_url=None, cancelled="false"):
    attempts = Attempts(attempt)
    resolver = PipelineStateResolver(
        legacy_observations=Observations(recording_url=recording_url,
                                         excel_synced_at=NOW, cancelled_session=cancelled),
        recording_link_mode=mode, recording_links=attempts, clock=lambda: NOW)
    result = resolver.for_lecture(StubConnection(complete_rows()), LECTURE_ID)
    return result, result["stages"][RECORDING_LINK], attempts


def attempt(state, status, *, due=None, written=False):
    from test_pipeline_state import LEGACY_SESSION_ID
    return {"status": status, "stage_state": state, "reason": status, "attempt_count": 2,
            "next_attempt_after": due, "recording_url_written": written,
            "legacy_session_id": LEGACY_SESSION_ID, "last_attempted_at": NOW}


def test_observe_mode_is_exactly_the_phase_4a_answer_and_reads_no_new_table():
    result, stage, attempts = resolve(mode="observe")
    assert (stage["state"], stage["action"], stage["owner"]) == (
        MISSING, WAIT_FOR_RECORDING, "LEGACY_RECORDING_BRANCH")
    assert result["next_executable_action"] == NOTHING_TO_DO
    assert attempts.reads == 0


@pytest.mark.parametrize("mode", ["observe", "write"])
def test_a_cancelled_lecture_is_not_applicable_in_both_modes(mode):
    result, stage, _ = resolve(mode=mode, cancelled="true")
    assert (stage["state"], stage["reason"]) == (NOT_APPLICABLE,
                                                 m.NO_RECORDING_EXPECTED_CANCELLED)
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_a_present_recording_is_complete_and_never_re_evaluated():
    result, stage, _ = resolve(recording_url="https://kbc.sharepoint.com/r")
    assert stage["state"] == COMPLETE
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_write_mode_offers_link_recording_for_a_new_gap():
    result, stage, _ = resolve()
    assert (stage["state"], stage["action"]) == (MISSING, LINK_RECORDING)
    assert result["next_executable_action"] == LINK_RECORDING


def test_a_retry_not_yet_due_waits_and_a_due_one_runs():
    _, waiting, _ = resolve(attempt=attempt(WAITING, m.GRAPH_LOOKUP_FAILED,
                                            due=NOW + timedelta(hours=3)))
    assert (waiting["state"], waiting["action"]) == (WAITING, WAIT_FOR_RECORDING)
    _, due, _ = resolve(attempt=attempt(WAITING, m.GRAPH_LOOKUP_FAILED,
                                        due=NOW - timedelta(minutes=1)))
    assert due["action"] == LINK_RECORDING


def test_a_review_outcome_stays_put_until_a_human_acts():
    result, stage, _ = resolve(attempt=attempt(REVIEW_REQUIRED, m.AMBIGUOUS_RECORDING_FILES))
    assert (stage["state"], stage["action"], stage["reason"]) == (
        REVIEW_REQUIRED, MANUAL_REVIEW_REQUIRED, m.AMBIGUOUS_RECORDING_FILES)
    assert result["requires_review"] is True


def test_a_coded_link_that_was_cleared_is_not_silently_rewritten():
    _, stage, _ = resolve(attempt=attempt(COMPLETE, m.WRITTEN, written=True))
    assert (stage["state"], stage["reason"]) == (
        REVIEW_REQUIRED, "RECORDING_URL_CLEARED_AFTER_CODED_WRITE")


def test_retries_back_off_and_are_bounded():
    from app.db.repositories.recording_links import retry_after
    assert [round((retry_after(n, NOW) - NOW).total_seconds() / 3600) for n in
            (1, 2, 3, 4, 8)] == [6, 12, 24, 24, 24]
    assert m.MAX_ATTEMPTS == 8
    for status in m.REVIEW_STATUSES:
        assert m.stage_state_for(status) == m.STATE_REVIEW


# ---------------------------------------------------------------------------
# 10. the runner, the scheduler and the backfill use ONE stage
# ---------------------------------------------------------------------------

class _Settings:
    def __init__(self, mode):
        self.recording_link_mode = mode

    @property
    def recording_links_writable(self):
        return self.recording_link_mode == "write"


def test_link_recording_is_a_gated_graph_write_that_never_costs_a_generation():
    assert LINK_RECORDING in AUTOMATABLE_ACTIONS
    assert LINK_RECORDING in PRODUCTION_WRITE_ACTIONS and LINK_RECORDING in LEGACY_WRITE_ACTIONS
    assert LINK_RECORDING in GRAPH_ACTIONS and LINK_RECORDING not in PROVIDER_ACTIONS


@pytest.mark.parametrize("mode, persist, legacy, graph, expected", [
    ("write", True, True, True, True),
    ("observe", True, True, True, False),
    ("write", False, True, True, False),
    ("write", True, False, True, False),
    ("write", True, True, False, False),
])
def test_the_runner_runs_link_recording_only_when_everything_allows_it(
        mode, persist, legacy, graph, expected):
    runner = StageRunner(settings=_Settings(mode), persist=persist,
                         allow_legacy_writes=legacy, allow_graph=graph)
    assert runner.can_run(LINK_RECORDING) is expected
    if not expected and mode == "observe":
        assert runner.refusal_reason(LINK_RECORDING) == "RECORDING_LINK_MODE_IS_NOT_WRITE"


def test_the_orchestrator_dispatches_link_recording_like_any_lecture_action():
    lecture = ScriptedLecture("a", [LINK_RECORDING])
    runner = RecordingRunner([lecture])
    from test_orchestration import ScriptedResolver, TARGET
    summary = PipelineOrchestrator(
        resolver=ScriptedResolver([lecture]), runner=runner,
        preflight=StubPreflight(), max_passes=4).run_window(None, TARGET)
    assert runner.calls == [(LINK_RECORDING, "a")]
    assert summary["provider_calls"] == 0
    assert summary["lectures"][0]["final_action"] == NOTHING_TO_DO


def test_scheduler_backfill_and_cli_build_the_same_armed_stage(monkeypatch):
    from app.config.settings import Settings
    from app.orchestration import factory
    from app.orchestration.scheduler import SchedulerConfig

    # Placeholder configuration: nothing here is ever called, it only has to
    # construct. conftest.py blocks every network attempt regardless.
    offline = dict(database_url="", aptem_database_url="", graph_tenant_id="t",
                   graph_client_id="c", graph_client_secret="s",
                   graph_scope="https://graph.microsoft.com/.default",
                   graph_base_url="https://graph.microsoft.com/v1.0",
                   calendar_user_upn="calendar@example.invalid")
    settings = Settings(**offline, recording_link_mode="write")
    scheduled = factory.build_scheduler(settings, config=SchedulerConfig(discover=False))
    backfill = factory.build_backfill_runner(settings, connection_factory=lambda: None)
    for orchestrator in (scheduled.orchestrator, backfill.orchestrator):
        assert orchestrator.resolver.recording_link_mode == "write"
        assert isinstance(orchestrator.runner, StageRunner)
        assert orchestrator.runner.can_run(LINK_RECORDING) is True
    default = Settings(**offline)
    assert factory.build_orchestrator(default).runner.can_run(LINK_RECORDING) is False


def test_the_recording_package_never_imports_a_model_provider_or_n8n():
    banned = ("app.qa.provider", "openai", "n8n_preflight", "tools.")
    for path in pathlib.Path("app/recordings").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [node.module or "" for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)]
        names += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                  for alias in node.names]
        assert not [n for n in names if any(n.startswith(b) for b in banned)], path.name


# ---------------------------------------------------------------------------
# 11. preview evidence labels and classification details
# ---------------------------------------------------------------------------

def test_a_non_exact_outcome_decided_from_offline_files_is_labelled_offline():
    from app.recordings.preview import OFFLINE_VERIFIED, verification_of
    decision = service(discovery=StaticDiscovery([], evidence="OFFLINE")).decide(target())
    assert decision.status == m.RECORDING_FILE_NOT_FOUND
    assert verification_of(decision) == OFFLINE_VERIFIED


def test_a_graph_refusal_with_no_evidence_is_not_yet_verified():
    from app.recordings.preview import NOT_YET_VERIFIED, verification_of
    decision = service(graph=FakeGraph(errors={"/users/": graph_error(403)})).decide(target())
    assert verification_of(decision) == NOT_YET_VERIFIED


def test_an_empty_live_discovery_may_be_supplemented_in_a_preview_only():
    empty = DriveItemDiscovery([OneDriveRecordingsFolder(FakeGraph())])
    result = LiveThenOfflineDiscovery(empty, [drive_item()]).discover(target())
    assert result.evidence == "OFFLINE" and result.live_source_counts == (
        ("onedrive_recordings", 0),)
    assert LiveThenOfflineDiscovery(empty, None).discover(target()).evidence == "LIVE"


def test_a_neighbouring_lecture_file_is_a_bounded_retry_not_a_review():
    assert m.SUBJECT_MISMATCH in m.RETRYABLE_STATUSES
    assert m.stage_state_for(m.SUBJECT_MISMATCH) == m.STATE_WAITING
    assert m.stage_state_for(m.TIMESTAMP_MISMATCH) == m.STATE_REVIEW


def test_channel_discovery_names_the_step_graph_refused():
    graph = FakeGraph(collections={f"/users/{ORGANIZER}/joinedTeams": [{"id": "team-1"}]},
                      errors={"/teams/team-1/allChannels": graph_error(403)})
    result = DriveItemDiscovery([ChannelRecordingsFolder(graph)]).discover(target())
    assert result.sources_failed[0]["error_code"] == "allChannels:Forbidden"


def test_the_qa_writers_never_own_a_recording_column():
    """QA refreshes preserve recording fields because they never name them."""
    from app.db.repositories.recording_links import (
        PERFECT_RECORDING_COLUMNS,
        SESSION_RECORDING_COLUMNS,
    )
    from app.writer.mapping import SESSION_COLUMNS
    from app.writer.perfect_mapping import CODED_OWNED_COLUMNS, RECORDING_OWNED_COLUMNS
    assert not set(SESSION_COLUMNS) & set(SESSION_RECORDING_COLUMNS)
    assert "recording_url" in RECORDING_OWNED_COLUMNS
    assert "recording_url" not in CODED_OWNED_COLUMNS
    assert set(PERFECT_RECORDING_COLUMNS) == {"recording_url", "meeting_id", "session_id"}


def test_run_pipeline_can_be_narrowed_to_one_canary_lecture():
    from app.cli.main import build_parser
    args = build_parser().parse_args(["run-pipeline", "--date", "2026-09-09",
                                      "--lecture-id", "d38dc566-bd76-5506-a284-8f4991bd3e7e"])
    assert args.lecture_ids == ["d38dc566-bd76-5506-a284-8f4991bd3e7e"]
