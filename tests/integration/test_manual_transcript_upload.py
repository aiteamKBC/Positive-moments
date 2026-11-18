"""
RELEASE GATE: manual transcript upload, end to end, on the isolated test database.

The situation it exists for (November 2026): Teams lists a channel meeting's transcript, Graph
refuses its content with 400 "Invalid transcript id", and the lecture stops at TRANSCRIPT. An
operator downloads the .vtt from Teams and uploads it on the lecture's page.

Real code throughout - ManualTranscriptUploadService, TranscriptArtifactRepository, the selector,
the combiner, the canonical parser, the speaker inventory, attendance, engagement, QA, rendering,
the legacy writer, and the real PipelineOrchestrator narrowed to the lecture. Only what would leave
the machine is a stub: the model provider (counted), the n8n precheck, and Graph (a fake gateway
that answers 400 the way Graph does). Every row is synthetic and every connection rolls back.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.common.hashing import content_sha256, provider_transcript_identity
from app.config.settings import Settings
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.pipeline_runs import PipelineRunRepository
from app.db.repositories.ready_lectures import ReadyLectureRepository
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.db.repositories.transcript_runs import TranscriptAcquisitionRunRepository
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository, TranscriptSelectionRunRepository,
)
from app.transcripts.selection_service import TranscriptSelectionService
from app.orchestration import runner as runner_module
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import StageRunner
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT, COMPLETE, RUN_QA, SELECTION, TRANSCRIPT,
)
from app.orchestration.state import PipelineStateResolver
from app.orchestration.transcript_recovery import (
    NO_MANUAL_UPLOAD, NOTHING_TO_PROCESS, WAITING_FOR_TRANSCRIPT_PARTS, ManualTranscriptRecovery,
    ProcessingRefused,
)
from app.transcripts import manual_upload as mu
from app.transcripts.manual_upload import ManualTranscriptUploadService, UploadRefused
from app.transcripts.models import (
    CONTENT_FETCH_ERROR, NO_TRANSCRIPTS_AVAILABLE, PROVIDER_ERROR, PROVIDER_MICROSOFT_GRAPH,
    TRANSCRIPTS_FOUND, TranscriptArtifact, TranscriptContent, TranscriptListing,
)
from app.transcripts.selection import SELECTION_VERSION
from app.transcripts.service import TranscriptAcquisitionService
from tests.integration.seeding import DAY, START, TRAINER, graph_id, insert, webvtt
from tests.integration.test_attendance_optional_qa_persistence import Provider
from tools.integration_db_guard import assert_isolated


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        assert_isolated(connection)
        yield connection
    finally:
        connection.rollback()
        connection.close()


class CountingProvider(Provider):
    """The only model in the building. Every call is counted; none leaves the machine."""

    calls_total = 0
    response_contract = None

    def __init__(self, **_configuration):
        super().__init__()

    def complete_json(self, **kwargs):
        CountingProvider.calls_total += 1
        return super().complete_json(**kwargs)


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    CountingProvider.calls_total = 0
    monkeypatch.setattr(runner_module, "OpenAIChatProvider", CountingProvider)
    return CountingProvider


class StubPreflight:
    def check(self):
        return {"status": "LEGACY_QA_DISABLED", "legacy_qa_node_disabled": True,
                "reason": "LEGACY_QA_DISABLED", "writes_permitted": True,
                "read_only": True, "http_method": "GET", "n8n_modified": False}


def _recovery(*, allow_provider=True):
    """
    The production wiring of build_manual_transcript_service, with the three stubs.
    allow_provider=True is the server CLI; False is the web console.
    """
    settings = dataclasses.replace(Settings.from_environment(), qa_model_api_key="not-a-real-key",
                                   qa_model_base_url="https://provider.invalid/v1")
    resolver = PipelineStateResolver(legacy_observations=LegacyObservationRepository())

    def factory(lecture_id):
        return PipelineOrchestrator(
            resolver=resolver,
            runner=StageRunner(settings=settings, allow_graph=False, allow_provider=allow_provider,
                               lecture_scope=[lecture_id]),
            run_repository=PipelineRunRepository(), preflight=StubPreflight())

    return ManualTranscriptRecovery(uploads=ManualTranscriptUploadService(), resolver=resolver,
                                    orchestrator_factory=factory, allow_provider=allow_provider)


# --- synthetic lectures, exactly as Graph leaves them -------------------------------------------

SUBJECT = "Synthetic Channel Lecture"
LEARNERS = ("Synthetic Learner 1", "Synthetic Learner 2")


def _cues(span_ms=7_200_000, offset=0):
    """Trainer speech across the span, two learners once each. Nine minutes in every ten."""
    cues = [(m, min(m + 540_000, span_ms), TRAINER, f"Synthetic line {offset + m // 60_000}.")
            for m in range(0, span_ms - 60_000, 600_000)]
    for index, name in enumerate(LEARNERS):
        start = 300_000 + index * 1_200_000
        cues.append((start, start + 20_000, name, f"Synthetic question {offset + index}."))
    return sorted(cues)


def _vtt(span_ms=7_200_000, offset=0) -> bytes:
    return webvtt(_cues(span_ms, offset)).encode("utf-8")


def _lecture(db, n, *, parts=((0, 120),), start=START, status=CONTENT_FETCH_ERROR, subject=SUBJECT,
             lms=True):
    """
    One lecture with Graph-listed transcript part(s) and NO content: the 400 case.
    `parts` are (start minute, end minute) of each Teams transcript relative to the lecture.
    Artifact ids are the provider identity, exactly as Phase 2A writes them.
    """
    lecture_id = uuid.uuid4()
    meeting_id = f"MTG-MANUAL-{n}-{uuid.uuid4().hex[:6]}"
    insert(db, "lecture_sessions", lecture_id=lecture_id, calendar_user_upn="calendar@example.invalid",
           calendar_event_id=f"evt-{lecture_id}", join_url=f"https://example.invalid/{lecture_id}",
           subject=subject, normalized_subject=subject.lower(), module=subject,
           scheduled_start=start, scheduled_end=start + timedelta(hours=2), session_date=DAY,
           calendar_mapping_status="RESOLVED", group_match_status="MATCHED", discovery_status="READY",
           meeting_id=meeting_id, downstream_ready=True, meeting_lookup_user_id="organizer",
           meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID")
    artifacts = []
    for index, (begin, end) in enumerate(parts):
        transcript_id = graph_id(7000 + n * 10 + index)
        artifact_id = provider_transcript_identity(provider=PROVIDER_MICROSOFT_GRAPH,
                                                   provider_transcript_id=transcript_id)
        insert(db, "lecture_transcript_artifacts", artifact_id=artifact_id,
               provider_transcript_id=transcript_id, meeting_id=meeting_id,
               meeting_lookup_user_id="organizer", artifact_status=status,
               provider_created_at=start + timedelta(minutes=begin),
               provider_end_at=start + timedelta(minutes=end),
               metadata={"content_fetch_reason": PROVIDER_ERROR})
        insert(db, "lecture_transcript_candidates", candidate_id=uuid.uuid4(), lecture_id=lecture_id,
               artifact_id=artifact_id, meeting_id=meeting_id, meeting_lookup_user_id="organizer")
        artifacts.append({"artifact_id": str(artifact_id), "transcript_id": transcript_id,
                          "start": start + timedelta(minutes=begin)})
    if lms:
        for student in range(2):
            insert(db, "kbc_users_data", ID=str(770000 + n * 10 + student),
                   FullName=f"Synthetic Student {n}-{student}", Group=subject,
                   **{"Program-Status": "Active"})
    return {"lecture_id": str(lecture_id), "meeting_id": meeting_id, "artifacts": artifacts,
            "subject": subject}


def _store_graph_content(db, artifact_id, raw: bytes):
    TranscriptArtifactRepository().store_content(
        db, artifact_id, raw_text=raw.decode(), content_sha256=content_sha256(raw),
        content_bytes=len(raw), content_format="text/vtt", speaker_attribution=True)


def _upload(db, lecture, raw, *, artifact_id=None, filename="Teams transcript.vtt", user="ops.user"):
    """Preview, then confirm exactly what the preview showed - the console's two steps."""
    service = ManualTranscriptUploadService()
    preview = service.preview(db, lecture["lecture_id"], filename=filename, raw=raw,
                              artifact_id=artifact_id)
    assert preview["ok"], preview["errors"]
    return service.commit(db, lecture["lecture_id"], filename=filename, raw=raw,
                          artifact_id=preview["part"]["artifact_id"],
                          expected_sha256=preview["file"]["content_sha256"], uploaded_by=user)


def _artifact(db, artifact_id):
    return db.execute("""
        SELECT artifact_status, content_version, content_sha256, content_fetched_at,
               (SELECT count(*) FROM public.lecture_transcript_artifact_contents v
                 WHERE v.artifact_id = a.artifact_id)
          FROM public.lecture_transcript_artifacts a WHERE artifact_id = %s""",
                      (artifact_id,)).fetchone()


def _provenance(db, lecture, artifact_id):
    return db.execute("SELECT metadata -> 'manual_upload' FROM public.lecture_transcript_candidates"
                      " WHERE lecture_id = %s AND artifact_id = %s",
                      (lecture["lecture_id"], artifact_id)).fetchone()[0]


def _selection(db, lecture):
    return db.execute("""
        SELECT s.selection_status, s.selected_part_count, cb.parts_combined, cb.content_sha256
          FROM public.lecture_transcript_selections s
          LEFT JOIN public.lecture_combined_transcripts cb ON cb.selection_id = s.selection_id
         WHERE s.lecture_id = %s AND s.selection_version = %s""",
                      (lecture["lecture_id"], SELECTION_VERSION)).fetchone()


def _stage(lecture, name):
    resolver = PipelineStateResolver(legacy_observations=LegacyObservationRepository())
    return lambda db: resolver.for_lecture(db, lecture["lecture_id"])["stages"][name]


def _refusal(call):
    with pytest.raises(UploadRefused) as caught:
        call()
    return [item["code"] for item in caught.value.problems]


# =================================================================================================
# 1. a valid Teams VTT
# =================================================================================================

def test_1_a_valid_teams_vtt_is_stored_on_the_graph_artifact_with_provenance(db):
    lecture = _lecture(db, 1)
    (part,) = lecture["artifacts"]
    assert _stage(lecture, TRANSCRIPT)(db)["state"] == "FAILED"          # the 400 situation
    raw = _vtt()

    result = _upload(db, lecture, raw)

    assert result["status"] == "STORED" and result["stored"] is True
    status, version, sha, fetched_at, versions = _artifact(db, part["artifact_id"])
    assert (status, version, sha, versions) == ("CONTENT_STORED", 1, content_sha256(raw), 1)
    stored = db.execute("SELECT raw_content FROM public.lecture_transcript_artifact_contents"
                        " WHERE artifact_id = %s", (part["artifact_id"],)).fetchone()[0]
    assert stored == raw.decode()                                         # verbatim, hash-verifiable
    provenance = _provenance(db, lecture, part["artifact_id"])
    assert provenance["source"] == "MANUAL_UPLOAD"
    assert provenance["lecture_id"] == lecture["lecture_id"]
    assert provenance["artifact_id"] == part["artifact_id"]
    assert provenance["provider_transcript_id"] == part["transcript_id"]
    assert provenance["content_sha256"] == content_sha256(raw) and provenance["content_version"] == 1
    assert provenance["uploaded_by"] == "ops.user" and provenance["uploaded_at"]
    assert provenance["original_filename"] == "Teams transcript.vtt"
    assert provenance["processing"] == {"status": "NOT_STARTED"}
    assert "Synthetic line" not in str(provenance)                       # never transcript text
    # The lecture's own selection was re-run: the transcript is now SELECTED and combined.
    assert _selection(db, lecture)[:3] == ("SELECTED", 1, 1)
    assert _stage(lecture, TRANSCRIPT)(db)["state"] == COMPLETE
    assert _stage(lecture, SELECTION)(db)["state"] == COMPLETE


# =================================================================================================
# 2. invalid or empty
# =================================================================================================

@pytest.mark.parametrize("filename, raw, code", [
    ("t.vtt", b"", mu.EMPTY_FILE),
    ("t.vtt", b"WEBVTT\n\n", mu.NO_CUES),
    ("t.vtt", b"not a transcript at all", mu.INVALID_WEBVTT_FILE),
    ("t.txt", None, mu.NOT_A_VTT_FILE),
], ids=["empty", "no-cues", "not-webvtt", "not-vtt"])
def test_2_an_invalid_or_empty_file_is_refused_and_nothing_is_written(db, filename, raw, code):
    lecture = _lecture(db, 2)
    raw = _vtt() if raw is None else raw
    service = ManualTranscriptUploadService()
    preview = service.preview(db, lecture["lecture_id"], filename=filename, raw=raw)
    assert not preview["ok"] and code in [item["code"] for item in preview["errors"]]
    artifact_id = lecture["artifacts"][0]["artifact_id"]
    assert code in _refusal(lambda: service.commit(
        db, lecture["lecture_id"], filename=filename, raw=raw, artifact_id=artifact_id,
        expected_sha256=content_sha256(raw), uploaded_by="ops.user"))
    assert _artifact(db, artifact_id)[1:3] == (0, None)
    assert _provenance(db, lecture, artifact_id) is None


# =================================================================================================
# 3. wrong lecture or meeting
# =================================================================================================

def test_3a_a_part_belonging_to_another_lecture_is_refused(db):
    mine, other = _lecture(db, 31), _lecture(db, 32, start=START + timedelta(hours=3))
    raw = _vtt()
    foreign = other["artifacts"][0]["artifact_id"]
    preview = ManualTranscriptUploadService().preview(db, mine["lecture_id"], filename="t.vtt",
                                                      raw=raw, artifact_id=foreign)
    assert [item["code"] for item in preview["errors"]][0] == mu.PART_NOT_IN_THIS_LECTURE
    assert mu.PART_NOT_IN_THIS_LECTURE in _refusal(lambda: ManualTranscriptUploadService().commit(
        db, mine["lecture_id"], filename="t.vtt", raw=raw, artifact_id=foreign,
        expected_sha256=content_sha256(raw), uploaded_by="x"))
    assert _artifact(db, foreign)[1] == 0


def test_3b_another_lectures_transcript_is_recognised_by_its_bytes(db):
    mine, other = _lecture(db, 33), _lecture(db, 34, start=START + timedelta(hours=3))
    theirs = _vtt(offset=1000)
    _store_graph_content(db, other["artifacts"][0]["artifact_id"], theirs)
    preview = ManualTranscriptUploadService().preview(db, mine["lecture_id"], filename="t.vtt", raw=theirs)
    (error,) = preview["errors"]
    assert error["code"] == mu.CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT
    assert error["lectures"][0]["lecture_id"] == other["lecture_id"]


def test_3c_a_transcript_longer_than_the_teams_transcript_is_refused(db):
    lecture = _lecture(db, 35, parts=((0, 60),))                   # Teams says one hour
    preview = ManualTranscriptUploadService().preview(db, lecture["lecture_id"], filename="t.vtt",
                                                      raw=_vtt(span_ms=7_200_000))
    assert mu.LONGER_THAN_TEAMS_TRANSCRIPT in [item["code"] for item in preview["errors"]]


def test_3d_a_lecture_with_no_teams_transcript_for_its_occurrence_takes_no_upload(db):
    # Graph listed the series' transcript of ANOTHER day only.
    lecture = _lecture(db, 36, parts=((-7 * 24 * 60, -7 * 24 * 60 + 120),))
    service = ManualTranscriptUploadService()
    target = service.target(db, lecture["lecture_id"])
    assert target["upload_allowed"] is False
    assert target["refusal"]["code"] == mu.NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE
    preview = service.preview(db, lecture["lecture_id"], filename="t.vtt", raw=_vtt())
    assert preview["errors"][0]["code"] == mu.NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE


def test_3e_the_request_never_supplies_identity_and_a_stale_confirmation_is_refused(db):
    lecture = _lecture(db, 37)
    service = ManualTranscriptUploadService()
    raw = _vtt()
    part = lecture["artifacts"][0]["artifact_id"]
    # The confirmation must carry the previewed file's hash: a different file is refused.
    assert mu.FILE_CHANGED_SINCE_PREVIEW in _refusal(lambda: service.commit(
        db, lecture["lecture_id"], filename="t.vtt", raw=raw, artifact_id=part,
        expected_sha256="0" * 64, uploaded_by="x"))
    # ...and must name the part the preview showed: it is never inferred at commit.
    assert mu.PART_REQUIRED in _refusal(lambda: service.commit(
        db, lecture["lecture_id"], filename="t.vtt", raw=raw, artifact_id=None,
        expected_sha256=content_sha256(raw), uploaded_by="x"))
    assert mu.LECTURE_NOT_FOUND in _refusal(lambda: service.preview(
        db, str(uuid.uuid4()), filename="t.vtt", raw=raw))
    db.execute("UPDATE public.lecture_sessions SET is_cancelled = true WHERE lecture_id = %s",
               (lecture["lecture_id"],))
    assert service.preview(db, lecture["lecture_id"], filename="t.vtt", raw=raw)["errors"][0]["code"] \
        == mu.LECTURE_CANCELLED


# =================================================================================================
# 4. duplicates, 5. an existing transcript is never replaced
# =================================================================================================

def test_4_uploading_the_same_file_again_is_a_no_op(db):
    lecture = _lecture(db, 4)
    part = lecture["artifacts"][0]["artifact_id"]
    raw = _vtt()
    _upload(db, lecture, raw)
    before = _artifact(db, part), _provenance(db, lecture, part)
    again = _upload(db, lecture, raw)
    assert again["status"] == "ALREADY_STORED" and again["stored"] is False and again["duplicate"]
    assert (_artifact(db, part), _provenance(db, lecture, part)) == before    # no version, same record


def test_5a_a_graph_transcript_is_never_replaced_by_an_upload(db):
    lecture = _lecture(db, 51, status="DISCOVERED")
    part = lecture["artifacts"][0]["artifact_id"]
    graph = _vtt(offset=500)
    _store_graph_content(db, part, graph)
    service = ManualTranscriptUploadService()
    assert service.target(db, lecture["lecture_id"])["refusal"]["code"] == mu.ALL_PARTS_HAVE_TRANSCRIPTS
    preview = service.preview(db, lecture["lecture_id"], filename="t.vtt", raw=_vtt(), artifact_id=part)
    assert preview["errors"][0]["code"] == mu.TRANSCRIPT_ALREADY_STORED
    assert preview["errors"][0]["content_source"] == "MICROSOFT_GRAPH"
    assert _artifact(db, part)[1:3] == (1, content_sha256(graph))


def test_5b_a_manual_transcript_is_never_replaced_by_a_second_upload(db):
    lecture = _lecture(db, 52)
    part = lecture["artifacts"][0]["artifact_id"]
    first = _vtt()
    _upload(db, lecture, first)
    second = _vtt(offset=900)
    assert mu.TRANSCRIPT_ALREADY_STORED in _refusal(lambda: ManualTranscriptUploadService().commit(
        db, lecture["lecture_id"], filename="t.vtt", raw=second, artifact_id=part,
        expected_sha256=content_sha256(second), uploaded_by="x"))
    assert _artifact(db, part)[1:3] == (1, content_sha256(first))


# =================================================================================================
# 6. the manual transcript survives later Graph refreshes
# =================================================================================================

class Graph400:
    """Graph as it behaves for these transcripts: listed fine, content refused with 400."""

    def __init__(self, lecture, *, content: bytes | None = None):
        self.lecture = lecture
        self.content = content
        self.fetches = 0

    def list_transcripts(self, user, meeting_id):
        if meeting_id != self.lecture["meeting_id"]:
            return TranscriptListing(NO_TRANSCRIPTS_AVAILABLE)
        return TranscriptListing(TRANSCRIPTS_FOUND, artifacts=[
            TranscriptArtifact(provider_transcript_id=item["transcript_id"],
                               provider_created_at=item["start"],
                               provider_end_at=item["start"] + timedelta(hours=2),
                               provider_call_id=None, content_correlation_id=None,
                               provider_meeting_id=meeting_id, metadata={"fresh": True})
            for item in self.lecture["artifacts"]])

    def fetch_content(self, user, meeting_id, transcript_id):
        self.fetches += 1
        if self.content is None:
            return TranscriptContent(CONTENT_FETCH_ERROR, reason=PROVIDER_ERROR, http_status=400)
        return TranscriptContent("CONTENT_STORED", raw=self.content, content_format="text/vtt",
                                 speaker_attribution=True)


def _nightly_acquisition(db, graph):
    return TranscriptAcquisitionService(
        gateway=graph, lecture_repository=ReadyLectureRepository(),
        artifact_repository=TranscriptArtifactRepository(),
        run_repository=TranscriptAcquisitionRunRepository()).acquire_day(db, DAY)


def test_6a_a_later_graph_400_does_not_invalidate_the_manual_transcript(db):
    lecture = _lecture(db, 61)
    part = lecture["artifacts"][0]["artifact_id"]
    raw = _vtt()
    _upload(db, lecture, raw)
    selection_before = _selection(db, lecture)
    graph = Graph400(lecture)

    _nightly_acquisition(db, graph)

    assert graph.fetches == 1                                   # Graph was asked, exactly as before
    status, version, sha, _fetched, versions = _artifact(db, part)
    assert (status, version, sha, versions) == ("CONTENT_STORED", 1, content_sha256(raw), 1)
    assert _provenance(db, lecture, part)["source"] == "MANUAL_UPLOAD"   # Graph's upsert kept it
    assert _stage(lecture, TRANSCRIPT)(db)["state"] == COMPLETE
    assert _selection(db, lecture) == selection_before


def test_6b_a_later_graph_success_does_not_replace_the_manual_transcript(db):
    lecture = _lecture(db, 62)
    part = lecture["artifacts"][0]["artifact_id"]
    raw = _vtt()
    _upload(db, lecture, raw)
    _nightly_acquisition(db, Graph400(lecture, content=_vtt(offset=4000)))
    assert _artifact(db, part)[1:] == (1, content_sha256(raw), _artifact(db, part)[3], 1)


def test_6c_graph_storage_is_unchanged_for_transcripts_without_a_manual_upload(db):
    lecture = _lecture(db, 63, status="DISCOVERED")
    part = lecture["artifacts"][0]["artifact_id"]
    first, second = _vtt(), _vtt(offset=4000)
    _store_graph_content(db, part, first)
    _nightly_acquisition(db, Graph400(lecture, content=second))
    assert _artifact(db, part)[1:3] == (2, content_sha256(second))       # a new version, as before
    _nightly_acquisition(db, Graph400(lecture))
    assert _artifact(db, part)[0] == CONTENT_FETCH_ERROR                  # the old status rule, untouched


# =================================================================================================
# 7. multiple transcript parts
# =================================================================================================

def test_7_a_two_part_lecture_takes_each_part_and_processes_only_when_both_are_stored(db):
    lecture = _lecture(db, 7, parts=((0, 60), (65, 120)))
    first, second = lecture["artifacts"]
    service = ManualTranscriptUploadService()
    target = service.target(db, lecture["lecture_id"])
    assert [part["artifact_id"] for part in target["parts"]] == [first["artifact_id"], second["artifact_id"]]
    assert target["missing_part_count"] == 2
    # Which part a file is, is the operator's to say when more than one is missing.
    preview = service.preview(db, lecture["lecture_id"], filename="p1.vtt", raw=_vtt(3_600_000))
    assert preview["errors"][0]["code"] == mu.PART_REQUIRED

    _upload(db, lecture, _vtt(3_600_000), artifact_id=first["artifact_id"])
    plan = _recovery().plan_processing(db, lecture["lecture_id"])
    assert plan["available"] is False and plan["reason"] == WAITING_FOR_TRANSCRIPT_PARTS
    with pytest.raises(ProcessingRefused):
        _recovery().process(db, lecture["lecture_id"])
    # The only part still missing no longer needs naming.
    result = _upload(db, lecture, _vtt(3_300_000, offset=100))
    assert result["part"]["artifact_id"] == second["artifact_id"]
    assert _selection(db, lecture)[:3] == ("SELECTED", 2, 2)               # combined by the existing combiner
    assert _recovery().plan_processing(db, lecture["lecture_id"])["available"] is True


# =================================================================================================
# 8-12. processing: canonical cues, QA, one lecture only
# =================================================================================================

def _documents(db, lecture_id):
    return db.execute("""
        SELECT d.parse_status, d.cue_count, d.source_content_sha256,
               (SELECT count(*) FROM public.lecture_transcript_cues c WHERE c.document_id = d.document_id)
          FROM public.lecture_transcript_documents d WHERE d.lecture_id = %s""",
                      (lecture_id,)).fetchall()


def _footprint(db, lecture_id):
    """Every coded row a pipeline stage could write for a lecture, with its last change."""
    return db.execute("""
        SELECT (SELECT array_agg(updated_at ORDER BY updated_at) FROM public.lecture_transcript_selections
                 WHERE lecture_id = %(l)s),
               (SELECT count(*) FROM public.lecture_transcript_documents WHERE lecture_id = %(l)s),
               (SELECT count(*) FROM public.lecture_qa_evaluations WHERE lecture_id = %(l)s),
               (SELECT count(*) FROM public.lecture_attendance_snapshots WHERE lecture_id = %(l)s),
               (SELECT count(*) FROM public.lecture_engagement_metrics WHERE lecture_id = %(l)s),
               (SELECT array_agg(a.updated_at) FROM public.lecture_transcript_candidates c
                  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = c.artifact_id
                 WHERE c.lecture_id = %(l)s)""", {"l": lecture_id}).fetchone()


def test_8_9_10_12_processing_builds_canonical_cues_runs_qa_and_touches_only_this_lecture(db, provider):
    lecture = _lecture(db, 8)
    # A sibling on the same day, its Graph transcript stored and SELECTED but not yet parsed: the
    # lecture a day-wide BUILD_CANONICAL_CUES / RESOLVE_SPEAKERS would pick up in the same pass.
    # The recovery must leave it exactly as it is.
    sibling = _lecture(db, 81, start=START + timedelta(hours=3), status="DISCOVERED",
                       subject="Synthetic Sibling Lecture")
    _store_graph_content(db, sibling["artifacts"][0]["artifact_id"], _vtt(offset=8100))
    TranscriptSelectionService(
        lecture_repository=ReadyLectureRepository(), selection_repository=TranscriptSelectionRepository(),
        run_repository=TranscriptSelectionRunRepository()).select_day(
            db, DAY, lecture_ids=[sibling["lecture_id"]])
    assert _selection(db, sibling)[0] == "SELECTED" and _documents(db, sibling["lecture_id"]) == []
    sibling_before = _footprint(db, sibling["lecture_id"])
    raw = _vtt()
    _upload(db, lecture, raw)
    recovery = _recovery()
    plan = recovery.plan_processing(db, lecture["lecture_id"])
    assert plan["available"] is True
    assert plan["cost"]["affects_other_lectures_that_day"] is False
    assert plan["cost"]["buys_model_generation"] is True

    result = recovery.process(db, lecture["lecture_id"])

    run = result["run"]
    assert [item["lecture_id"] for item in run["lectures"]] == [lecture["lecture_id"]]
    (item,) = run["lectures"]
    assert item["error_code"] is None, item
    for action in ("BUILD_CANONICAL_CUES", "RESOLVE_SPEAKERS", RUN_QA, "RENDER_QA", "SYNC_LEGACY_QA"):
        assert action in item["actions"], item["actions"]
    assert ACQUIRE_TRANSCRIPT not in item["actions"]
    # 8. the canonical transcript is built from the uploaded bytes by the existing parser.
    ((parse_status, cue_count, _source_sha, stored_cues),) = _documents(db, lecture["lecture_id"])
    assert parse_status == "PARSED" and cue_count == stored_cues == len(_cues())
    # 9. the existing QA stage ran once, with the (stub) provider, and was recorded.
    assert provider.calls_total == 1 and run["provider_calls"] == 1 and run["graph_calls"] == 0
    assert db.execute("SELECT qa_status FROM public.lecture_qa_evaluations WHERE lecture_id = %s",
                      (lecture["lecture_id"],)).fetchone()[0] == "COMPLETED"
    processing = _provenance(db, lecture, lecture["artifacts"][0]["artifact_id"])["processing"]
    assert processing["status"] == "COMPLETED" and processing["run_id"] == str(run["run_id"])
    assert RUN_QA in processing["actions"]
    # 10/12. the sibling, ready to be parsed, was not: no document, no cues, no speakers, no QA,
    # its selection and artifact rows untouched. A day-wide parse would have built its document.
    assert _footprint(db, sibling["lecture_id"]) == sibling_before
    assert _documents(db, sibling["lecture_id"]) == []
    assert _stage(sibling, "CANONICAL_CUES")(db)["action"] == "BUILD_CANONICAL_CUES"

    # Idempotent: processing again finds nothing to do and buys nothing.
    again = recovery.plan_processing(db, lecture["lecture_id"])
    assert again["available"] is False and again["reason"] == NOTHING_TO_PROCESS
    with pytest.raises(ProcessingRefused):
        recovery.process(db, lecture["lecture_id"])
    assert provider.calls_total == 1


def test_the_console_never_buys_the_generation_and_the_server_command_does(db, provider):
    """
    The console's run (provider OFF) finishes every deterministic stage and stops at RUN_QA with no
    model call, so a request timeout can never discard a paid answer. The server CLI (provider ON)
    then takes exactly the remaining step, once.
    """
    lecture = _lecture(db, 9)
    _upload(db, lecture, _vtt())
    console = _recovery(allow_provider=False)
    plan = console.plan_processing(db, lecture["lecture_id"])
    assert plan["available"] is True and plan["cost"]["buys_model_generation"] is False
    assert plan["qa_runs_on_the_server"] is True
    assert plan["server_command"].endswith(f"--lecture-id {lecture['lecture_id']}")

    result = console.process(db, lecture["lecture_id"])

    (item,) = result["run"]["lectures"]
    assert "BUILD_CANONICAL_CUES" in item["actions"] and RUN_QA not in item["actions"]
    assert provider.calls_total == 0 and result["run"]["provider_calls"] == 0
    assert result["processing"]["final_action"] == RUN_QA
    assert result["processing"]["qa_pending_on_server"] is True
    assert result["processing"]["entry_point"] == "CONSOLE"
    assert _documents(db, lecture["lecture_id"])[0][0] == "PARSED"
    after = console.plan_processing(db, lecture["lecture_id"])
    assert after["available"] is False and after["reason"] == "QA_RUNS_ON_THE_SERVER"

    server = _recovery(allow_provider=True)
    done = server.process(db, lecture["lecture_id"])
    (item,) = done["run"]["lectures"]
    assert item["actions"][0] == RUN_QA and provider.calls_total == 1
    assert done["processing"]["entry_point"] == "CLI" and done["processing"]["qa_pending_on_server"] is False
    assert _provenance(db, lecture, lecture["artifacts"][0]["artifact_id"])["processing"]["entry_point"] == "CLI"


def test_11_processing_needs_a_manual_upload(db):
    lecture = _lecture(db, 11, status="DISCOVERED")
    _store_graph_content(db, lecture["artifacts"][0]["artifact_id"], _vtt())
    plan = _recovery().plan_processing(db, lecture["lecture_id"])
    assert plan["available"] is False and plan["reason"] == NO_MANUAL_UPLOAD
    with pytest.raises(ProcessingRefused) as refused:
        _recovery().process(db, lecture["lecture_id"])
    assert refused.value.code == NO_MANUAL_UPLOAD


def test_the_upload_waits_for_a_writer_holding_the_lecture(db):
    """The lecture lock the cycle and the Publisher take: an upload never races them."""
    from app.orchestration.locks import LectureBusy, LectureLockManager
    lecture = _lecture(db, 12)
    url = Settings.from_environment().database_url
    with psycopg.connect(url, autocommit=True) as other:
        assert_isolated(other)
        assert LectureLockManager().try_acquire(other, lecture["lecture_id"])
        raw = _vtt()
        with pytest.raises(LectureBusy):
            ManualTranscriptUploadService().commit(
                db, lecture["lecture_id"], filename="t.vtt", raw=raw,
                artifact_id=lecture["artifacts"][0]["artifact_id"],
                expected_sha256=content_sha256(raw), uploaded_by="x")
    assert _artifact(db, lecture["artifacts"][0]["artifact_id"])[1] == 0


def test_the_console_status_names_the_part_its_failure_and_the_upload(db):
    lecture = _lecture(db, 13)
    service = ManualTranscriptUploadService()
    before = service.target(db, lecture["lecture_id"])
    assert before["upload_allowed"] is True and before["missing_part_count"] == 1
    (part,) = before["parts"]
    assert part["graph_failure_reason"] == PROVIDER_ERROR and part["has_content"] is False
    _upload(db, lecture, _vtt(), user="someone@example.invalid")
    after = service.target(db, lecture["lecture_id"])
    assert after["upload_allowed"] is False and after["all_parts_stored"] is True
    (part,) = after["parts"]
    assert part["content_source"] == "MANUAL_UPLOAD" and part["uploaded_by"] == "someone@example.invalid"
    (record,) = after["uploads"]
    assert record["is_current_content"] is True and record["source"] == "MANUAL_UPLOAD"
    assert isinstance(datetime.fromisoformat(record["uploaded_at"]), datetime)
    assert datetime.fromisoformat(record["uploaded_at"]).tzinfo == timezone.utc


# =================================================================================================
# The Operations dashboard's recovery list: exactly the lectures the console would take an upload for
# =================================================================================================

def test_the_recovery_list_offers_exactly_the_lectures_an_upload_could_complete(db):
    missing = _lecture(db, 81)
    half = _lecture(db, 82, parts=((0, 55), (60, 120)))
    _store_graph_content(db, half["artifacts"][0]["artifact_id"], _vtt(span_ms=3_300_000))
    graph_stored = _lecture(db, 83, status="DISCOVERED")
    _store_graph_content(db, graph_stored["artifacts"][0]["artifact_id"], _vtt(offset=300))
    uploaded = _lecture(db, 84)
    _upload(db, uploaded, _vtt(offset=600))
    cancelled = _lecture(db, 85)
    db.execute("UPDATE public.lecture_sessions SET is_cancelled = true WHERE lecture_id = %s",
               (cancelled["lecture_id"],))
    # Listed content-less, but for another day's occurrence: the prefilter finds it, target() refuses.
    other_day = _lecture(db, 86, parts=((-7 * 24 * 60, -7 * 24 * 60 + 120),))

    service = ManualTranscriptUploadService()
    queue = service.recovery_queue(db, date_from=DAY, date_to=DAY)
    listed = {row["lecture_id"]: row for row in queue["lectures"]}
    assert missing["lecture_id"] in listed and half["lecture_id"] in listed
    for absent in (graph_stored, uploaded, cancelled, other_day):
        assert absent["lecture_id"] not in listed
    # Every entry is one the per-lecture console would accept.
    for lecture_id in listed:
        assert service.target(db, lecture_id)["upload_allowed"] is True

    row = listed[half["lecture_id"]]
    assert (row["part_count"], row["missing_part_count"]) == (2, 1)
    assert row["missing_parts"][0]["artifact_id"] == half["artifacts"][1]["artifact_id"]
    assert row["missing_parts"][0]["graph_failure_reason"] == PROVIDER_ERROR
    assert listed[missing["lecture_id"]]["subject"] == SUBJECT
    assert listed[missing["lecture_id"]]["session_date"] == DAY.isoformat()

    # Unbounded is every date; a bound that excludes the day lists none of these.
    unbounded = {row["lecture_id"] for row in service.recovery_queue(db)["lectures"]}
    assert {missing["lecture_id"], half["lecture_id"]} <= unbounded
    later = service.recovery_queue(db, date_from=DAY + timedelta(days=1))
    assert not {missing["lecture_id"], half["lecture_id"]} & {row["lecture_id"] for row in later["lectures"]}

    # After the upload the lecture leaves the list.
    _upload(db, missing, _vtt(offset=1200))
    after = {row["lecture_id"] for row in service.recovery_queue(db, date_from=DAY, date_to=DAY)["lectures"]}
    assert missing["lecture_id"] not in after and half["lecture_id"] in after
