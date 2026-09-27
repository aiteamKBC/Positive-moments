"""
Full-recording resolution, offline.

Several files can pass the identity rule for one lecture. These tests pin how
each reason for that is answered: the same physical file seen twice is one
candidate; a short false start next to the delivered lecture yields the
lecture; two substantial parts are MULTIPART_RECORDING; two plausible full
recordings stay ambiguous. Every threshold is relative to the lecture's own
schedule - the tests use a 2 h slot, but nothing in the code knows that.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.orchestration.stages import (
    LEGACY_QA_SYNC,
    LINK_RECORDING,
    MANUAL_REVIEW_REQUIRED,
    MISSING,
    RECORDING_LINK,
    REVIEW_REQUIRED,
)
from app.recordings import models as m
from app.recordings import resolution as r
from app.recordings.candidates import candidate_from_drive_item, deduplicate
from app.recordings.drive_items import (
    CandidateMetadataReader,
    ChannelRecordingsFolder,
    DriveItemDiscovery,
    OneDriveRecordingsFolder,
    TenantRecordingSearch,
)
from app.recordings.graph_lookup import RecordingMetadataGateway
from app.recordings.service import RecordingLinkService
from tests.unit.recording_graph_fakes import FakeGraph, graph_error
from test_recording_links import (
    MEETING,
    ORGANIZER,
    SUBJECT,
    StaticDiscovery,
    StubPublisher,
    StubRepository,
    attempt,
    recording,
    resolve as resolve_stage,
    target as base_target,
)
from test_recording_links import REAL_CALL_ID, REAL_CANONICAL, LECTURE_ID

GRAPH = datetime(2026, 9, 11, 7, 57, 5, tzinfo=timezone.utc)
START = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
THREAD = "19:6125745a17464ca0801b13976816bde1@thread.tacv2"
MB = 1024 * 1024


def target(hours=2.0, **overrides):
    return base_target(scheduled_start=START,
                       scheduled_end=START + timedelta(hours=hours), **overrides)


def item(n, *, lead=30.0, minutes=None, size=None, graph=GRAPH, item_id=None,
         drive_id=None, subject=SUBJECT, extra=None):
    """A Teams recording driveItem named `lead` seconds before `graph`."""
    stamp = graph - timedelta(seconds=lead)
    value = {"id": item_id or f"item-{n}",
             "name": f"{subject}-{stamp:%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4",
             "file": {"mimeType": "video/mp4"},
             "webUrl": f"https://kbc.sharepoint.com/sites/x/r{n}.mp4",
             "parentReference": {"driveId": drive_id or f"drive-{n}", "id": "parent-1"},
             "createdDateTime": stamp.isoformat()}
    if minutes is not None:
        value["video"] = {"duration": int(minutes * 60_000)}
    if size is not None:
        value["size"] = size
    value.update(extra or {})
    return value


def graph_recording(rid, created, minutes=None):
    row = recording(rid=rid, created=created.isoformat().replace("+00:00", "Z"))
    if minutes is not None:
        row["endDateTime"] = (created + timedelta(minutes=minutes)).isoformat()
    return row


def service(items, *, recordings=None, reader=None, publisher=None, repository=None):
    graph = FakeGraph(collections={"/users/": recordings or [recording(
        created=GRAPH.isoformat().replace("+00:00", "Z"))]})
    return RecordingLinkService(
        repository=repository or StubRepository(target()),
        metadata_gateway=RecordingMetadataGateway(graph),
        discovery=StaticDiscovery(items), publisher=publisher, evidence_reader=reader)


def decide(items, hours=2.0, **kwargs):
    return service(items, **kwargs).decide(target(hours))


# ---------------------------------------------------------------------------
# 1-2. physical file identity: (drive_id, item_id), whoever found it
# ---------------------------------------------------------------------------

class _Fixed:
    def __init__(self, name, items):
        self.name, self._items, self.calls = name, items, 0

    def applicable(self, target_):
        return True

    def items(self, target_):
        self.calls += 1
        return self._items


def test_one_drive_item_seen_by_two_sources_is_one_candidate():
    same = item(0, minutes=124, size=900 * MB)
    discovered = DriveItemDiscovery([_Fixed("tenant_search", [same]),
                                     _Fixed("channel_recordings", [dict(same)])]
                                    ).discover(target())
    assert discovered.raw_candidate_count == 2
    [candidate] = discovered.candidates
    assert candidate.sources == ("tenant_search", "channel_recordings")
    assert candidate.duration_seconds == 124 * 60 and candidate.size_bytes == 900 * MB


def test_duplicate_discovery_merges_provenance_and_fills_missing_evidence():
    search_hit = candidate_from_drive_item(item(0, size=900 * MB), "tenant_search")
    listing = candidate_from_drive_item(item(0, minutes=124, size=900 * MB),
                                        "onedrive_recordings")
    [merged] = deduplicate([search_hit, listing])
    assert merged.sources == ("tenant_search", "onedrive_recordings")
    assert merged.duration_seconds == 124 * 60          # the listing had the facet


def test_the_same_file_from_search_and_the_channel_folder_is_not_ambiguous():
    """Real Graph sources, one physical file: EXACT, not AMBIGUOUS."""
    hit = item(0)
    graph = FakeGraph(
        collections={
            f"/users/{ORGANIZER}/onlineMeetings/": [recording(
                created=GRAPH.isoformat().replace("+00:00", "Z"))],
            f"/users/{ORGANIZER}/joinedTeams": [{"id": "team-1"}],
            "/teams/team-1/allChannels": [{"id": THREAD}],
            "/drives/drive-0/items/folder-1:/Recordings:/children": [dict(hit)],
        },
        json={f"/teams/team-1/channels/{THREAD.replace(':', '%3A').replace('@', '%40')}"
              "/filesFolder": {"id": "folder-1", "parentReference": {"driveId": "drive-0"}}},
        posts={"/search/query": {"value": [{"hitsContainers": [{
            "moreResultsAvailable": False, "hits": [{"resource": dict(hit)}]}]}]}})
    discovery = DriveItemDiscovery([TenantRecordingSearch(graph, region="GBR"),
                                    ChannelRecordingsFolder(graph)])
    decision = RecordingLinkService(
        repository=StubRepository(target()),
        metadata_gateway=RecordingMetadataGateway(graph),
        discovery=discovery).decide(target())
    assert decision.discovery_sources_failed == ()
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.match.exact_candidate_count == 1
    assert decision.match.candidate.sources == ("tenant_search", "channel_recordings")
    assert decision.attempt_detail()["raw_candidate_count"] == 2


def test_a_byte_identical_copy_under_another_item_id_is_one_logical_file():
    copy = decide([item(0, size=700 * MB, drive_id="drive-onedrive"),
                   item(1, size=700 * MB, drive_id="drive-channel")])
    assert copy.status == m.EXACT_RECORDING_FILE_MATCHED
    assert copy.match.resolution["logical_copies_merged"] == 1
    # Deterministic: never the discovery order.
    again = decide([item(1, size=700 * MB, drive_id="drive-channel"),
                    item(0, size=700 * MB, drive_id="drive-onedrive")])
    assert again.match.candidate.item_id == copy.match.candidate.item_id


def test_equal_names_alone_never_merge_two_files():
    different = decide([item(0, size=700 * MB), item(1, size=650 * MB)])
    assert different.status == m.AMBIGUOUS_RECORDING_FILES
    no_size = decide([item(0), item(1)])
    assert no_size.status == m.AMBIGUOUS_RECORDING_FILES
    same_size_other_media = decide([item(0, size=700 * MB, minutes=110),
                                    item(1, size=700 * MB, minutes=3)])
    assert same_size_other_media.match.resolution["logical_copies_merged"] == 0


# ---------------------------------------------------------------------------
# 3-6. duration against the lecture's own schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("short, full", [(3, 124), (12, 118), (1, 95), (29, 61)])
def test_a_short_false_start_yields_the_full_lecture(short, full):
    decision = decide([item(0, lead=20, minutes=short, size=20 * MB),
                       item(1, lead=80, minutes=full, size=900 * MB)])
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.match.candidate.item_id == "item-1"
    assert decision.match.timestamp_difference_seconds == 80
    assert decision.would_write is True and decision.stage_state == m.STATE_READY
    resolution = decision.match.resolution
    assert resolution["rule"] == r.RULE_FULL_OVER_FRAGMENT
    assert resolution["policy"] == m.RESOLUTION_POLICY_VERSION
    assert resolution["expected_duration_seconds"] == 7200
    assert resolution["exact_candidate_count"] == 2
    assert resolution["selected"]["duration_seconds"] == full * 60
    assert [x["duration_seconds"] for x in resolution["rejected"]] == [short * 60]
    assert r.RULE_FULL_OVER_FRAGMENT in decision.reason


def test_the_rule_is_relative_to_the_schedule_not_to_two_hours():
    """A 40-min recording is the whole of a 45-min slot, but only part of a 3 h one."""
    items = [item(0, lead=20, minutes=4), item(1, lead=80, minutes=40)]
    assert decide(items, hours=0.75).status == m.EXACT_RECORDING_FILE_MATCHED
    assert decide(items, hours=3).status == m.AMBIGUOUS_RECORDING_FILES


def test_two_plausible_full_recordings_stay_ambiguous():
    decision = decide([item(0, lead=20, minutes=105, size=800 * MB),
                       item(1, lead=80, minutes=112, size=850 * MB)])
    assert decision.status == m.AMBIGUOUS_RECORDING_FILES
    assert decision.stage_state == m.STATE_REVIEW
    assert decision.match.candidate is None and decision.would_write is False
    assert decision.match.resolution["rule"] == r.RULE_SEVERAL_FULL


def test_two_substantial_parts_are_multipart_never_the_largest():
    decision = decide([item(0, lead=20, minutes=55, size=400 * MB),
                       item(1, lead=80, minutes=65, size=480 * MB)])
    assert decision.status == m.MULTIPART_RECORDING
    assert decision.stage_state == m.STATE_REVIEW
    assert m.MULTIPART_RECORDING in m.REVIEW_STATUSES
    assert decision.match.candidate is None
    assert decision.match.resolution["rule"] == r.RULE_MULTIPART


def test_multipart_and_ambiguity_never_publish_or_write():
    for items in ([item(0, minutes=55), item(1, lead=80, minutes=65)],
                  [item(0, minutes=105), item(1, lead=80, minutes=112)]):
        repository, publisher = StubRepository(target()), StubPublisher()
        outcome = service(items, repository=repository, publisher=publisher).link(
            None, LECTURE_ID, REAL_CANONICAL, write=True)
        assert outcome["written"] is False
        assert publisher.calls == 0 and repository.writes == []


def test_a_short_file_and_a_partial_one_is_not_a_full_lecture():
    decision = decide([item(0, lead=20, minutes=3), item(1, lead=80, minutes=45)])
    assert decision.status == m.AMBIGUOUS_RECORDING_FILES
    assert decision.match.resolution["rule"] == r.RULE_NO_FULL


def test_one_exact_candidate_is_unchanged_whatever_its_length():
    reader = CandidateMetadataReader(FakeGraph())
    decision = decide([item(0, lead=20, minutes=3)], reader=reader)
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.match.resolution["rule"] == r.RULE_SINGLE
    assert reader.calls == 0                       # no evidence fetched for one file
    assert "single_exact_candidate" not in decision.reason


def test_a_file_named_after_graph_is_still_rejected_before_resolution():
    decision = decide([item(0, lead=20, minutes=3), item(1, lead=-30, minutes=124)])
    assert decision.match.exact_candidate_count == 1
    assert decision.match.candidate.item_id == "item-0"


def test_an_existing_url_short_circuits_before_any_resolution():
    graph = FakeGraph(collections={"/users/": [recording()]})
    discovery = StaticDiscovery([item(0, minutes=3), item(1, lead=80, minutes=124)])
    repository, publisher = StubRepository(target(existing_recording_url="https://old")), \
        StubPublisher()
    outcome = RecordingLinkService(repository=repository,
                                   metadata_gateway=RecordingMetadataGateway(graph),
                                   discovery=discovery, publisher=publisher).link(
        None, LECTURE_ID, REAL_CANONICAL, write=True)
    assert outcome["status"] == m.RECORDING_ALREADY_LINKED
    assert graph.paths == [] and discovery.calls == 0
    assert publisher.calls == 0 and repository.writes == []


def test_a_cancelled_lecture_costs_nothing_even_with_several_files():
    graph = FakeGraph(collections={"/users/": [recording()]})
    discovery = StaticDiscovery([item(0, minutes=3), item(1, lead=80, minutes=124)])
    decision = RecordingLinkService(
        repository=None, metadata_gateway=RecordingMetadataGateway(graph),
        discovery=discovery).decide(target(cancelled=True,
                                           cancelled_reason="LECTURE_CANCELLED"))
    assert decision.status == m.NO_RECORDING_EXPECTED_CANCELLED
    assert decision.stage_state == m.STATE_NOT_APPLICABLE
    assert graph.paths == [] and discovery.calls == 0


# ---------------------------------------------------------------------------
# size-only fallback: conservative dominance, never max(size)
# ---------------------------------------------------------------------------

def test_without_durations_a_dominant_file_wins_only_by_a_wide_margin():
    won = decide([item(0, lead=20, size=25 * MB), item(1, lead=80, size=900 * MB)])
    assert won.status == m.EXACT_RECORDING_FILE_MATCHED
    assert won.match.candidate.item_id == "item-1"
    assert won.match.resolution["rule"] == r.RULE_SIZE_OVER_FRAGMENT
    close = decide([item(0, lead=20, size=400 * MB), item(1, lead=80, size=500 * MB)])
    assert close.status == m.AMBIGUOUS_RECORDING_FILES
    assert close.match.resolution["rule"] == r.RULE_NO_EVIDENCE
    just_under = decide([item(0, lead=20, size=91 * MB), item(1, lead=80, size=900 * MB)])
    assert just_under.status == m.AMBIGUOUS_RECORDING_FILES


def test_size_dominance_never_overrides_a_known_duration():
    decision = decide([item(0, lead=20, size=10 * MB, minutes=55),
                       item(1, lead=80, size=900 * MB)])
    assert decision.status == m.AMBIGUOUS_RECORDING_FILES


def test_size_dominance_needs_the_single_graph_recording_to_be_full_length():
    short_call = [graph_recording("rec-1", GRAPH, minutes=20)]
    decision = decide([item(0, lead=20, size=5 * MB), item(1, lead=80, size=900 * MB)],
                      recordings=short_call)
    assert decision.status == m.AMBIGUOUS_RECORDING_FILES


def test_no_size_and_no_duration_is_never_resolved():
    assert decide([item(0), item(1, lead=80)]).status == m.AMBIGUOUS_RECORDING_FILES


# ---------------------------------------------------------------------------
# media evidence for the final candidates only, server-side
# ---------------------------------------------------------------------------

def evidence_path(n):
    return (f"/drives/drive-{n}/items/item-{n}?$select="
            "id,name,webUrl,parentReference,createdDateTime,lastModifiedDateTime,"
            "file,size,video")


def test_media_evidence_is_read_for_the_exact_candidates_only():
    short, full = item(0, lead=20), item(1, lead=80)
    elsewhere = item(2, subject="Another Lecture")
    graph = FakeGraph(json={evidence_path(0): item(0, lead=20, minutes=3, size=20 * MB),
                            evidence_path(1): item(1, lead=80, minutes=124, size=900 * MB)})
    reader = CandidateMetadataReader(graph)
    decision = decide([short, full, elsewhere], reader=reader)
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.match.candidate.item_id == "item-1"
    assert reader.calls == 2
    assert sorted(path for _, path in graph.paths) == [evidence_path(0), evidence_path(1)]
    assert all("content" not in path and "download" not in path.lower()
               for _, path in graph.paths)                      # never the MP4


def test_a_metadata_read_failure_is_a_bounded_retry_not_a_review():
    reader = CandidateMetadataReader(FakeGraph(errors={"/drives/": graph_error(429)}))
    decision = decide([item(0, lead=20), item(1, lead=80)], reader=reader)
    assert decision.status == m.DRIVE_ITEM_DISCOVERY_FAILED
    assert m.DRIVE_ITEM_DISCOVERY_FAILED in m.RETRYABLE_STATUSES
    assert "candidate_metadata:429" in decision.reason
    assert decision.would_write is False


def test_no_download_url_or_secret_is_kept_or_persisted():
    leaky = {"@microsoft.graph.downloadUrl": "https://kbc.sharepoint.com/download?tempauth=SECRET",
             "tempauth": "SECRET"}
    decision = decide([item(0, lead=20, minutes=3, extra=leaky),
                       item(1, lead=80, minutes=124, extra=leaky)])
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    persisted = json.dumps(decision.attempt_detail(), default=str)
    shown = json.dumps(decision.preview_row(), default=str)
    for text in (persisted, shown):
        assert "SECRET" not in text and "tempauth" not in text
        assert "https://" not in text and "downloadUrl" not in text
    assert not hasattr(decision.match.candidate, "download_url")
    evidence = decision.preview_row()["resolution_evidence"]
    assert evidence["selected"]["duration_seconds"] == 124 * 60
    assert "Meeting Recording" not in json.dumps(evidence)          # no file names shown


# ---------------------------------------------------------------------------
# several Graph recordings for one call id
# ---------------------------------------------------------------------------

def test_a_graph_false_start_and_the_real_call_resolve_to_the_real_call():
    false_start, real = GRAPH, GRAPH + timedelta(minutes=4)
    recordings = [graph_recording("rec-a", false_start, minutes=2),
                  graph_recording("rec-b", real, minutes=124)]
    decision = decide([item(0, lead=20, graph=false_start),
                       item(1, lead=30, graph=real)], recordings=recordings)
    assert decision.graph.status == m.GRAPH_RECORDING_AMBIGUOUS
    assert decision.graph.call_id_matches == 2
    assert decision.status == m.EXACT_RECORDING_FILE_MATCHED
    assert decision.match.candidate.item_id == "item-1"
    resolution = decision.match.resolution
    assert resolution["rule"] == r.RULE_FULL_GRAPH_OVER_FRAGMENT
    assert resolution["selected"]["duration_seconds"] == 124 * 60
    assert resolution["selected"]["graph_recording_id"] == "rec-b"


def test_two_full_graph_recordings_for_one_call_stay_ambiguous():
    recordings = [graph_recording("rec-a", GRAPH, minutes=110),
                  graph_recording("rec-b", GRAPH + timedelta(minutes=3), minutes=115)]
    decision = decide([item(0, lead=20), item(1, lead=20, graph=GRAPH + timedelta(minutes=3))],
                      recordings=recordings)
    assert decision.status == m.GRAPH_RECORDING_AMBIGUOUS
    assert decision.stage_state == m.STATE_REVIEW and decision.would_write is False


def test_an_interrupted_call_is_multipart():
    second = GRAPH + timedelta(minutes=60)
    recordings = [graph_recording("rec-a", GRAPH, minutes=55),
                  graph_recording("rec-b", second, minutes=65)]
    decision = decide([item(0, lead=20), item(1, lead=20, graph=second)],
                      recordings=recordings)
    assert decision.status == m.MULTIPART_RECORDING
    assert decision.match.candidate is None


def test_only_the_fragment_file_visible_is_a_retry_never_the_fragment():
    real = GRAPH + timedelta(minutes=4)
    recordings = [graph_recording("rec-a", GRAPH, minutes=2),
                  graph_recording("rec-b", real, minutes=124)]
    decision = decide([item(0, lead=20)], recordings=recordings)
    assert decision.status == m.RECORDING_FILE_NOT_FOUND
    assert m.RECORDING_FILE_NOT_FOUND in m.RETRYABLE_STATUSES
    assert decision.would_write is False


def test_graph_recordings_without_end_times_fall_back_to_file_durations():
    real = GRAPH + timedelta(minutes=4)
    recordings = [graph_recording("rec-a", GRAPH), graph_recording("rec-b", real)]
    won = decide([item(0, lead=20, minutes=2), item(1, lead=30, minutes=124, graph=real)],
                 recordings=recordings)
    assert won.status == m.EXACT_RECORDING_FILE_MATCHED
    assert won.match.candidate.item_id == "item-1"
    one_file = decide([item(1, lead=30, minutes=124, graph=real)], recordings=recordings)
    assert one_file.status == m.GRAPH_RECORDING_AMBIGUOUS       # rec-a may be the lecture
    assert one_file.match.resolution["rule"] == r.RULE_GRAPH_WITHOUT_FILE


def test_duplicate_graph_metadata_is_one_recording():
    created = GRAPH.isoformat().replace("+00:00", "Z")
    lookup = RecordingMetadataGateway(FakeGraph(collections={"/users/": [
        recording(rid="a", created=created), recording(rid="b", created=created)]})
    ).lookup(meeting_lookup_user_id=ORGANIZER, meeting_id=MEETING, call_id=REAL_CALL_ID)
    assert lookup.status == m.GRAPH_RECORDING_FOUND
    assert lookup.call_id_matches == 2 and len(lookup.recordings) == 1


def test_same_call_recordings_are_carried_with_their_end_times():
    lookup = RecordingMetadataGateway(FakeGraph(collections={"/users/": [
        graph_recording("a", GRAPH, minutes=3),
        graph_recording("b", GRAPH + timedelta(minutes=5), minutes=120)]})
    ).lookup(meeting_lookup_user_id=ORGANIZER, meeting_id=MEETING, call_id=REAL_CALL_ID)
    assert lookup.status == m.GRAPH_RECORDING_AMBIGUOUS
    assert [x.duration_seconds for x in lookup.recordings] == [180, 7200]
    assert lookup.created_at is None and lookup.recording_id is None


# ---------------------------------------------------------------------------
# the policy itself
# ---------------------------------------------------------------------------

def test_thresholds_are_validated_and_have_safe_defaults():
    policy = r.ResolutionPolicy()
    assert policy.thresholds() == {"full_min_expected_ratio": 0.5,
                                   "fragment_max_expected_ratio": 0.25,
                                   "size_dominance_ratio": 10.0}
    for bad in ({"fragment_max_expected_ratio": 0.6}, {"full_min_expected_ratio": 1.5},
                {"size_dominance_ratio": 1.0}):
        with pytest.raises(ValueError):
            r.ResolutionPolicy(**bad)


def test_settings_thresholds_parse_from_the_environment(monkeypatch):
    from app.config import settings as s
    monkeypatch.setenv("RECORDING_SIZE_DOMINANCE_RATIO", "12")
    assert s._ratio("RECORDING_SIZE_DOMINANCE_RATIO", 10.0) == 12.0
    monkeypatch.setenv("RECORDING_SIZE_DOMINANCE_RATIO", "")
    assert s._ratio("RECORDING_SIZE_DOMINANCE_RATIO", 10.0) == 10.0
    for bad in ("zero", "-1", "0"):
        monkeypatch.setenv("RECORDING_SIZE_DOMINANCE_RATIO", bad)
        with pytest.raises(ValueError):
            s._ratio("RECORDING_SIZE_DOMINANCE_RATIO", 10.0)


def test_a_configured_policy_reaches_the_service():
    from app.config.settings import Settings
    from app.recordings.factory import build_recording_link_service
    settings = Settings(database_url="", aptem_database_url="", graph_tenant_id="",
                        graph_client_id="", graph_client_secret="", graph_scope="",
                        graph_base_url="", calendar_user_upn="",
                        recording_short_fragment_max_expected_ratio=0.05)
    built = build_recording_link_service(settings, graph=FakeGraph())
    assert built.policy.fragment_max_expected_ratio == 0.05
    assert isinstance(built.evidence_reader, CandidateMetadataReader)
    # 12 minutes is no longer a fragment of a 2 h lecture under that policy.
    built.metadata = RecordingMetadataGateway(FakeGraph(collections={"/users/": [
        recording(created=GRAPH.isoformat().replace("+00:00", "Z"))]}))
    built.discovery = StaticDiscovery([item(0, lead=20, minutes=12),
                                       item(1, lead=80, minutes=118)])
    assert built.decide(target()).status == m.MULTIPART_RECORDING


def test_missing_schedule_never_classifies_durations():
    decision = service([item(0, minutes=3), item(1, lead=80, minutes=124)]).decide(
        base_target())
    assert decision.status == m.AMBIGUOUS_RECORDING_FILES


# ---------------------------------------------------------------------------
# 11-12. the pipeline: stage guards kept, old ambiguity re-evaluated once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", sorted(m.REEVALUABLE_REVIEW_STATUSES))
@pytest.mark.parametrize("policy", [None, "recording_match_v1"])
def test_an_ambiguity_under_an_older_policy_is_offered_again(status, policy):
    result, stage, _ = resolve_stage(attempt=attempt(REVIEW_REQUIRED, status, policy=policy))
    assert (stage["state"], stage["action"]) == (MISSING, LINK_RECORDING)
    assert stage["reason"] == "RECORDING_RESOLUTION_POLICY_CHANGED"
    assert result["next_executable_action"] == LINK_RECORDING


@pytest.mark.parametrize("status", sorted(m.REEVALUABLE_REVIEW_STATUSES))
def test_the_same_ambiguity_under_the_current_policy_stays_in_review(status):
    _, stage, _ = resolve_stage(attempt=attempt(REVIEW_REQUIRED, status,
                                                policy=m.RESOLUTION_POLICY_VERSION))
    assert (stage["state"], stage["action"]) == (REVIEW_REQUIRED, MANUAL_REVIEW_REQUIRED)


@pytest.mark.parametrize("status", [m.TIMESTAMP_MISMATCH, m.ORGANIZER_LOOKUP_ID_MISSING,
                                    m.LECTURE_IDENTITY_MISMATCH])
def test_other_reviews_are_not_reopened_by_a_policy_change(status):
    _, stage, _ = resolve_stage(attempt=attempt(REVIEW_REQUIRED, status, policy=None))
    assert stage["state"] == REVIEW_REQUIRED


def test_every_evaluation_records_the_current_policy():
    for items in ([item(0, minutes=3), item(1, lead=80, minutes=124)],
                  [item(0, minutes=105), item(1, lead=80, minutes=112)], []):
        assert decide(items).attempt_detail()["resolution_policy"] == \
            m.RESOLUTION_POLICY_VERSION


def test_an_earlier_unfinished_stage_still_runs_first():
    """
    The Andrew-Scheduling pattern: a legacy row exists and the recording would
    resolve, but LEGACY_QA_SYNC has not settled. RECORDING_LINK never jumps it.
    """
    from test_pipeline_state import (
        EVALUATION_ID, LEGACY_SESSION_ID, NOW as STATE_NOW, WRITE_ID, WRITER_VERSION,
        StubConnection, complete_rows)
    from test_recording_links import NOW, Attempts, Observations
    from app.orchestration.orchestrator import PipelineOrchestrator  # noqa: F401
    from app.orchestration.stages import SYNC_LEGACY_QA
    from app.orchestration.state import PipelineStateResolver
    resolver = PipelineStateResolver(
        legacy_observations=Observations(excel_synced_at=NOW),
        recording_link_mode="write", recording_links=Attempts(None), clock=lambda: NOW)
    state = resolver.for_lecture(StubConnection(complete_rows(legacy_writes_state=[(
        WRITE_ID, "older-render", EVALUATION_ID, "WRITTEN", WRITER_VERSION,
        LEGACY_SESSION_ID, STATE_NOW)])), LECTURE_ID)
    assert state["stages"][RECORDING_LINK]["action"] == LINK_RECORDING
    assert state["executable_stage"] == LEGACY_QA_SYNC
    assert state["next_executable_action"] == SYNC_LEGACY_QA
